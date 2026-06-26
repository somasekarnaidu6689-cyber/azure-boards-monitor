"""
EOD Task Monitor - main entry point.

Run modes:
  python main.py             -- run the pipeline once immediately, for every
                                 configured team (see Config.teams())
  python main.py --schedule  -- run on schedule (RUN_HOUR:RUN_MINUTE daily)
                                 LOCAL/FALLBACK MODE ONLY — see README
                                 "Scheduling & Deployment" for why this
                                 should not be how production runs.
  python main.py --force     -- bypass the working-day/holiday skip (useful
                                 for manual backfills/reruns)
"""

import sys
import logging
import schedule
import time
from datetime import datetime, date, timezone

from config import Config
from utils.logger import setup_logging
from fetcher.azure_client import validate_auth
from fetcher.board_fetcher import fetch_active_sprint_tasks
from agents.pipeline import run_pipeline
from mailer.sender import send_report, send_individual_task_emails
from mailer.teams import send_teams_report, send_teams_alert, teams_enabled
from storage.schema import init_schema
from storage.writer import save_report, update_delivery_status
from storage.comment_history import save_today_comments
from storage.metrics import RunMetrics, write_run_metrics

setup_logging("INFO")
logger = logging.getLogger(__name__)


def _is_working_day(today: date) -> bool:
    """
    Gap addressed: "Scheduling & Deployment" (High) — the original
    scheduler ran 7 days a week with no concept of weekends or holidays,
    so it sent EOD reports (and nudges) on days nobody was working.
    """
    if Config.SKIP_WEEKENDS and today.weekday() >= 5:  # 5=Sat, 6=Sun
        logger.info("Skipping run — %s is a weekend.", today.isoformat())
        return False

    if today.isoformat() in Config.HOLIDAYS:
        logger.info("Skipping run — %s is in the configured HOLIDAYS list.", today.isoformat())
        return False

    if Config.HOLIDAY_COUNTRY:
        try:
            import holidays as holidays_pkg
            country_holidays = holidays_pkg.country_holidays(Config.HOLIDAY_COUNTRY)
            if today in country_holidays:
                logger.info(
                    "Skipping run — %s is a %s public holiday (%s).",
                    today.isoformat(), Config.HOLIDAY_COUNTRY, country_holidays.get(today),
                )
                return False
        except Exception as exc:
            logger.warning("Holiday lookup for country '%s' failed (continuing anyway): %s",
                            Config.HOLIDAY_COUNTRY, exc)

    return True


