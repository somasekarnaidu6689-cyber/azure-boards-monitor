"""
storage/comment_history.py

Gap addressed: "Data Quality" (Med) — TF-IDF copy-paste detection was
initialized fresh every run with only the current COMMENT_LOOKBACK_DAYS
window. A developer who repeats the same boilerplate comment across weeks
slips through because earlier comments are discarded between runs.

This module persists each day's "today" comment text per assignee into a
small Databricks table (CommentHistory) and loads recent history back at
the start of a run, so detect_copy_paste() can compare against a much
wider corpus than just the last COMMENT_LOOKBACK_DAYS. It also enables
tracking comment-quality trend lines per developer over time (the
secondary benefit called out in the gap analysis).

Failure mode: if Databricks is unavailable, both read and write degrade to
a no-op with a warning — the pipeline still runs using only the
in-run lookback window (the previous behavior), it just doesn't get worse.
"""

import logging
from datetime import date
from config import Config
from storage.databricks_client import get_connection, execute

logger = logging.getLogger(__name__)

_DDL_COMMENT_HISTORY = """
CREATE TABLE IF NOT EXISTS {t} (
    snapshot_date    DATE    NOT NULL,
    assignee_email   STRING  NOT NULL,
    work_item_id     INT     NOT NULL,
    comment_text     STRING
)
USING DELTA
COMMENT 'Per-assignee EOD comment text history, used to widen the copy-paste detection corpus beyond the current lookback window'
"""


def init_comment_history_schema() -> None:
    fq = Config.db_table("CommentHistory")
    with get_connection() as conn:
        execute(conn, _DDL_COMMENT_HISTORY.format(t=fq))


def load_recent_comments_by_assignee(assignee_emails: list[str]) -> dict[str, list[str]]:
    """
    Load up to COMMENT_HISTORY_MAX_PER_ASSIGNEE most recent historical
    comment texts per assignee email, most recent first.
    Returns {} (degraded, not an error) on any failure.
    """
    if not Config.PERSIST_COMMENT_HISTORY or not assignee_emails:
        return {}

    emails = [e for e in set(assignee_emails) if e]
    if not emails:
        return {}

    fq = Config.db_table("CommentHistory")
    emails_list = ", ".join("'" + e.replace("'", "''") + "'" for e in emails)
    limit = Config.COMMENT_HISTORY_MAX_PER_ASSIGNEE

    sql = f"""
        SELECT assignee_email, comment_text, snapshot_date
        FROM {fq}
        WHERE assignee_email IN ({emails_list})
            AND comment_text IS NOT NULL
        ORDER BY snapshot_date DESC
    """

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
    except Exception as exc:
        logger.warning(
            "load_recent_comments_by_assignee failed — falling back to "
            "in-run lookback window only: %s", exc
        )
        return {}

    history: dict[str, list[str]] = {}
    for email, text, _snapshot_date in rows:
        bucket = history.setdefault(email, [])
        if len(bucket) < limit:
            bucket.append(text)

    return history


def save_today_comments(tasks: list[dict], snapshot_date: date) -> None:
    """
    Append one row per task that has a today_comment_text, for use as
    history in future runs. Best-effort: failures are logged and swallowed
    so a Databricks blip never blocks email delivery.
    """
    if not Config.PERSIST_COMMENT_HISTORY:
        return

    rows = []
    for task in tasks:
        text = task.get("today_comment_text")
        email = (task.get("assignee") or {}).get("email") or ""
        if not text or not email:
            continue
        rows.append((snapshot_date, email, task["id"], text))

    if not rows:
        return

    fq = Config.db_table("CommentHistory")
    insert_sql = f"INSERT INTO {fq} VALUES (?, ?, ?, ?)"

    try:
        with get_connection() as conn:
            # Idempotency: remove any rows already written today for these
            # work items before inserting (safe to re-run).
            ids_str = ", ".join(str(r[2]) for r in rows)
            execute(
                conn,
                f"DELETE FROM {fq} WHERE snapshot_date = '{snapshot_date}' "
                f"AND work_item_id IN ({ids_str})",
            )
            with conn.cursor() as cur:
                cur.executemany(insert_sql, rows)
        logger.info("Persisted %d comment(s) to CommentHistory for future copy-paste comparisons.", len(rows))
    except Exception as exc:
        logger.warning("save_today_comments failed (non-fatal): %s", exc)
