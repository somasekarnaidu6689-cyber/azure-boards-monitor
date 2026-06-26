"""
storage/schema.py

Tables:
  - TaskDailySnapshot: one row per task per day (unchanged design rationale
    below), now also carrying azure_team and per-channel delivery status.
  - RunMetrics: one row per pipeline run, for observability/alerting.
  - CommentHistory: managed by storage/comment_history.py, used to widen
    the copy-paste detection corpus beyond the current lookback window.

Design rationale (TaskDailySnapshot):
- Replaces 6-table star schema with one wide append table
- Power BI connects directly to this table for all visuals
- Append-only; idempotent via DELETE + INSERT on (snapshot_date, work_item_id)
"""

import logging
from config import Config
from storage.databricks_client import get_connection, execute
from storage.comment_history import init_comment_history_schema

logger = logging.getLogger(__name__)

_DDL_SNAPSHOT = """
CREATE TABLE IF NOT EXISTS {t} (
    -- Identity
    snapshot_date               DATE        NOT NULL,
    snapshot_timestamp          TIMESTAMP   NOT NULL,
    azure_org                   STRING      NOT NULL,
    azure_project               STRING      NOT NULL,
    azure_team                  STRING,

    -- Sprint
    sprint_name                 STRING      NOT NULL,
    iteration_path              STRING      NOT NULL,
    sprint_start_date           DATE,
    sprint_finish_date          DATE,
    sprint_days_remaining       INT         NOT NULL,

    -- Task
    work_item_id                INT         NOT NULL,
    task_title                  STRING      NOT NULL,
    work_item_type               STRING      NOT NULL,
    task_state                  STRING      NOT NULL,
    assignee_name               STRING,
    assignee_email               STRING,
    remaining_hours              DOUBLE,
    original_estimate            DOUBLE,
    days_since_state_change      INT         NOT NULL,
    days_since_last_update       INT         NOT NULL,

    -- Today's comment
    has_comment_today           BOOLEAN     NOT NULL,
    today_comment_text           STRING,
    today_comment_author_name    STRING,
    today_comment_author_email   STRING,
    today_comment_timestamp      TIMESTAMP,
    today_comment_image_urls     STRING,

    -- Comment history (last N days, pipe-separated)
    recent_comment_count        INT         NOT NULL,
    recent_comment_authors       STRING,

    -- AI analysis
    comment_quality_score       INT         NOT NULL,
    comment_quality_label        STRING      NOT NULL,
    copy_paste_detected         BOOLEAN     NOT NULL,
    blocker_detected             BOOLEAN     NOT NULL,
    hours_stale                  BOOLEAN     NOT NULL,
    sentiment                   STRING,
    ai_suggested_followup        STRING,

    -- Risk
    risk_score                  INT         NOT NULL,
    risk_label                  STRING      NOT NULL,
    needs_attention              BOOLEAN     NOT NULL,
    eod_compliant                BOOLEAN     NOT NULL,

    -- Nudge
    nudge_tone                  STRING,
    nudge_message                STRING,

    -- Delivery status (Gap: "Email Delivery" / "Teams Integration" —
    -- per-task send outcome, populated after mailer/teams calls return)
    report_email_status          STRING,    -- sent | skipped | failed
    individual_email_status      STRING,    -- sent | skipped | failed
    teams_notification_status    STRING     -- sent | skipped | failed | disabled
)
USING DELTA
COMMENT 'EOD Task Monitor — daily snapshot, one row per task per day'
TBLPROPERTIES (
    'delta.autoOptimize.optimizeWrite' = 'true',
    'delta.autoOptimize.autoCompact'   = 'true'
)
"""

# Gap: "Observability" (High) — run-level metrics, one row per pipeline run.
# Power BI / Azure Monitor can alert off `status` and `finished_at IS NULL`
# rows that are older than RUN_DEADLINE_MINUTES (see main.py / utils/metrics.py).
_DDL_RUN_METRICS = """
CREATE TABLE IF NOT EXISTS {t} (
    run_id                    STRING     NOT NULL,
    run_date                  DATE       NOT NULL,
    started_at                TIMESTAMP  NOT NULL,
    finished_at                TIMESTAMP,
    duration_seconds           DOUBLE,
    azure_team                 STRING,
    status                     STRING     NOT NULL,  -- success | partial_failure | failed
    tasks_processed             INT,
    groq_calls_made             INT,
    groq_calls_failed           INT,
    emails_sent                 INT,
    emails_failed               INT,
    teams_notifications_sent    INT,
    error_message               STRING
)
USING DELTA
COMMENT 'EOD Task Monitor — one row per pipeline run, for alerting and health dashboards'
"""


def init_schema() -> None:
    """
    Create schema and all required tables if they do not exist.
    Idempotent — safe to call on every run.
    """
    catalog = Config.DATABRICKS_CATALOG
    schema  = Config.DATABRICKS_SCHEMA
    fq         = Config.db_table("TaskDailySnapshot")
    fq_metrics = Config.db_table("RunMetrics")

    logger.info("Initialising Databricks schema '%s.%s'...", catalog, schema)

    with get_connection() as conn:
        execute(conn, f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}")
        execute(conn, _DDL_SNAPSHOT.format(t=fq))
        logger.info("  %s — ready", fq)
        execute(conn, _DDL_RUN_METRICS.format(t=fq_metrics))
        logger.info("  %s — ready", fq_metrics)

    init_comment_history_schema()
    logger.info("  %s — ready", Config.db_table("CommentHistory"))

    logger.info("Schema initialisation complete.")