def run_once_for_team(team: dict, metrics: RunMetrics) -> None:
    """Run the full pipeline for a single team's Azure DevOps project."""
    # Apply this team's overrides to Config for the duration of the run.
    # (Config attributes are simple class attributes — safe to patch
    # sequentially since teams run one at a time, not concurrently.)
    Config.AZURE_ORG = team["azure_org"]
    Config.AZURE_PROJECT = team["azure_project"]
    Config.AZURE_TEAM = team["azure_team"]
    Config.AZURE_BASE_URL = f"https://dev.azure.com/{Config.AZURE_ORG}/{Config.AZURE_PROJECT}/_apis"
    metrics.azure_team = team["name"]

    logger.info("=== EOD Task Monitor starting for team '%s' ===", team["name"])

    logger.info("Step 0 / 4 — Validating Azure DevOps authentication...")
    validate_auth()

    logger.info("Step 1 / 4 — Fetching active sprint board data from Azure DevOps...")
    board_data = fetch_active_sprint_tasks()

    task_count = len(board_data.get("tasks", []))
    sprint_name = board_data["sprint"].get("name", "unknown")
    logger.info("Fetched %d task(s) from sprint '%s'", task_count, sprint_name)

    if task_count == 0:
        logger.warning(
            "No work items of type %s found among children of %s items in the "
            "active sprint ('%s'). Check that: (1) PARENT_WORK_ITEM_TYPES in "
            "config.py matches the parent items in this sprint, (2) those "
            "parent items have child work items linked via "
            "System.LinkTypes.Hierarchy-Forward, and (3) ALLOWED_WORK_ITEM_TYPES "
            "matches the type of those child items. Exiting for this team.",
            Config.ALLOWED_WORK_ITEM_TYPES, Config.PARENT_WORK_ITEM_TYPES, sprint_name,
        )
        return

    logger.info("Step 2 / 4 — Running agent pipeline (comment analysis -> risk scoring -> nudge writing)...")
    report = run_pipeline(board_data, metrics=metrics)

    summary = report["summary"]
    logger.info(
        "Pipeline complete. Total: %d | Flagged: %d | Healthy: %d | Blockers: %d",
        summary["total_tasks"], summary["flagged_tasks"], summary["healthy_tasks"],
        summary["tasks_with_blockers"],
    )

    logger.info("Step 3 / 4 — Saving snapshot to Databricks...")
    save_report(report, azure_team=team["name"])
    save_today_comments(report["tasks"], date.today())

    logger.info("Step 4 / 4 — Sending email report(s) and Teams notification...")
    report_status = send_report(report)
    individual_statuses = send_individual_task_emails(report)
    teams_status = send_teams_report(report) if teams_enabled() else "disabled"

    snapshot_date = date.today()
    all_ids = [t["id"] for t in report["tasks"]]
    if all_ids:
        update_delivery_status(all_ids, snapshot_date, "report_email_status", report_status)
    for status_value in set(individual_statuses.values()):
        ids = [wid for wid, s in individual_statuses.items() if s == status_value]
        update_delivery_status(ids, snapshot_date, "individual_email_status", status_value)
    if all_ids and teams_status != "disabled":
        update_delivery_status(all_ids, snapshot_date, "teams_notification_status", teams_status)

    metrics.emails_sent += sum(1 for s in individual_statuses.values() if s == "sent") + (1 if report_status == "sent" else 0)
    metrics.emails_failed += sum(1 for s in individual_statuses.values() if s == "failed") + (1 if report_status == "failed" else 0)
    if teams_status == "sent":
        metrics.teams_notifications_sent += 1

    if report_status == "failed":
        logger.error("Full report email failed to send for team '%s'.", team["name"])

    logger.info("=== EOD Task Monitor finished for team '%s' ===", team["name"])


def run_once(force: bool = False) -> None:
    today = datetime.now(timezone.utc).date()
    if not force and not _is_working_day(today):
        return

    init_schema()

    overall_status = "success"
    error_messages = []

    for team in Config.teams():
        metrics = RunMetrics(azure_team=team["name"])
        try:
            run_once_for_team(team, metrics)
            metrics.mark_success()
        except Exception as exc:
            logger.exception("Run failed for team '%s'", team["name"])
            metrics.mark_failed(str(exc))
            overall_status = "partial_failure"
            error_messages.append(f"{team['name']}: {exc}")
            if Config.METRICS_ALERT_ON_FAILURE:
                send_teams_alert(
                    f"EOD Task Monitor run FAILED for team '{team['name']}': {exc}"
                )
        finally:
            metrics.log_summary()
            write_run_metrics(metrics)

    if overall_status != "success" and Config.METRICS_ALERT_ON_FAILURE and len(error_messages) > 1:
        send_teams_alert(
            "EOD Task Monitor — multiple teams failed this run:\n" + "\n".join(error_messages)
        )


def main() -> None:
    try:
        Config.validate()
    except EnvironmentError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    force = "--force" in sys.argv

    if "--schedule" in sys.argv:
        run_time = f"{Config.RUN_HOUR:02d}:{Config.RUN_MINUTE:02d}"
        logger.info(
            "Scheduler mode (LOCAL/FALLBACK ONLY — see README 'Scheduling & "
            "Deployment'). Pipeline will run daily at %s UTC.", run_time,
        )
        schedule.every().day.at(run_time).do(run_once, force=force)
        while True:
            schedule.run_pending()
            time.sleep(30)
    else:
        run_once(force=force)


if __name__ == "__main__":
    main()
