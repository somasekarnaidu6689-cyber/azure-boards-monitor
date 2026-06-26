"""
storage/writer.py

Maps the enriched pipeline report into TaskDailySnapshot rows and writes
them all in a single executemany call (one round trip to Databricks).

Idempotency: DELETE rows for today's date + work_item_id first, then INSERT.
Two SQL statements total regardless of how many tasks there are.
"""

import logging
from datetime import datetime, timezone, date
from config import Config
from storage.databricks_client import get_connection, execute

logger = logging.getLogger(__name__)

_INSERT_SQL = """
INSERT INTO {t} VALUES (
    ?,?,?,?,?,
    ?,?,?,?,?,
    ?,?,?,?,?,?,?,?,?,?,
    ?,?,?,?,?,?,
    ?,?,
    ?,?,?,?,?,?,?,
    ?,?,?,?,
    ?,?,
    ?,?,?
)
"""


def _build_row(
    task: dict,
    sprint: dict,
    snapshot_date: date,
    snapshot_ts: datetime,
    azure_team: str | None = None,
) -> tuple:
    """Map one enriched task dict into a flat tuple matching the INSERT column order."""
    analysis = task.get("analysis", {})
    risk     = task.get("risk", {})
    signals  = risk.get("signals", {})
    nudge    = task.get("nudge") or {}
    assignee = task.get("assignee", {})
    comments = task.get("comments", [])
    delivery = task.get("delivery_status", {})

    # Today's comment details
    today_comment = next((c for c in comments if c.get("is_today")), None)
    today_ts = None
    if today_comment and today_comment.get("created_date"):
        try:
            today_ts = datetime.fromisoformat(
                today_comment["created_date"].replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except Exception:
            today_ts = None

    image_urls_str = "|".join(task.get("today_image_urls", [])) or None

    # Recent comment metadata (pipe-separated author names, excl. today)
    recent = [c for c in comments if not c.get("is_today")]
    recent_authors = "|".join(
        dict.fromkeys(c.get("author_display_name", "") for c in recent)
    ) or None

    sprint_start = sprint.get("start_date") or None
    sprint_finish = sprint.get("finish_date") or None

    return (
        # Identity
        snapshot_date,
        snapshot_ts,
        Config.AZURE_ORG   or "",
        Config.AZURE_PROJECT or "",
        azure_team or Config.AZURE_TEAM or None,

        # Sprint
        sprint.get("name", ""),
        sprint.get("iteration_path", ""),
        sprint_start,
        sprint_finish,
        sprint.get("days_remaining", 0),

        # Task
        task["id"],
        task.get("title", ""),
        task.get("work_item_type", ""),
        task.get("state", ""),
        assignee.get("display_name"),
        assignee.get("email") or None,
        task.get("remaining_hours"),
        task.get("original_estimate"),
        task.get("days_since_state_change", 0),
        task.get("days_since_last_update", 0),

        # Today's comment
        task.get("has_comment_today", False),
        task.get("today_comment_text") or None,
        today_comment.get("author_display_name") if today_comment else None,
        today_comment.get("author_email") if today_comment else None,
        today_ts,
        image_urls_str,

        # Comment history
        len(recent),
        recent_authors,

        # AI analysis
        analysis.get("quality_score", 0),
        analysis.get("quality_label", "missing"),
        analysis.get("copy_paste_detected", False),
        analysis.get("blocker_detected", False),
        signals.get("hours_stale", False),
        analysis.get("sentiment") or None,
        analysis.get("suggested_followup") or None,

        # Risk
        risk.get("risk_score", 0),
        risk.get("risk_label", ""),
        task.get("needs_attention", False),
        analysis.get("has_comment_today", False),  # eod_compliant

        # Nudge
        nudge.get("tone") or None,
        nudge.get("message") or None,

        # Delivery status — populated by main.py after send_report() /
        # send_individual_task_emails() / Teams notification return.
        # Defaults to "pending" if save_report() is called before delivery
        # (use update_delivery_status() afterwards to backfill).
        delivery.get("report_email_status", "pending"),
        delivery.get("individual_email_status", "pending"),
        delivery.get("teams_notification_status", "pending"),
    )


def save_report(report: dict, azure_team: str | None = None) -> None:
    """
    Write all enriched tasks to TaskDailySnapshot in two SQL statements:
      1. DELETE today's rows for these work item IDs (idempotency)
      2. INSERT all rows in one executemany batch
    """
    sprint = report["sprint"]
    tasks  = report["tasks"]

    if not tasks:
        logger.info("No tasks to write — skipping Databricks.")
        return

    snapshot_ts   = datetime.now(timezone.utc).replace(tzinfo=None)
    snapshot_date = snapshot_ts.date()
    fq            = Config.db_table("TaskDailySnapshot")

    logger.info(
        "Writing %d task(s) to %s (%s)...",
        len(tasks), fq, snapshot_date,
    )

    rows = [_build_row(t, sprint, snapshot_date, snapshot_ts, azure_team) for t in tasks]
    task_ids_str = ", ".join(str(t["id"]) for t in tasks)

    with get_connection() as conn:
        # DELETE existing rows for today + these IDs (idempotency)
        execute(
            conn,
            f"DELETE FROM {fq} "
            f"WHERE snapshot_date = '{snapshot_date}' "
            f"AND work_item_id IN ({task_ids_str})"
        )

        # Bulk INSERT — single round trip
        insert_sql = _INSERT_SQL.format(t=fq)
        with conn.cursor() as cur:
            cur.executemany(insert_sql, rows)

    logger.info(
        "Databricks write complete — %d row(s) inserted into %s.",
        len(rows), fq,
    )


def update_delivery_status(
    work_item_ids: list[int],
    snapshot_date: date,
    field: str,
    status: str,
) -> None:
    """
    Backfill a delivery-status column (report_email_status,
    individual_email_status, or teams_notification_status) for a batch of
    work items after a send attempt completes. Best-effort: logs and
    swallows failures so a Databricks blip never masks a successful send.
    """
    if field not in ("report_email_status", "individual_email_status", "teams_notification_status"):
        raise ValueError(f"Unknown delivery status field: {field}")
    if not work_item_ids:
        return

    fq = Config.db_table("TaskDailySnapshot")
    ids_str = ", ".join(str(i) for i in work_item_ids)

    try:
        with get_connection() as conn:
            execute(
                conn,
                f"UPDATE {fq} SET {field} = '{status}' "
                f"WHERE snapshot_date = '{snapshot_date}' AND work_item_id IN ({ids_str})"
            )
    except Exception as exc:
        logger.warning("update_delivery_status(%s) failed (non-fatal): %s", field, exc)
