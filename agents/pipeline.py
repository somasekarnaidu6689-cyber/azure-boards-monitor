import logging
from agents.comment_analyst import analyse_task_comment
from agents.risk_scorer import calculate_risk_score
from agents.nudge_writer import write_nudge_message
from config import Config

logger = logging.getLogger(__name__)


def run_pipeline(board_data: dict) -> dict:
    """
    Run all agents (comment analyst -> risk scorer -> nudge writer) over every
    task in the board data payload.

    Returns enriched payload:
    {
        "sprint": ...,
        "fetched_at": ...,
        "summary": {
            "total_tasks": int,
            "flagged_tasks": int,
            "healthy_tasks": int,
            "tasks_missing_comment": int,
            "tasks_with_blockers": int,
            "tasks_at_risk_or_critical": int,
        },
        "tasks": [
            {
                ...original task fields...,
                "analysis": { quality_score, quality_label, blocker_detected, ... },
                "risk": { risk_score, risk_label, signals },
                "nudge": { message, tone } | None,
                "needs_attention": bool,
            }
        ]
    }
    """
    sprint = board_data["sprint"]
    days_remaining = sprint.get("days_remaining", 0)
    sprint_dates_configured = sprint.get("dates_configured", True)
    tasks = board_data.get("tasks", [])

    enriched_tasks = []
    total = len(tasks)
    flagged = 0
    missing_comment = 0
    with_blockers = 0
    at_risk_or_critical = 0

    for idx, task in enumerate(tasks):
        logger.info(
            "Processing task %d/%d: [%d] %s",
            idx + 1,
            total,
            task["id"],
            task["title"],
        )

        # Agent 2: Comment analysis
        analysis = analyse_task_comment(task)

        # Agent 3: Risk scoring
        risk = calculate_risk_score(task, analysis, days_remaining, sprint_dates_configured)

        # Determine if this task needs attention (above threshold or blocker)
        needs_attention = (
            risk["risk_score"] >= Config.RISK_FLAG_THRESHOLD
            or analysis["blocker_detected"]
            or not analysis["has_comment_today"]
            or analysis["quality_score"] < 5
        )

        # Agent 4: Nudge message (only for flagged tasks)
        nudge = None
        if needs_attention:
            nudge = write_nudge_message(task, analysis, risk)
            flagged += 1

        if not analysis["has_comment_today"]:
            missing_comment += 1
        if analysis["blocker_detected"]:
            with_blockers += 1
        if risk["risk_label"] in ("At Risk", "Critical"):
            at_risk_or_critical += 1

        enriched_tasks.append(
            {
                **task,
                "analysis": analysis,
                "risk": risk,
                "nudge": nudge,
                "needs_attention": needs_attention,
            }
        )

    # Sort: most critical first
    enriched_tasks.sort(key=lambda t: t["risk"]["risk_score"], reverse=True)

    return {
        "sprint": sprint,
        "fetched_at": board_data["fetched_at"],
        "summary": {
            "total_tasks": total,
            "flagged_tasks": flagged,
            "healthy_tasks": total - flagged,
            "tasks_missing_comment": missing_comment,
            "tasks_with_blockers": with_blockers,
            "tasks_at_risk_or_critical": at_risk_or_critical,
        },
        "tasks": enriched_tasks,
    }