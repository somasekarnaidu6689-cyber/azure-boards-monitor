from unittest.mock import patch
from agents.pipeline import run_pipeline


def _board(tasks):
    return {
        "sprint": {"name": "Sprint 1", "days_remaining": 5, "dates_configured": True},
        "fetched_at": "2026-06-26T18:00:00+00:00",
        "tasks": tasks,
    }


def _task(id_, state="Active", **overrides):
    task = {
        "id": id_,
        "title": f"Task {id_}",
        "state": state,
        "assignee": {"display_name": "Jane", "email": "jane@example.com"},
        "remaining_hours": 4.0,
        "original_estimate": 8.0,
        "days_since_state_change": 1,
        "days_since_last_update": 1,
        "has_comment_today": True,
        "today_comment_text": "Worked on it today.",
        "recent_comment_texts": [],
    }
    task.update(overrides)
    return task


@patch("agents.pipeline.fetch_latest_snapshot", return_value={})
@patch("agents.pipeline.load_recent_comments_by_assignee", return_value={})
@patch("agents.pipeline.write_nudge_message", return_value={"message": "Nudge", "tone": "gentle"})
@patch("agents.pipeline.calculate_risk_score")
@patch("agents.pipeline.analyse_task_comment")
def test_task_in_irrelevant_state_skips_groq_entirely(
    mock_analyse, mock_risk, mock_nudge, mock_history, mock_snapshot
):
    """Tasks in states outside EMAIL_NOTIFY_STATES/EMAIL_QUALITY_GATE_STATE
    must never trigger a Groq call."""
    tasks = [_task(1, state="Closed"), _task(2, state="New")]
    report = run_pipeline(_board(tasks))

    mock_analyse.assert_not_called()
    mock_risk.assert_not_called()
    for t in report["tasks"]:
        assert t["needs_attention"] is False
        assert t["analysis"]["quality_label"] == "skipped"


@patch("agents.pipeline.fetch_latest_snapshot", return_value={})
@patch("agents.pipeline.load_recent_comments_by_assignee", return_value={})
@patch("agents.pipeline.write_nudge_message", return_value={"message": "Nudge", "tone": "gentle"})
@patch(
    "agents.pipeline.calculate_risk_score",
    return_value={"risk_score": 10, "risk_label": "Healthy", "signals": {}},
)
@patch(
    "agents.pipeline.analyse_task_comment",
    return_value={
        "has_comment_today": True, "quality_score": 9, "quality_label": "excellent",
        "blocker_detected": False, "progress_detectable": True,
        "sentiment": "positive", "suggested_followup": "", "copy_paste_detected": False,
    },
)
def test_active_task_runs_through_full_pipeline(mock_analyse, mock_risk, mock_nudge, mock_history, mock_snapshot):
    tasks = [_task(1, state="Active")]
    report = run_pipeline(_board(tasks))

    mock_analyse.assert_called_once()
    mock_risk.assert_called_once()
    assert report["tasks"][0]["needs_attention"] is False
    assert report["summary"]["total_tasks"] == 1


@patch("agents.pipeline.fetch_latest_snapshot")
@patch("agents.pipeline.load_recent_comments_by_assignee", return_value={})
@patch("agents.pipeline.write_nudge_message", return_value={"message": "Nudge", "tone": "gentle"})
@patch("agents.pipeline.calculate_risk_score")
@patch("agents.pipeline.analyse_task_comment")
def test_quality_gate_suppresses_reminder_when_yesterday_was_good(
    mock_analyse, mock_risk, mock_nudge, mock_history, mock_snapshot
):
    """Gap-relevant regression test: this also guards against the
    duplicate fetch_latest_snapshot() bug that previously overwrote the
    real lookup with a second, often-empty call."""
    mock_snapshot.return_value = {
        1: {"latest_date": "2026-06-25", "latest_score": 9, "is_good": True}
    }
    tasks = [_task(1, state="Active", has_comment_today=False, today_comment_text=None)]
    report = run_pipeline(_board(tasks))

    mock_analyse.assert_not_called()
    mock_risk.assert_not_called()
    mock_snapshot.assert_called_once()  # must only be called once, not twice
    assert report["tasks"][0]["needs_attention"] is False


@patch("agents.pipeline.fetch_latest_snapshot")
@patch("agents.pipeline.load_recent_comments_by_assignee", return_value={})
@patch("agents.pipeline.write_nudge_message", return_value={"message": "Nudge", "tone": "gentle"})
@patch(
    "agents.pipeline.calculate_risk_score",
    return_value={"risk_score": 70, "risk_label": "At Risk", "signals": {}},
)
@patch(
    "agents.pipeline.analyse_task_comment",
    return_value={
        "has_comment_today": False, "quality_score": 0, "quality_label": "missing",
        "blocker_detected": False, "progress_detectable": False,
        "sentiment": "neutral", "suggested_followup": "", "copy_paste_detected": False,
    },
)
def test_quality_gate_runs_analysis_when_last_good_was_stale(
    mock_analyse, mock_risk, mock_nudge, mock_history, mock_snapshot
):
    mock_snapshot.return_value = {
        1: {"latest_date": "2026-06-20", "latest_score": 2, "is_good": False}
    }
    tasks = [_task(1, state="Active", has_comment_today=False, today_comment_text=None)]
    report = run_pipeline(_board(tasks))

    mock_analyse.assert_called_once()
    assert report["tasks"][0]["needs_attention"] is True


@patch("agents.pipeline.fetch_latest_snapshot", return_value={})
@patch("agents.pipeline.load_recent_comments_by_assignee", return_value={})
@patch("agents.pipeline.write_nudge_message", return_value={"message": "Nudge", "tone": "gentle"})
@patch("agents.pipeline.calculate_risk_score")
@patch("agents.pipeline.analyse_task_comment")
def test_one_failing_task_does_not_abort_other_tasks(mock_analyse, mock_risk, mock_nudge, mock_history, mock_snapshot):
    """Gap: 'Agent Architecture' concurrency — one task's analysis raising
    must not prevent other tasks (processed on other workers) from
    completing successfully."""
    def analyse_side_effect(task, history_comments=None):
        if task["id"] == 1:
            raise RuntimeError("boom")
        return {
            "has_comment_today": True, "quality_score": 7, "quality_label": "good",
            "blocker_detected": False, "progress_detectable": True,
            "sentiment": "neutral", "suggested_followup": "", "copy_paste_detected": False,
        }

    mock_analyse.side_effect = analyse_side_effect
    mock_risk.return_value = {"risk_score": 20, "risk_label": "Healthy", "signals": {}}

    tasks = [_task(1, state="Active"), _task(2, state="Active")]
    report = run_pipeline(_board(tasks))

    assert report["summary"]["total_tasks"] == 2
    task_2_result = next(t for t in report["tasks"] if t["id"] == 2)
    assert task_2_result["analysis"]["quality_label"] == "good"
