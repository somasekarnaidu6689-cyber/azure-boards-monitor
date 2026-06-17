"""
storage/writer.py

Reads the enriched report produced by agents/pipeline.py and writes
snapshot data to all six Databricks tables in the correct dependency order:

    1. DimDate        — upsert today's date row
    2. DimSprint      — upsert sprint row
    3. DimMember      — upsert one row per unique assignee
    4. DimTask        — upsert one row per task
    5. FactTaskSnapshot — insert one row per task (Task x Day)
    6. FactComments   — insert one row per comment (deduplicated on comment_id)

All dimension upserts use MERGE ON primary key so re-runs are safe.
Fact inserts are deduplicated: FactTaskSnapshot merges on (snapshot_date, work_item_id);
FactComments merges on (comment_id).
"""

import logging
from datetime import datetime, timezone, date
from config import Config
from storage.databricks_client import get_connection, execute, executemany

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------

def _sprint_key(sprint: dict) -> str:
    """Stable sprint key: org/project/iteration_path."""
    return (
        f"{Config.AZURE_ORG}/{Config.AZURE_PROJECT}/"
        f"{sprint.get('iteration_path', sprint.get('name', 'unknown'))}"
    )


def _member_key(email: str) -> str:
    """Member key is the normalized Azure DevOps uniqueName (email)."""
    return email.strip().lower() if email else "unassigned"


def _task_key(work_item_id: int) -> int:
    """Task key is the Azure DevOps work item ID (already globally unique)."""
    return work_item_id


# ---------------------------------------------------------------------------
# DimDate
# ---------------------------------------------------------------------------

def _upsert_dim_date(conn, today: date) -> None:
    t = Config.db_table("DimDate")
    day_name = today.strftime("%A")
    weekday = today.isoweekday()  # 1=Mon … 7=Sun
    is_weekend = weekday >= 6

    sql = f"""
    MERGE INTO {t} AS tgt
    USING (SELECT
        CAST('{today.isoformat()}' AS DATE)    AS date_key,
        {today.year}                            AS year,
        {(today.month - 1) // 3 + 1}           AS quarter,
        {today.month}                           AS month,
        {today.isocalendar()[1]}                AS week_of_year,
        {weekday}                               AS day_of_week,
        '{day_name}'                            AS day_name,
        {str(is_weekend).upper()}               AS is_weekend,
        {str(not is_weekend).upper()}           AS is_working_day
    ) AS src ON tgt.date_key = src.date_key
    WHEN NOT MATCHED THEN INSERT *
    """
    execute(conn, sql)
    logger.debug("DimDate upserted for %s", today)


# ---------------------------------------------------------------------------
# DimSprint
# ---------------------------------------------------------------------------

def _upsert_dim_sprint(conn, sprint: dict) -> None:
    t = Config.db_table("DimSprint")
    sk = _sprint_key(sprint)
    start = f"CAST('{sprint['start_date']}' AS DATE)" if sprint.get("start_date") else "NULL"
    finish = f"CAST('{sprint['finish_date']}' AS DATE)" if sprint.get("finish_date") else "NULL"
    total = sprint.get("total_days") or "NULL"
    iteration_path = sprint.get("iteration_path", "").replace("'", "''")
    sprint_name = sprint.get("name", "").replace("'", "''")

    sql = f"""
    MERGE INTO {t} AS tgt
    USING (SELECT
        '{sk}'                  AS sprint_key,
        '{sprint_name}'         AS sprint_name,
        '{iteration_path}'      AS iteration_path,
        {start}                 AS start_date,
        {finish}                AS finish_date,
        {total}                 AS total_days,
        '{Config.AZURE_PROJECT}' AS azure_project,
        '{Config.AZURE_ORG}'    AS azure_org
    ) AS src ON tgt.sprint_key = src.sprint_key
    WHEN MATCHED THEN UPDATE SET
        sprint_name    = src.sprint_name,
        start_date     = src.start_date,
        finish_date    = src.finish_date,
        total_days     = src.total_days
    WHEN NOT MATCHED THEN INSERT *
    """
    execute(conn, sql)
    logger.debug("DimSprint upserted: %s", sk)


# ---------------------------------------------------------------------------
# DimMember
# ---------------------------------------------------------------------------

def _collect_members(tasks: list[dict]) -> dict[str, dict]:
    """Return dict of member_key -> member info, deduplicated across all tasks."""
    members = {}
    for task in tasks:
        assignee = task.get("assignee", {})
        email = assignee.get("email", "")
        mk = _member_key(email)
        if mk not in members:
            members[mk] = {
                "display_name": assignee.get("display_name", "Unassigned"),
                "email": email or "",
                "azure_unique_name": email or "",
            }
        # also collect comment authors
        for c in task.get("comments", []):
            cemail = c.get("author_email", "")
            cmk = _member_key(cemail)
            if cmk not in members:
                members[cmk] = {
                    "display_name": c.get("author_display_name", ""),
                    "email": cemail,
                    "azure_unique_name": cemail,
                }
    return members


