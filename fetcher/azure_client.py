import base64
import logging
import re
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Optional

from config import Config
from utils.retry import retryable

logger = logging.getLogger(__name__)

# ── Shared session — reuses TCP connections across all calls ─────────────────
_session: Optional[requests.Session] = None

# Service-principal token cache: (token, expires_at_epoch_seconds)
_sp_token_cache: Optional[tuple[str, float]] = None


def _get_service_principal_token() -> str:
    """
    Acquire (and cache) an AAD access token for Azure DevOps using the
    configured service principal. Refreshes automatically a minute before
    expiry. Used when Config.AUTH_MODE == "service_principal".

    Gap addressed: "Authentication" (High) — replaces the user-bound PAT
    with a service principal / managed identity so the pipeline does not
    silently break if an individual's token expires or they leave.
    """
    global _sp_token_cache

    if _sp_token_cache and _sp_token_cache[1] - 60 > time.time():
        return _sp_token_cache[0]

    from azure.identity import ClientSecretCredential

    credential = ClientSecretCredential(
        tenant_id=Config.AZURE_SP_TENANT_ID,
        client_id=Config.AZURE_SP_CLIENT_ID,
        client_secret=Config.AZURE_SP_CLIENT_SECRET,
    )
    scope = f"{Config.AZURE_DEVOPS_RESOURCE_ID}/.default"
    token = credential.get_token(scope)
    _sp_token_cache = (token.token, token.expires_on)
    logger.info("Acquired new Azure DevOps service-principal token (expires %s).",
                datetime.fromtimestamp(token.expires_on, tz=timezone.utc).isoformat())
    return token.token


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"Content-Type": "application/json"})

    if Config.AUTH_MODE == "service_principal":
        _session.headers["Authorization"] = f"Bearer {_get_service_principal_token()}"
    else:
        token = base64.b64encode(f":{Config.AZURE_PAT}".encode()).decode()
        _session.headers["Authorization"] = f"Basic {token}"

    return _session


def validate_auth() -> None:
    """
    Startup validation: confirm the configured credential (PAT or service
    principal) is actually valid before the pipeline makes any real API
    calls, rather than discovering an expired/revoked token deep into the
    run after partial work has already happened.

    Gap addressed: "Authentication" (High).
    Raises RuntimeError with a clear message if auth fails.
    """
    url = f"https://dev.azure.com/{Config.AZURE_ORG}/_apis/projects?api-version=7.1&$top=1"
    try:
        response = _get_session().get(url, timeout=15)
        if response.status_code == 401:
            mode = Config.AUTH_MODE
            raise RuntimeError(
                f"Azure DevOps authentication failed (401) using AUTH_MODE='{mode}'. "
                + (
                    "The PAT is invalid, expired, or lacks Work Items: Read scope."
                    if mode == "pat"
                    else "The service principal credentials are invalid or lack project access."
                )
            )
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Azure DevOps auth validation request failed: {exc}") from exc

    logger.info("Azure DevOps authentication validated successfully (AUTH_MODE=%s).", Config.AUTH_MODE)


@retryable("azure-devops")
def _get(url: str, params: Optional[dict] = None) -> dict:
    response = _get_session().get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


@retryable("azure-devops")
def _post(url: str, body: dict) -> dict:
    response = _get_session().post(url, json=body, timeout=30)
    response.raise_for_status()
    return response.json()


# ── HTML utilities ───────────────────────────────────────────────────────────

def _extract_image_urls(html: str) -> list[str]:
    """
    Extract all image URLs from HTML comment text before stripping tags.
    Captures src attributes of <img> tags.
    Returns a list of URL strings (empty list if none).
    """
    if not html:
        return []
    return re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)


