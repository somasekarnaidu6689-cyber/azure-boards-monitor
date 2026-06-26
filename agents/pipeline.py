import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from agents.comment_analyst import analyse_task_comment
from agents.risk_scorer import calculate_risk_score
from agents.nudge_writer import write_nudge_message
from storage.databricks_client import fetch_latest_snapshot
from storage.comment_history import load_recent_comments_by_assignee
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


def _process_one_task(
    task: dict,
    days_remaining: int,
    sprint_dates_configured: bool,
    history_by_email: dict[str, list[str]],
) -> dict:
    """
    Run Agents 2-4 (comment analysis -> risk scoring -> nudge writing) for
    a single task. Pulled out into its own function so it can be submitted
    to a thread pool — Groq calls are network-bound, so this is where the
    "Agent Architecture" concurrency gap is addressed (Med severity).
    """
    assignee_email = (task.get("assignee") or {}).get("email", "")
    history = history_by_email.get(assignee_email, [])

    analysis = analyse_task_comment(task, history_comments=history)
    risk = calculate_risk_score(task, analysis, days_remaining, sprint_dates_configured)

    return {"task": task, "analysis": analysis, "risk": risk}


def run_pipeline(board_data: dict, metrics=None) -> dict:
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

    Concurrency (Gap: "Agent Architecture", Med): tasks that need Groq
    analysis are processed concurrently, up to
    Config.MAX_CONCURRENT_TASK_WORKERS at a time, instead of one at a time.
    Order is not significant during processing — results are re-sorted by
    risk score at the end as before.

    metrics: optional storage.metrics.RunMetrics instance; if provided,
    tasks_processed / groq_calls_made / groq_calls_failed are updated as
    the pipeline runs (Gap: "Observability", High).
    """
    sprint = board_data["sprint"]
    days_remaining       = sprint.get("days_remaining", 0)
    sprint_dates_configured = sprint.get("dates_configured", True)
    tasks                = board_data.get("tasks", [])

    if len(tasks) > Config.TASK_COUNT_WARNING_THRESHOLD:
        logger.warning(
            "This run has %d tasks, above TASK_COUNT_WARNING_THRESHOLD (%d). "
            "The pipeline was originally benchmarked at a handful of tasks per "
            "sprint — watch Groq rate limits and Databricks write latency as "
            "task counts grow (see README 'Scalability').",
            len(tasks), Config.TASK_COUNT_WARNING_THRESHOLD,
        )

    active_states      = _active_states()
    quality_gate_state = Config.EMAIL_QUALITY_GATE_STATE.strip().lower()
    good_threshold     = Config.EMAIL_GOOD_QUALITY_THRESHOLD

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
    # NOTE: previously fetch_latest_snapshot() was called a second time here
    # unconditionally, silently overwriting the result above with one built
    # from possibly-empty gate_task_ids and doubling a Databricks round trip
    # for no purpose. Removed — this was a bug, not intentional behavior.

    # ── Persisted comment history per assignee (Gap: "Data Quality") ──────
    assignee_emails = [
        (t.get("assignee") or {}).get("email", "") for t in tasks
        if t.get("state", "").strip().lower() in active_states
    ]
    history_by_email = load_recent_comments_by_assignee(assignee_emails)

    enriched_tasks      = []
    total               = len(tasks)
    flagged             = 0
    missing_comment     = 0
    with_blockers       = 0
    at_risk_or_critical = 0
    skipped_state       = 0
    skipped_good_hist    = 0

    tasks_needing_analysis = []

    for task in tasks:
        task_state_lower = task.get("state", "").strip().lower()
        task_id          = task["id"]

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
                logger.info(
                    "Task #%d latest comment on %s scored %d (below threshold) — running analysis.",
                    task_id, rec["latest_date"], rec["latest_score"],
                )

        tasks_needing_analysis.append(task)

    # ── Concurrent Agent 2/3 processing for all tasks that need it ────────
    results_by_id: dict[int, dict] = {}
    max_workers = max(1, min(Config.MAX_CONCURRENT_TASK_WORKERS, len(tasks_needing_analysis) or 1))

    if tasks_needing_analysis:
        logger.info(
            "Running comment analysis + risk scoring for %d task(s) using up to %d concurrent workers...",
            len(tasks_needing_analysis), max_workers,
        )
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    _process_one_task, task, days_remaining, sprint_dates_configured, history_by_email
                ): task["id"]
                for task in tasks_needing_analysis
            }
            for future in as_completed(futures):
                task_id = futures[future]
                try:
                    results_by_id[task_id] = future.result()
                    if metrics is not None:
                        metrics.groq_calls_made += 1
                except Exception as exc:
                    logger.error("Task #%d analysis failed unexpectedly: %s", task_id, exc)
                    if metrics is not None:
                        metrics.groq_calls_failed += 1
                    failing_task = next(t for t in tasks_needing_analysis if t["id"] == task_id)
                    results_by_id[task_id] = {
                        "task": failing_task,
                        "analysis": _NULL_ANALYSIS.copy(),
                        "risk": _NULL_RISK.copy(),
                    }

    # ── Agent 4: Nudge writing — sequential (low volume: only flagged tasks) ─
    for task in tasks_needing_analysis:
        task_id = task["id"]
        result = results_by_id[task_id]
        analysis = result["analysis"]
        risk = result["risk"]
        task_state_lower = task.get("state", "").strip().lower()

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

    if metrics is not None:
        metrics.tasks_processed = total

    # Sort: most critical first
    enriched_tasks.sort(key=lambda t: t["risk"]["risk_score"], reverse=True)

    logger.info(
        "Pipeline done. Total: %d | Groq called: %d | "
        "Skipped (state): %d | Skipped (good history): %d",
        total,
        len(tasks_needing_analysis),
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
