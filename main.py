"""
EOD Task Monitor - main entry point.

Run modes:
  python main.py           -- run the pipeline once immediately
  python main.py --schedule -- run on schedule (RUN_HOUR:RUN_MINUTE daily)
"""

import sys
import logging
import schedule
import time

from config import Config
from utils.logger import setup_logging
from fetcher.board_fetcher import fetch_active_sprint_tasks
from agents.pipeline import run_pipeline
from mailer.sender import send_report, send_individual_task_emails

setup_logging("INFO")
logger = logging.getLogger(__name__)


def run_once() -> None:
    logger.info("=== EOD Task Monitor starting ===")

    logger.info("Step 1 / 3 — Fetching active sprint board data from Azure DevOps...")
    board_data = fetch_active_sprint_tasks()

    task_count = len(board_data.get("tasks", []))
    sprint_name = board_data["sprint"].get("name", "unknown")
    logger.info(
        "Fetched %d task(s) from sprint '%s'", task_count, sprint_name
    )

    if task_count == 0:
        logger.warning(
            "No work items of type %s found among children of %s items in the "
            "active sprint ('%s'). Check that: (1) PARENT_WORK_ITEM_TYPES in "
            "config.py matches the parent items in this sprint, (2) those "
            "parent items have child work items linked via "
            "System.LinkTypes.Hierarchy-Forward, and (3) ALLOWED_WORK_ITEM_TYPES "
            "matches the type of those child items. Exiting.",
            Config.ALLOWED_WORK_ITEM_TYPES,
            Config.PARENT_WORK_ITEM_TYPES,
            sprint_name,
        )
        return

    logger.info("Step 2 / 3 — Running agent pipeline (comment analysis -> risk scoring -> nudge writing)...")
    report = run_pipeline(board_data)

    summary = report["summary"]
    logger.info(
        "Pipeline complete. Total: %d | Flagged: %d | Healthy: %d | Blockers: %d",
        summary["total_tasks"],
        summary["flagged_tasks"],
        summary["healthy_tasks"],
        summary["tasks_with_blockers"],
    )

    logger.info("Step 3 / 3 — Sending email report(s)...")
    send_report(report)
    send_individual_task_emails(report)

    logger.info("=== EOD Task Monitor finished ===")


def main() -> None:
    try:
        Config.validate()
    except EnvironmentError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    if "--schedule" in sys.argv:
        run_time = f"{Config.RUN_HOUR:02d}:{Config.RUN_MINUTE:02d}"
        logger.info("Scheduler mode. Pipeline will run daily at %s UTC.", run_time)
        schedule.every().day.at(run_time).do(run_once)
        while True:
            schedule.run_pending()
            time.sleep(30)
    else:
        run_once()


if __name__ == "__main__":
    main()