def _upsert_dim_members(conn, members: dict) -> None:
    t = Config.db_table("DimMember")
    for mk, m in members.items():
        dn = m["display_name"].replace("'", "''")
        em = m["email"].replace("'", "''")
        an = m["azure_unique_name"].replace("'", "''")
        sql = f"""
        MERGE INTO {t} AS tgt
        USING (SELECT
            '{mk}'  AS member_key,
            '{dn}'  AS display_name,
            '{em}'  AS email,
            '{an}'  AS azure_unique_name
        ) AS src ON tgt.member_key = src.member_key
        WHEN MATCHED THEN UPDATE SET
            display_name      = src.display_name,
            email             = src.email,
            azure_unique_name = src.azure_unique_name
        WHEN NOT MATCHED THEN INSERT *
        """
        execute(conn, sql)
    logger.debug("DimMember upserted: %d members", len(members))


# ---------------------------------------------------------------------------
# DimTask
# ---------------------------------------------------------------------------

def _upsert_dim_tasks(conn, tasks: list[dict], sprint: dict, today: date) -> None:
    t = Config.db_table("DimTask")
    sk = _sprint_key(sprint)
    for task in tasks:
        tk = _task_key(task["id"])
        title = task.get("title", "").replace("'", "''")
        wtype = task.get("work_item_type", "").replace("'", "''")
        ak = _member_key(task.get("assignee", {}).get("email", ""))

        sql = f"""
        MERGE INTO {t} AS tgt
        USING (SELECT
            {tk}                    AS task_key,
            {task['id']}            AS work_item_id,
            '{title}'               AS title,
            '{wtype}'               AS work_item_type,
            '{sk}'                  AS sprint_key,
            '{ak}'                  AS assignee_key,
            '{Config.AZURE_PROJECT}' AS azure_project,
            CAST('{today.isoformat()}' AS DATE) AS first_seen_date
        ) AS src ON tgt.task_key = src.task_key
        WHEN MATCHED THEN UPDATE SET
            title        = src.title,
            assignee_key = src.assignee_key
        WHEN NOT MATCHED THEN INSERT *
        """
        execute(conn, sql)
    logger.debug("DimTask upserted: %d tasks", len(tasks))


# ---------------------------------------------------------------------------
# FactTaskSnapshot
# ---------------------------------------------------------------------------

def _insert_fact_task_snapshots(
    conn, tasks: list[dict], sprint: dict, today: date, created_at: datetime
) -> None:
    t = Config.db_table("FactTaskSnapshot")
    sk = _sprint_key(sprint)
    ts = created_at.strftime("%Y-%m-%d %H:%M:%S")

    for task in tasks:
        analysis = task.get("analysis", {})
        risk = task.get("risk", {})
        signals = risk.get("signals", {})
        nudge = task.get("nudge") or {}
        tk = _task_key(task["id"])
        ak = _member_key(task.get("assignee", {}).get("email", ""))

        nudge_tone = (nudge.get("tone") or "").replace("'", "''")
        nudge_msg = (nudge.get("message") or "").replace("'", "''")

        remaining = task.get("remaining_hours")
        remaining_sql = str(remaining) if remaining is not None else "NULL"
        original = task.get("original_estimate")
        original_sql = str(original) if original is not None else "NULL"

        sql = f"""
        MERGE INTO {t} AS tgt
        USING (SELECT
            CAST('{today.isoformat()}' AS DATE) AS snapshot_date,
            {tk}                        AS task_key,
            {task['id']}                AS work_item_id,
            '{sk}'                      AS sprint_key,
            '{ak}'                      AS assignee_key,
            '{task.get('state','').replace("'","''")}' AS current_state,
            {remaining_sql}             AS remaining_hours,
            {original_sql}              AS original_estimate,
            {task.get('days_since_state_change', 0)} AS days_since_state_change,
            {task.get('days_since_last_update', 0)}  AS days_since_last_update,
            {risk.get('risk_score', 0)} AS risk_score,
            '{risk.get('risk_label','').replace("'","''")}' AS risk_label,
            {analysis.get('quality_score', 0)} AS comment_quality_score,
            '{analysis.get('quality_label','').replace("'","''")}' AS comment_quality_label,
            {str(analysis.get('has_comment_today', False)).upper()} AS has_comment_today,
            {str(analysis.get('copy_paste_detected', False)).upper()} AS copy_paste_detected,
            {str(analysis.get('blocker_detected', False)).upper()} AS blocker_detected,
            {str(signals.get('hours_stale', False)).upper()} AS hours_stale,
            {str(task.get('needs_attention', False)).upper()} AS needs_attention,
            {signals.get('days_remaining_in_sprint', 0)} AS sprint_days_remaining,
            '{nudge_tone}' AS nudge_tone,
            '{nudge_msg}'  AS nudge_message,
            {str(analysis.get('has_comment_today', False)).upper()} AS eod_compliant,
            CAST('{ts}' AS TIMESTAMP) AS created_at
        ) AS src
        ON tgt.snapshot_date = src.snapshot_date
        AND tgt.work_item_id  = src.work_item_id
        WHEN MATCHED THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
        """
        execute(conn, sql)

    logger.info("FactTaskSnapshot: %d rows written for %s", len(tasks), today)


