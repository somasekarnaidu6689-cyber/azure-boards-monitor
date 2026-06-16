import logging
from config import Config

logger = logging.getLogger(__name__)

# Maximum raw values used to normalise each signal to 0-100 range
_MAX_DAYS_NO_STATE_CHANGE = 10   # 10+ days stuck = max penalty
_MAX_HOURS_UNCHANGED_DAYS = 7    # 7+ days with same hours = max penalty
_MAX_COMMENT_QUALITY_DEFICIT = 10  # quality score is 0-10; deficit = 10 - score
_SPRINT_BUFFER_DAYS = 14         # sprint typically 14 days; 0 remaining = max


def _normalise(value: float, max_value: float) -> float:
    """Clamp value to [0, 1] after dividing by max_value."""
    return min(1.0, max(0.0, value / max_value))


def _risk_label(score: int) -> str:
    """Map risk score to label per document section 6."""
    for threshold, label in Config.RISK_LABELS:
        if score <= threshold:
            return label
    return "Critical"


def calculate_risk_score(
    task: dict,
    analysis: dict,
    days_remaining_in_sprint: int,
    sprint_dates_configured: bool = True,
) -> dict:
    """
    Calculate a weighted risk score (0-100) for a single task.

    Signal weights from the document:
      - days_since_state_change:    25%
      - remaining_hours_unchanged:  20%
      - comment_quality_score:      20%
      - copy_paste_detected:        15%
      - blocker_detected:           10%
      - days_remaining_in_sprint:   10%

    Returns dict with raw_score (0-100), label, and per-signal breakdown.
    """
    weights = Config.RISK_WEIGHTS

    # --- Signal 1: Days since last state change (25%) ---
    days_state = task.get("days_since_state_change", 0)
    s1 = _normalise(days_state, _MAX_DAYS_NO_STATE_CHANGE)

    # --- Signal 2: Remaining hours unchanged (20%) ---
    # We use days_since_last_update as a proxy; if remaining == original estimate,
    # that also indicates stagnation
    remaining = task.get("remaining_hours")
    original = task.get("original_estimate")
    hours_stale = False
    if remaining is not None and original is not None:
        if remaining >= original:
            hours_stale = True
    # Also check days since any field was updated
    days_update = task.get("days_since_last_update", 0)
    if days_update >= 3:
        hours_stale = True
    s2 = _normalise(days_update if hours_stale else 0, _MAX_HOURS_UNCHANGED_DAYS)

    # --- Signal 3: Comment quality (20%) ---
    # Low quality = high risk. Deficit = 10 - quality_score.
    quality_score = analysis.get("quality_score", 0)
    comment_deficit = max(0, 10 - quality_score)
    s3 = _normalise(comment_deficit, _MAX_COMMENT_QUALITY_DEFICIT)

    # --- Signal 4: Copy-paste detected (15%) ---
    s4 = 1.0 if analysis.get("copy_paste_detected", False) else 0.0

    # --- Signal 5: Blocker detected (10%) ---
    s5 = 1.0 if analysis.get("blocker_detected", False) else 0.0

    # --- Signal 6: Days remaining in sprint (10%) ---
    # Very few days left + incomplete task = high risk.
    # If the iteration has no startDate/finishDate configured in Azure DevOps,
    # days_remaining_in_sprint defaults to 0 but does NOT indicate urgency,
    # so this signal is skipped in that case.
    sprint_urgency = 0.0
    if sprint_dates_configured:
        if days_remaining_in_sprint == 0:
            sprint_urgency = 1.0
        elif days_remaining_in_sprint <= 2:
            sprint_urgency = 0.75
        elif days_remaining_in_sprint <= 5:
            sprint_urgency = 0.4
    s6 = sprint_urgency

    # Weighted sum -> scale to 0-100
    raw = (
        s1 * weights["days_since_state_change"]
        + s2 * weights["remaining_hours_unchanged"]
        + s3 * weights["comment_quality_score"]
        + s4 * weights["copy_paste_detected"]
        + s5 * weights["blocker_detected"]
        + s6 * weights["days_remaining_in_sprint"]
    )
    score = round(raw * 100)
    label = _risk_label(score)

    # Override: a missing EOD comment is a direct accountability issue per
    # the use case document (section 2 / section 8 - "EOD comment present"
    # check). Even if the weighted score lands in "Healthy" because every
    # other signal is clean, a task with no comment today should never be
    # labeled Healthy, since the team has no visibility into its status.
    if not analysis.get("has_comment_today", True) and label == "Healthy":
        label = "Watch"

    logger.debug(
        "Task %d '%s' risk: %d (%s) | signals: state=%.2f hrs=%.2f qual=%.2f cp=%.2f blk=%.2f sprint=%.2f",
        task["id"],
        task["title"],
        score,
        label,
        s1,
        s2,
        s3,
        s4,
        s5,
        s6,
    )

    return {
        "risk_score": score,
        "risk_label": label,
        "signals": {
            "days_since_state_change": days_state,
            "days_since_last_update": days_update,
            "hours_stale": hours_stale,
            "comment_quality_score": quality_score,
            "copy_paste_detected": analysis.get("copy_paste_detected", False),
            "blocker_detected": analysis.get("blocker_detected", False),
            "days_remaining_in_sprint": days_remaining_in_sprint,
        },
    }