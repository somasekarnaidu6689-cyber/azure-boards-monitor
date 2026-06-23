import logging
from datetime import datetime, timezone, date
from agents.comment_analyst import analyse_task_comment
from agents.risk_scorer import calculate_risk_score
from agents.nudge_writer import write_nudge_message
from storage.databricks_client import fetch_latest_snapshot
from config import Config

logger = logging.getLogger(__name__)


def _active_states() -> set[str]:
    """
    Combined set of states that warrant Groq analysis:
    EMAIL_NOTIFY_STATES union EMAIL_QUALITY_GATE_STATE.
    Tasks in any other state get no API calls.
    """
    notify = {s.strip().lower() for s in Config.EMAIL_NOTIFY_STATES}
    gate   = {Config.EMAIL_QUALITY_GATE_STATE.strip().lower()}
    return notify | gate


_NULL_ANALYSIS = {
    "has_comment_today":   False,
    "quality_score":       0,
    "quality_label":       "skipped",
    "blocker_detected":    False,
    "progress_detectable": False,
    "copy_paste_detected": False,
    "sentiment":           "neutral",
    "suggested_followup":  "",
}

_NULL_RISK = {
    "risk_score": 0,
    "risk_label": "Healthy",
    "signals": {
        "days_since_state_change":  0,
        "days_since_last_update":   0,
        "hours_stale":              False,
        "comment_quality_score":    0,
        "copy_paste_detected":      False,
        "blocker_detected":         False,
        "days_remaining_in_sprint": 0,
    },
}


def run_pipeline(board_data: dict) -> dict:
    """
    Run all agents (comment analyst -> risk scorer -> nudge writer) over
    every task in the board data payload.

    Quality gate memory (EMAIL_QUALITY_GATE_STATE):
      Before the main loop, Databricks is queried once for the most recent
      snapshot date on which each gate-state task had a genuinely good
      comment (eod_compliant=true, not copy-pasted, score >= threshold).

      - If that date was YESTERDAY: the assignee wrote a good comment then
        and does not need to be reminded today even if no comment exists yet.
        Groq is skipped entirely and needs_attention = False.
      - If that date was TWO OR MORE DAYS AGO (or never): analysis runs
        normally — the grace period has expired.

    Groq API calls are only made for tasks whose state is in the combined
    set of EMAIL_NOTIFY_STATES + EMAIL_QUALITY_GATE_STATE.
    """
    sprint = board_data["sprint"]
    days_remaining       = sprint.get("days_remaining", 0)
    sprint_dates_configured = sprint.get("dates_configured", True)
    tasks                = board_data.get("tasks", [])

    active_states      = _active_states()
    quality_gate_state = Config.EMAIL_QUALITY_GATE_STATE.strip().lower()
    good_threshold     = Config.EMAIL_GOOD_QUALITY_THRESHOLD
    today_str          = datetime.now(timezone.utc).date().isoformat()

    # ── One DB round-trip to fetch history for all gate-state tasks ───────
    gate_task_ids = [
        t["id"] for t in tasks
        if t.get("state", "").strip().lower() == quality_gate_state
    ]
    last_snapshot: dict[int, dict] = {}
    if gate_task_ids:
        logger.info(
            "Fetching last good comment history for %d quality-gate task(s)...",
            len(gate_task_ids),
        )
        last_snapshot = fetch_latest_snapshot(gate_task_ids)
        logger.info(
            "History found for %d / %d task(s).",
            len(last_snapshot), len(gate_task_ids),
        )

    enriched_tasks      = []
    total               = len(tasks)
    flagged             = 0
    missing_comment     = 0
    with_blockers       = 0
    at_risk_or_critical = 0
    skipped_state       = 0
    skipped_good_hist   = 0
    last_snapshot = fetch_latest_snapshot(gate_task_ids)

    for idx, task in enumerate(tasks):
        task_state_lower = task.get("state", "").strip().lower()
        task_id          = task["id"]

        logger.info(
            "Processing task %d/%d: [%d] %s (state: %s)",
            idx + 1, total, task_id, task["title"], task.get("state"),
        )

        # ── Skip Groq for states that are not relevant at all ─────────────
        if task_state_lower not in active_states:
            logger.info(
                "Task #%d state '%s' not in active states %s — skipping Groq.",
                task_id, task.get("state"), active_states,
            )
            skipped_state += 1
            enriched_tasks.append({
                **task,
                "analysis":        _NULL_ANALYSIS.copy(),
                "risk":            _NULL_RISK.copy(),
                "nudge":           None,
                "needs_attention": False,
            })
            continue

        # ── Quality gate: suppress if last good comment was yesterday ──────
        
 
        # inside the loop:
        if task_state_lower == quality_gate_state and task_id in last_snapshot:
            rec = last_snapshot[task_id]
            if rec["is_good"]:
                logger.info(
                    "Task #%d latest comment on %s is good (score %d) — suppressing reminder.",
                    task_id, rec["latest_date"], rec["latest_score"],
                )
                skipped_good_hist += 1
                enriched_tasks.append({
                    **task,
                    "analysis": _NULL_ANALYSIS.copy(),
                    "risk": _NULL_RISK.copy(),
                    "nudge": None,
                    "needs_attention": False,
                })
                continue
            else:
                # 2+ days ago — grace period expired, run analysis normally
                logger.info(
                    "Task #%d last good comment was %d day(s) ago (%s, score %d) — "
                    "grace period expired, running analysis.",
                    task_id, rec["latest_date"], rec["latest_score"],
                )

        # ── Agent 2: Comment analysis (Groq) ──────────────────────────────
        analysis = analyse_task_comment(task)

        # ── Agent 3: Risk scoring (local) ─────────────────────────────────
        risk = calculate_risk_score(task, analysis, days_remaining, sprint_dates_configured)

        # ── Quality gate triggered? ───────────────────────────────────────
        quality_gate_triggered = task_state_lower == quality_gate_state and (
            not analysis["has_comment_today"]
            or analysis["quality_score"] < good_threshold
            or analysis["copy_paste_detected"]
        )

        needs_attention = (
            risk["risk_score"] >= Config.RISK_FLAG_THRESHOLD
            or analysis["blocker_detected"]
            or not analysis["has_comment_today"]
            or analysis["quality_score"] < 5
            or analysis["copy_paste_detected"]
            or quality_gate_triggered
        )

        # ── Agent 4: Nudge writing (Groq, flagged tasks only) ─────────────
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

        enriched_tasks.append({
            **task,
            "analysis":        analysis,
            "risk":            risk,
            "nudge":           nudge,
            "needs_attention": needs_attention,
        })

    # Sort: most critical first
    enriched_tasks.sort(key=lambda t: t["risk"]["risk_score"], reverse=True)

    logger.info(
        "Pipeline done. Total: %d | Groq called: %d | "
        "Skipped (state): %d | Skipped (good history): %d",
        total,
        total - skipped_state - skipped_good_hist,
        skipped_state,
        skipped_good_hist,
    )

    return {
        "sprint":     sprint,
        "fetched_at": board_data["fetched_at"],
        "summary": {
            "total_tasks":                total,
            "flagged_tasks":              flagged,
            "healthy_tasks":              total - flagged,
            "tasks_missing_comment":      missing_comment,
            "tasks_with_blockers":        with_blockers,
            "tasks_at_risk_or_critical":  at_risk_or_critical,
            "tasks_skipped_state":        skipped_state,
            "tasks_skipped_good_history": skipped_good_hist,
        },
        "tasks": enriched_tasks,
    }