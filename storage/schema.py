"""
DDL definitions for all six tables in the EOD Task Monitor data model.

Tables:
    DimDate           -- calendar dimension
    DimSprint         -- sprint dimension
    DimMember         -- team member dimension
    DimTask           -- task work item dimension
    FactTaskSnapshot  -- one row per Task x Day
    FactComments      -- one row per comment

All tables live in DATABRICKS_CATALOG.DATABRICKS_SCHEMA.
Run init_schema() once at startup; it is idempotent (CREATE TABLE IF NOT EXISTS).
"""

import logging
from config import Config
from storage.databricks_client import get_connection, execute

logger = logging.getLogger(__name__)

_DDL_DIM_DATE = """
CREATE TABLE IF NOT EXISTS {t} (
    date_key        DATE        NOT NULL,
    year            INT         NOT NULL,
    quarter         INT         NOT NULL,
    month           INT         NOT NULL,
    week_of_year    INT         NOT NULL,
    day_of_week     INT         NOT NULL,
    day_name        STRING      NOT NULL,
    is_weekend      BOOLEAN     NOT NULL,
    is_working_day  BOOLEAN     NOT NULL
)
USING DELTA
COMMENT 'Calendar dimension — one row per calendar day'
"""

_DDL_DIM_SPRINT = """
CREATE TABLE IF NOT EXISTS {t} (
    sprint_key          STRING      NOT NULL,
    sprint_name         STRING      NOT NULL,
    iteration_path      STRING      NOT NULL,
    start_date          DATE,
    finish_date         DATE,
    total_days          INT,
    azure_project       STRING      NOT NULL,
    azure_org           STRING      NOT NULL
)
USING DELTA
COMMENT 'Sprint / iteration dimension — one row per Azure DevOps iteration'
"""

_DDL_DIM_MEMBER = """
CREATE TABLE IF NOT EXISTS {t} (
    member_key          STRING      NOT NULL,
    display_name        STRING      NOT NULL,
    email               STRING      NOT NULL,
    azure_unique_name   STRING      NOT NULL
)
USING DELTA
COMMENT 'Team member dimension — keyed on Azure DevOps uniqueName (email)'
"""

_DDL_DIM_TASK = """
CREATE TABLE IF NOT EXISTS {t} (
    task_key            BIGINT      NOT NULL,
    work_item_id        INT         NOT NULL,
    title               STRING      NOT NULL,
    work_item_type      STRING      NOT NULL,
    sprint_key          STRING      NOT NULL,
    assignee_key        STRING,
    azure_project       STRING      NOT NULL,
    first_seen_date     DATE        NOT NULL
)
USING DELTA
COMMENT 'Task work item dimension — one row per unique Task ID'
"""

_DDL_FACT_TASK_SNAPSHOT = """
CREATE TABLE IF NOT EXISTS {t} (
    snapshot_date           DATE        NOT NULL,
    task_key                BIGINT      NOT NULL,
    work_item_id            INT         NOT NULL,
    sprint_key              STRING      NOT NULL,
    assignee_key            STRING,
    current_state           STRING      NOT NULL,
    remaining_hours         DOUBLE,
    original_estimate       DOUBLE,
    days_since_state_change INT         NOT NULL,
    days_since_last_update  INT         NOT NULL,
    risk_score              INT         NOT NULL,
    risk_label              STRING      NOT NULL,
    comment_quality_score   INT         NOT NULL,
    comment_quality_label   STRING      NOT NULL,
    has_comment_today       BOOLEAN     NOT NULL,
    copy_paste_detected     BOOLEAN     NOT NULL,
    blocker_detected        BOOLEAN     NOT NULL,
    hours_stale             BOOLEAN     NOT NULL,
    needs_attention         BOOLEAN     NOT NULL,
    sprint_days_remaining   INT         NOT NULL,
    nudge_tone              STRING,
    nudge_message           STRING,
    eod_compliant           BOOLEAN     NOT NULL,
    created_at              TIMESTAMP   NOT NULL
)
USING DELTA
COMMENT 'Fact: one row per Task x Day'
TBLPROPERTIES ('delta.autoOptimize.optimizeWrite' = 'true')
"""

_DDL_FACT_COMMENTS = """
CREATE TABLE IF NOT EXISTS {t} (
    comment_id              BIGINT      NOT NULL,
    work_item_id            INT         NOT NULL,
    task_key                BIGINT      NOT NULL,
    sprint_key              STRING      NOT NULL,
    author_key              STRING      NOT NULL,
    comment_date            DATE        NOT NULL,
    comment_timestamp       TIMESTAMP   NOT NULL,
    comment_text            STRING      NOT NULL,
    quality_score           INT         NOT NULL,
    quality_label           STRING      NOT NULL,
    copy_paste_flag         BOOLEAN     NOT NULL,
    blocker_detected        BOOLEAN     NOT NULL,
    sentiment               STRING,
    ai_suggested_followup   STRING,
    is_today                BOOLEAN     NOT NULL,
    created_at              TIMESTAMP   NOT NULL
)
USING DELTA
COMMENT 'Fact: one row per comment within the lookback window'
TBLPROPERTIES ('delta.autoOptimize.optimizeWrite' = 'true')
"""


def init_schema() -> None:
    """
    Create the catalog schema and all six tables if they do not yet exist.
    Safe to call on every pipeline run — idempotent.
    """
    catalog = Config.DATABRICKS_CATALOG
    schema = Config.DATABRICKS_SCHEMA

    logger.info("Initialising Databricks schema '%s.%s'...", catalog, schema)

    tables = [
        ("DimDate",          _DDL_DIM_DATE),
        ("DimSprint",        _DDL_DIM_SPRINT),
        ("DimMember",        _DDL_DIM_MEMBER),
        ("DimTask",          _DDL_DIM_TASK),
        ("FactTaskSnapshot", _DDL_FACT_TASK_SNAPSHOT),
        ("FactComments",     _DDL_FACT_COMMENTS),
    ]

    with get_connection() as conn:
        execute(conn, f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}")

        for name, ddl in tables:
            fq = Config.db_table(name)
            execute(conn, ddl.format(t=fq))
            logger.info("  %s — ready", fq)

    logger.info("Schema initialisation complete.")