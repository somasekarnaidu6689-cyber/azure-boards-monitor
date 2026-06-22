"""
storage/schema.py

Single flat snapshot table: TaskDailySnapshot
One row per task per day, capturing all useful fields including
comment text, AI analysis, risk score, and image URLs from comments.

Design rationale:
- Replaces 6-table star schema with one wide append table
- Power BI connects directly to this table for all visuals
- Append-only; idempotent via DELETE + INSERT on (snapshot_date, work_item_id)
"""

import logging
from config import Config
from storage.databricks_client import get_connection, execute

logger = logging.getLogger(__name__)

_DDL_SNAPSHOT = """
CREATE TABLE IF NOT EXISTS {t} (
    -- Identity
    snapshot_date               DATE        NOT NULL,
    snapshot_timestamp          TIMESTAMP   NOT NULL,
    azure_org                   STRING      NOT NULL,
    azure_project               STRING      NOT NULL,

    -- Sprint
    sprint_name                 STRING      NOT NULL,
    iteration_path              STRING      NOT NULL,
    sprint_start_date           DATE,
    sprint_finish_date          DATE,
    sprint_days_remaining       INT         NOT NULL,

    -- Task
    work_item_id                INT         NOT NULL,
    task_title                  STRING      NOT NULL,
    work_item_type              STRING      NOT NULL,
    task_state                  STRING      NOT NULL,
    assignee_name               STRING,
    assignee_email              STRING,
    remaining_hours             DOUBLE,
    original_estimate           DOUBLE,
    days_since_state_change     INT         NOT NULL,
    days_since_last_update      INT         NOT NULL,

    -- Today's comment
    has_comment_today           BOOLEAN     NOT NULL,
    today_comment_text          STRING,
    today_comment_author_name   STRING,
    today_comment_author_email  STRING,
    today_comment_timestamp     TIMESTAMP,
    today_comment_image_urls    STRING,

    -- Comment history (last N days, pipe-separated)
    recent_comment_count        INT         NOT NULL,
    recent_comment_authors      STRING,

    -- AI analysis
    comment_quality_score       INT         NOT NULL,
    comment_quality_label       STRING      NOT NULL,
    copy_paste_detected         BOOLEAN     NOT NULL,
    blocker_detected            BOOLEAN     NOT NULL,
    hours_stale                 BOOLEAN     NOT NULL,
    sentiment                   STRING,
    ai_suggested_followup       STRING,

    -- Risk
    risk_score                  INT         NOT NULL,
    risk_label                  STRING      NOT NULL,
    needs_attention             BOOLEAN     NOT NULL,
    eod_compliant               BOOLEAN     NOT NULL,

    -- Nudge
    nudge_tone                  STRING,
    nudge_message               STRING
)
USING DELTA
COMMENT 'EOD Task Monitor — daily snapshot, one row per task per day'
TBLPROPERTIES (
    'delta.autoOptimize.optimizeWrite' = 'true',
    'delta.autoOptimize.autoCompact'   = 'true'
)
"""


def init_schema() -> None:
    """
    Create schema and the single snapshot table if they do not exist.
    Idempotent — safe to call on every run.
    """
    catalog = Config.DATABRICKS_CATALOG
    schema  = Config.DATABRICKS_SCHEMA
    fq      = Config.db_table("TaskDailySnapshot")

    logger.info("Initialising Databricks schema '%s.%s'...", catalog, schema)

    with get_connection() as conn:
        execute(conn, f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}")
        execute(conn, _DDL_SNAPSHOT.format(t=fq))
        logger.info("  %s — ready", fq)

    logger.info("Schema initialisation complete.")