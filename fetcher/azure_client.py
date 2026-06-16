import base64
import logging
import re
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

from config import Config

logger = logging.getLogger(__name__)


def _strip_html(text: str) -> str:
    """
    Azure DevOps comments are returned as HTML (e.g. '<div>testing phase</div>').
    Strip tags and collapse whitespace to get plain text for LLM/embedding input.
    """
    if not text:
        return ""
    # Replace common block-level tags with newlines before stripping
    text = re.sub(r"</(div|p|li|br)\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    # Strip all remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode common HTML entities
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _auth_header() -> dict:
    token = base64.b64encode(f":{Config.AZURE_PAT}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }


def _get(url: str, params: Optional[dict] = None) -> dict:
    response = requests.get(url, headers=_auth_header(), params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def _post(url: str, body: dict) -> dict:
    response = requests.post(url, headers=_auth_header(), json=body, timeout=30)
    response.raise_for_status()
    return response.json()


def get_active_sprint() -> dict:
    """
    Fetch the currently active sprint (iteration) for the configured team.
    Returns the sprint object with id, name, path, attributes (startDate,
    finishDate, timeFrame).
    """
    url = (
        f"https://dev.azure.com/{Config.AZURE_ORG}/{Config.AZURE_PROJECT}"
        f"/{Config.AZURE_TEAM}/_apis/work/teamsettings/iterations"
    )
    params = {
        "$timeframe": "current",
        "api-version": "7.1",
    }
    data = _get(url, params)
    iterations = data.get("value", [])
    if not iterations:
        raise RuntimeError(
            "No active sprint found for team "
            f"'{Config.AZURE_TEAM}' in project '{Config.AZURE_PROJECT}'"
        )
    # The API returns only current when $timeframe=current
    return iterations[0]


def get_parent_story_ids_in_sprint(iteration_path: str) -> list[int]:
    """
    Find all parent work items (e.g. User Stories) in the active sprint.
    These are the "container" items whose child Tasks we actually care about.
    """
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


def get_child_task_ids(parent_id: int) -> list[int]:
    """
    Run a 'oneHop' WIQL query to find all child work items linked to the
    given parent via System.LinkTypes.Hierarchy-Forward. Returns the IDs
    of the child work items (the "target" side of each relation).
    """
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

    child_ids = []
    for rel in relations:
        # The first relation in the result has rel=None and represents the
        # source item itself (target == parent_id) -- skip it.
        if rel.get("rel") is None:
            continue
        target = rel.get("target")
        if target and target.get("id"):
            child_ids.append(target["id"])

    return child_ids


def get_task_ids_in_sprint(iteration_path: str) -> list[int]:
    """
    Two-step lookup:
    1. Find all parent items (e.g. User Stories) in the active sprint.
    2. For each parent, find its child Task work items via hierarchy links.

    Returns a deduplicated, sorted list of child Task IDs whose
    WorkItemType is in Config.ALLOWED_WORK_ITEM_TYPES. The type filter is
    applied later in board_fetcher.py once full work item details are
    fetched, since the oneHop query only returns IDs.
    """
    parent_ids = get_parent_story_ids_in_sprint(iteration_path)
    logger.info("Found %d parent work item(s) in sprint", len(parent_ids))

    all_child_ids: set[int] = set()
    for parent_id in parent_ids:
        child_ids = get_child_task_ids(parent_id)
        all_child_ids.update(child_ids)

    return sorted(all_child_ids)


def get_work_item_details(work_item_ids: list[int]) -> list[dict]:
    """
    Batch-fetch full details for the given work item IDs.
    Returns list of work item detail dicts.

    Note: we do NOT pass a 'fields' filter here. Some process templates
    (e.g. Agile "User Story") don't support fields like
    Microsoft.VSTS.Scheduling.RemainingWork, and Azure DevOps returns a
    400 Bad Request if an unsupported field is requested. Fetching all
    fields and extracting what we need downstream (board_fetcher.py) is
    more robust across process templates.
    """
    if not work_item_ids:
        return []

    # Azure DevOps batch API accepts up to 200 IDs at a time
    results = []
    chunk_size = 200

    for i in range(0, len(work_item_ids), chunk_size):
        chunk = work_item_ids[i : i + chunk_size]
        ids_str = ",".join(str(wid) for wid in chunk)
        url = f"{Config.AZURE_BASE_URL}/wit/workitems?ids={ids_str}&api-version=7.1"
        data = _get(url)
        results.extend(data.get("value", []))

    return results


def get_comments_for_work_item(work_item_id: int) -> list[dict]:
    """
    Fetch all comments for a work item and return only those added
    within the last COMMENT_LOOKBACK_DAYS days (section 6, Agent 1 spec).
    Each comment includes: id, text, createdBy email, createdDate.
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
        # Azure returns ISO 8601 with trailing Z
        created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        if created_dt >= cutoff:
            author = c.get("createdBy", {})
            filtered.append(
                {
                    "comment_id": c.get("id"),
                    "text": _strip_html(c.get("text", "")),
                    "author_display_name": author.get("displayName", ""),
                    "author_email": author.get("uniqueName", ""),
                    "created_date": created_dt.isoformat(),
                    "is_today": created_dt.date() == today,
                }
            )

    # Most recent first
    filtered.sort(key=lambda x: x["created_date"], reverse=True)
    return filtered