def _strip_html(text: str) -> str:
    """
    Strip HTML tags and decode entities to produce plain text for LLM input.
    """
    if not text:
        return ""
    text = re.sub(r"</(div|p|li|br)\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


# ── Sprint ───────────────────────────────────────────────────────────────────

def get_active_sprint() -> dict:
    url = (
        f"https://dev.azure.com/{Config.AZURE_ORG}/{Config.AZURE_PROJECT}"
        f"/{Config.AZURE_TEAM}/_apis/work/teamsettings/iterations"
    )
    params = {"$timeframe": "current", "api-version": "7.1"}
    data = _get(url, params)
    iterations = data.get("value", [])
    if not iterations:
        raise RuntimeError(
            f"No active sprint found for team '{Config.AZURE_TEAM}' "
            f"in project '{Config.AZURE_PROJECT}'"
        )
    return iterations[0]


# ── Work item discovery ──────────────────────────────────────────────────────

def get_parent_story_ids_in_sprint(iteration_path: str) -> list[int]:
    url = f"{Config.AZURE_BASE_URL}/wit/wiql?api-version=7.1"
    parent_types = ", ".join(f"'{t}'" for t in Config.PARENT_WORK_ITEM_TYPES)
    wiql = {
        "query": (
            "SELECT [System.Id], [System.Title], [System.WorkItemType], "
            "[System.State], [System.AssignedTo] "
            "FROM WorkItems "
            f"WHERE [System.IterationPath] = '{iteration_path}' "
            f"AND [System.WorkItemType] IN ({parent_types}) "
            "AND [System.State] NOT IN ('Closed', 'Removed') "
            "ORDER BY [System.Id]"
        )
    }
    data = _post(url, wiql)
    return [item["id"] for item in data.get("workItems", [])]


def _fetch_child_ids_for_parent(parent_id: int) -> list[int]:
    """Fetch child task IDs for a single parent — used inside thread pool."""
    url = f"{Config.AZURE_BASE_URL}/wit/wiql?api-version=7.1"
    wiql = {
        "query": (
            "SELECT [System.Id] FROM WorkItemLinks "
            f"WHERE ([Source].[System.Id] = {parent_id}) "
            "AND ([System.Links.LinkType] = 'System.LinkTypes.Hierarchy-Forward') "
            "MODE (MustContain)"
        )
    }
    data = _post(url, wiql)
    relations = data.get("workItemRelations", [])
    return [
        rel["target"]["id"]
        for rel in relations
        if rel.get("rel") is not None and rel.get("target")
    ]


def get_task_ids_in_sprint(iteration_path: str) -> list[int]:
    """
    Two-step lookup with parallel child fetching:
    1. Find all parent items in the sprint (single WIQL call).
    2. Fetch child IDs for all parents in parallel (ThreadPoolExecutor).
    """
    parent_ids = get_parent_story_ids_in_sprint(iteration_path)
    logger.info("Found %d parent work item(s) in sprint", len(parent_ids))

    all_child_ids: set[int] = set()

    with ThreadPoolExecutor(max_workers=min(10, len(parent_ids) or 1)) as pool:
        futures = {pool.submit(_fetch_child_ids_for_parent, pid): pid for pid in parent_ids}
        for future in as_completed(futures):
            try:
                all_child_ids.update(future.result())
            except Exception as exc:
                logger.warning("Child fetch failed for parent %d: %s", futures[future], exc)

    return sorted(all_child_ids)


def get_work_item_details(work_item_ids: list[int]) -> list[dict]:
    """
    Batch-fetch work item details in chunks of 200 (single call per chunk).
    No fields filter — avoids 400s from missing fields in some process templates.
    """
    if not work_item_ids:
        return []

    results = []
    chunk_size = 200
    for i in range(0, len(work_item_ids), chunk_size):
        chunk = work_item_ids[i: i + chunk_size]
        ids_str = ",".join(str(wid) for wid in chunk)
        url = f"{Config.AZURE_BASE_URL}/wit/workitems?ids={ids_str}&api-version=7.1"
        data = _get(url)
        results.extend(data.get("value", []))

    return results


# ── Comments — parallel fetch ────────────────────────────────────────────────

def _fetch_comments_for_one(work_item_id: int) -> tuple[int, list[dict]]:
    """
    Fetch and filter comments for a single work item.
    Returns (work_item_id, comments_list).
    Used inside ThreadPoolExecutor.
    """
    url = (
        f"{Config.AZURE_BASE_URL}/wit/workItems/{work_item_id}"
        f"/comments?api-version=7.1-preview.3"
    )
    data = _get(url)
    all_comments = data.get("comments", [])

    cutoff = datetime.now(timezone.utc) - timedelta(days=Config.COMMENT_LOOKBACK_DAYS)
    today = datetime.now(timezone.utc).date()

    filtered = []
    for c in all_comments:
        created_str = c.get("createdDate", "")
        if not created_str:
            continue
        created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        if created_dt < cutoff:
            continue

        raw_html = c.get("text", "")
        author = c.get("createdBy", {})
        filtered.append({
            "comment_id": c.get("id"),
            "text": _strip_html(raw_html),
            "image_urls": _extract_image_urls(raw_html),   # new: extracted before stripping
            "author_display_name": author.get("displayName", ""),
            "author_email": author.get("uniqueName", ""),
            "created_date": created_dt.isoformat(),
            "is_today": created_dt.date() == today,
        })

    filtered.sort(key=lambda x: x["created_date"], reverse=True)
    return work_item_id, filtered


def get_comments_for_all_tasks(work_item_ids: list[int]) -> dict[int, list[dict]]:
    """
    Fetch comments for ALL work items in parallel.
    Returns dict of work_item_id -> list of comment dicts.
    """
    results: dict[int, list[dict]] = {}
    max_workers = min(20, len(work_item_ids) or 1)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_comments_for_one, wid): wid for wid in work_item_ids}
        for future in as_completed(futures):
            wid = futures[future]
            try:
                item_id, comments = future.result()
                results[item_id] = comments
            except Exception as exc:
                logger.warning("Comment fetch failed for work item %d: %s", wid, exc)
                results[wid] = []

    return results


# Keep the old single-item function for backward compat with any other callers
def get_comments_for_work_item(work_item_id: int) -> list[dict]:
    _, comments = _fetch_comments_for_one(work_item_id)
    return comments