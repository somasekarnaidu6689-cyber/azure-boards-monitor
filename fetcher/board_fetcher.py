import logging
from datetime import datetime, timezone

from fetcher.azure_client import (
    get_active_sprint,
    get_task_ids_in_sprint,
    get_work_item_details,
    get_comments_for_all_tasks,
)
from config import Config

logger = logging.getLogger(__name__)


def _parse_assignee(assigned_to_field) -> dict:
    """
    Azure returns AssignedTo as either a dict or None.
    Normalise to {display_name, email}.
    """
    if not assigned_to_field:
        return {"display_name": "Unassigned", "email": ""}
    if isinstance(assigned_to_field, dict):
        return {
            "display_name": assigned_to_field.get("displayName", ""),
            "email": assigned_to_field.get("uniqueName", ""),
        }
    return {"display_name": str(assigned_to_field), "email": ""}


def _days_since(date_str: str) -> int:
    """Return number of full days between date_str (ISO 8601) and now UTC."""
    if not date_str:
        return 0
    dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    delta = datetime.now(timezone.utc) - dt
    return max(0, delta.days)


def fetch_active_sprint_tasks() -> dict:
    """
    Main entry point for the fetcher layer.

    Returns a structured payload:
    {
        "sprint": { name, iteration_path, start_date, finish_date,
                    days_remaining, total_days },
        "fetched_at": ISO timestamp,
        "tasks": [
            {
                "id": int,
                "title": str,
                "work_item_type": str,
                "state": str,
                "iteration_path": str,
                "assignee": { display_name, email },
                "remaining_hours": float | None,
                "original_estimate": float | None,
                "days_since_state_change": int,
                "days_since_last_update": int,
                "comments": [
                    {
                        "comment_id": int,
                        "text": str,
                        "author_display_name": str,
                        "author_email": str,
                        "created_date": str,
                        "is_today": bool,
                    }
                ],
                "has_comment_today": bool,
                "today_comment_text": str | None,
                "recent_comment_texts": list[str],  # last 3 days excl. today
            }
        ]
    }
    """
    logger.info("Fetching active sprint...")
    sprint = get_active_sprint()

    sprint_name = sprint.get("name", "")
    iteration_path = sprint.get("path", sprint.get("id", ""))
    # Azure returns these keys with a null value (not absent) when sprint
    # dates haven't been configured for the iteration, so coerce to "" here.
    attributes = sprint.get("attributes") or {}
    start_date_str = attributes.get("startDate") or ""
    finish_date_str = attributes.get("finishDate") or ""

    # Calculate days remaining in sprint
    now = datetime.now(timezone.utc)
    days_remaining = 0
    total_days = 0
    if finish_date_str:
        finish_dt = datetime.fromisoformat(finish_date_str.replace("Z", "+00:00"))
        days_remaining = max(0, (finish_dt.date() - now.date()).days)
    if start_date_str and finish_date_str:
        start_dt = datetime.fromisoformat(start_date_str.replace("Z", "+00:00"))
        finish_dt = datetime.fromisoformat(finish_date_str.replace("Z", "+00:00"))
        total_days = max(1, (finish_dt.date() - start_dt.date()).days)

    if not finish_date_str:
        logger.warning(
            "Sprint '%s' has no startDate/finishDate configured in Azure DevOps. "
            "days_remaining will default to 0 and the sprint-urgency risk signal "
            "will not contribute meaningfully. Configure sprint dates in "
            "Project Settings > Iterations for accurate risk scoring.",
            sprint_name,
        )

    logger.info(
        "Active sprint: '%s' | path: '%s' | days remaining: %d",
        sprint_name,
        iteration_path,
        days_remaining,
    )

    logger.info("Fetching child task IDs linked to sprint user stories...")
    task_ids = get_task_ids_in_sprint(iteration_path)
    logger.info("Found %d child work item(s) linked to sprint user stories", len(task_ids))

    if not task_ids:
        return {
            "sprint": {
                "name": sprint_name,
                "iteration_path": iteration_path,
                "start_date": start_date_str,
                "finish_date": finish_date_str,
                "days_remaining": days_remaining,
                "total_days": total_days,
                "dates_configured": bool(finish_date_str),
            },
            "fetched_at": now.isoformat(),
            "tasks": [],
        }

    logger.info("Fetching work item details...")
    raw_items = get_work_item_details(task_ids)

    # Filter to in-scope types before fetching comments
    in_scope = [
        item for item in raw_items
        if item.get("fields", {}).get("System.WorkItemType", "") in Config.ALLOWED_WORK_ITEM_TYPES
    ]

    if not in_scope:
        logger.info("No in-scope work items after type filter.")
        return {
            "sprint": {
                "name": sprint_name,
                "iteration_path": iteration_path,
                "start_date": start_date_str,
                "finish_date": finish_date_str,
                "days_remaining": days_remaining,
                "total_days": total_days,
                "dates_configured": bool(finish_date_str),
            },
            "fetched_at": now.isoformat(),
            "tasks": [],
        }

    # Fetch ALL comments in parallel — one request per task, all concurrent
    logger.info("Fetching comments for %d task(s) in parallel...", len(in_scope))
    in_scope_ids = [item.get("id") for item in in_scope]
    all_comments_map = get_comments_for_all_tasks(in_scope_ids)

    tasks = []
    for item in in_scope:
        fields = item.get("fields", {})
        work_item_type = fields.get("System.WorkItemType", "")
        task_id = item.get("id")
        state_change_date = fields.get("Microsoft.VSTS.Common.StateChangeDate", "")
        changed_date = fields.get("System.ChangedDate", "")

        assignee = _parse_assignee(fields.get("System.AssignedTo"))
        comments = all_comments_map.get(task_id, [])

        today_comments = [c for c in comments if c["is_today"]]
        has_comment_today = len(today_comments) > 0
        today_comment = today_comments[0] if today_comments else None
        today_comment_text = today_comment["text"] if today_comment else None
        today_image_urls = today_comment["image_urls"] if today_comment else []
        recent_comment_texts = [c["text"] for c in comments if not c["is_today"]]

        tasks.append(
            {
                "id": task_id,
                "title": fields.get("System.Title", ""),
                "work_item_type": work_item_type,
                "state": fields.get("System.State", ""),
                "iteration_path": fields.get("System.IterationPath", ""),
                "assignee": assignee,
                "remaining_hours": fields.get(
                    "Microsoft.VSTS.Scheduling.RemainingWork"
                ),
                "original_estimate": fields.get(
                    "Microsoft.VSTS.Scheduling.OriginalEstimate"
                ),
                "days_since_state_change": _days_since(state_change_date),
                "days_since_last_update": _days_since(changed_date),
                "comments": comments,
                "has_comment_today": has_comment_today,
                "today_comment_text": today_comment_text,
                "today_image_urls": today_image_urls,
                "recent_comment_texts": recent_comment_texts,
            }
        )

    logger.info(
        "Assembled %d Task work item(s) with comments", len(tasks)
    )

    return {
        "sprint": {
            "name": sprint_name,
            "iteration_path": iteration_path,
            "start_date": start_date_str,
            "finish_date": finish_date_str,
            "days_remaining": days_remaining,
            "total_days": total_days,
            "dates_configured": bool(finish_date_str),
        },
        "fetched_at": now.isoformat(),
        "tasks": tasks,
    }