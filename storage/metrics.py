"""
storage/metrics.py

Gap addressed: "Observability" (High) — previously the only signal of
pipeline health was structured stdout logging, with no metrics, alerting,
or way to know the pipeline failed without tailing logs manually.

RunMetrics is a small in-memory counter object threaded through main.py
and the pipeline, written to the RunMetrics Databricks table at the end
of every run (success or failure). Azure Monitor / a Databricks dashboard
/ Power BI can alert on:
  - rows where status != 'success'
  - rows where finished_at is NULL and started_at is older than
    RUN_DEADLINE_MINUTES (a run that started but never finished)
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import Config
from storage.databricks_client import get_connection, execute

logger = logging.getLogger(__name__)

_INSERT_SQL = "INSERT INTO {t} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"


@dataclass
class RunMetrics:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    azure_team: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    status: str = "running"  # running | success | partial_failure | failed
    tasks_processed: int = 0
    groq_calls_made: int = 0
    groq_calls_failed: int = 0
    emails_sent: int = 0
    emails_failed: int = 0
    teams_notifications_sent: int = 0
    error_message: str | None = None

    def mark_success(self) -> None:
        self.status = "success"
        self.finished_at = datetime.now(timezone.utc)

    def mark_partial_failure(self, message: str) -> None:
        self.status = "partial_failure"
        self.error_message = message
        self.finished_at = datetime.now(timezone.utc)

    def mark_failed(self, message: str) -> None:
        self.status = "failed"
        self.error_message = message
        self.finished_at = datetime.now(timezone.utc)

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def log_summary(self) -> None:
        logger.info(
            "Run %s [%s] | duration=%.1fs | tasks=%d | groq_ok=%d groq_failed=%d | "
            "emails_sent=%d emails_failed=%d | teams_sent=%d%s",
            self.run_id, self.status,
            self.duration_seconds or 0.0,
            self.tasks_processed,
            self.groq_calls_made, self.groq_calls_failed,
            self.emails_sent, self.emails_failed,
            self.teams_notifications_sent,
            f" | error={self.error_message}" if self.error_message else "",
        )


def write_run_metrics(metrics: RunMetrics) -> None:
    """Best-effort write of a completed (or failed) run's metrics row."""
    fq = Config.db_table("RunMetrics")
    row = (
        metrics.run_id,
        metrics.started_at.date(),
        metrics.started_at.replace(tzinfo=None),
        metrics.finished_at.replace(tzinfo=None) if metrics.finished_at else None,
        metrics.duration_seconds,
        metrics.azure_team,
        metrics.status,
        metrics.tasks_processed,
        metrics.groq_calls_made,
        metrics.groq_calls_failed,
        metrics.emails_sent,
        metrics.emails_failed,
        metrics.teams_notifications_sent,
        metrics.error_message,
    )

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_INSERT_SQL.format(t=fq), row)
        logger.info("RunMetrics row written for run %s.", metrics.run_id)
    except Exception as exc:
        # Never let metrics-writing failure mask the actual run outcome.
        logger.warning("Failed to write RunMetrics row (non-fatal): %s", exc)