# ---------------------------------------------------------------------------
# FactComments
# ---------------------------------------------------------------------------

def _insert_fact_comments(
    conn, tasks: list[dict], sprint: dict, created_at: datetime
) -> None:
    t = Config.db_table("FactComments")
    sk = _sprint_key(sprint)
    ts = created_at.strftime("%Y-%m-%d %H:%M:%S")

    for task in tasks:
        analysis = task.get("analysis", {})
        tk = _task_key(task["id"])

        for c in task.get("comments", []):
            cid = c.get("comment_id")
            if not cid:
                continue

            text = (c.get("text") or "").replace("'", "''")
            author_key = _member_key(c.get("author_email", ""))
            comment_ts = c.get("created_date", ts).replace("T", " ").replace("Z", "")[:19]
            comment_date = comment_ts[:10]

            # Quality/analysis fields only for today's comment on this task.
            # Past-day comments don't have per-comment analysis — store defaults.
            is_today = c.get("is_today", False)
            quality_score = analysis.get("quality_score", 0) if is_today else 0
            quality_label = (analysis.get("quality_label", "unknown") if is_today else "unknown").replace("'", "''")
            copy_paste = str(analysis.get("copy_paste_detected", False) if is_today else False).upper()
            blocker = str(analysis.get("blocker_detected", False) if is_today else False).upper()
            sentiment = (analysis.get("sentiment", "") if is_today else "").replace("'", "''")
            followup = (analysis.get("suggested_followup", "") if is_today else "").replace("'", "''")

            sql = f"""
            MERGE INTO {t} AS tgt
            USING (SELECT
                {cid}                           AS comment_id,
                {task['id']}                    AS work_item_id,
                {tk}                            AS task_key,
                '{sk}'                          AS sprint_key,
                '{author_key}'                  AS author_key,
                CAST('{comment_date}' AS DATE)  AS comment_date,
                CAST('{comment_ts}' AS TIMESTAMP) AS comment_timestamp,
                '{text}'                        AS comment_text,
                {quality_score}                 AS quality_score,
                '{quality_label}'               AS quality_label,
                {copy_paste}                    AS copy_paste_flag,
                {blocker}                       AS blocker_detected,
                '{sentiment}'                   AS sentiment,
                '{followup}'                    AS ai_suggested_followup,
                {str(is_today).upper()}         AS is_today,
                CAST('{ts}' AS TIMESTAMP)       AS created_at
            ) AS src ON tgt.comment_id = src.comment_id
            WHEN MATCHED THEN UPDATE SET
                quality_score        = src.quality_score,
                quality_label        = src.quality_label,
                copy_paste_flag      = src.copy_paste_flag,
                blocker_detected     = src.blocker_detected,
                sentiment            = src.sentiment,
                ai_suggested_followup = src.ai_suggested_followup
            WHEN NOT MATCHED THEN INSERT *
            """
            execute(conn, sql)

    logger.info("FactComments: written for %d tasks", len(tasks))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def save_report(report: dict) -> None:
    """
    Write the enriched pipeline report to all six Databricks tables.
    Called just before email delivery in main.py.
    """
    sprint = report["sprint"]
    tasks = report["tasks"]
    created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    today = created_at.date()

    logger.info(
        "Writing %d tasks to Databricks (%s.%s)...",
        len(tasks),
        Config.DATABRICKS_CATALOG,
        Config.DATABRICKS_SCHEMA,
    )

    members = _collect_members(tasks)

    with get_connection() as conn:
        _upsert_dim_date(conn, today)
        _upsert_dim_sprint(conn, sprint)
        _upsert_dim_members(conn, members)
        _upsert_dim_tasks(conn, tasks, sprint, today)
        _insert_fact_task_snapshots(conn, tasks, sprint, today, created_at)
        _insert_fact_comments(conn, tasks, sprint, created_at)

    logger.info("Databricks write complete.")