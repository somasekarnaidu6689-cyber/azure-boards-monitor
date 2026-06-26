import json
from unittest.mock import patch

from agents.comment_analyst import (
    detect_copy_paste,
    evaluate_comment_quality,
    analyse_task_comment,
)


# ── detect_copy_paste (pure logic, no mocking needed) ─────────────────────

def test_detect_copy_paste_exact_repeat():
    assert detect_copy_paste(
        "Finished login validation, writing tests next.",
        ["Finished login validation, writing tests next."],
    ) is True


def test_detect_copy_paste_genuinely_different_text():
    assert detect_copy_paste(
        "Finished login validation, writing tests next.",
        ["Refactored the payment retry logic and added logging."],
    ) is False


def test_detect_copy_paste_no_history_returns_false():
    assert detect_copy_paste("Some update today.", []) is False


def test_detect_copy_paste_no_today_comment_returns_false():
    assert detect_copy_paste(None, ["Yesterday's comment."]) is False


def test_detect_copy_paste_matches_against_persisted_history():
    """Gap: 'Data Quality' — history passed in (e.g. from Databricks via
    storage/comment_history.py) must be checked exactly like in-run
    lookback comments."""
    today = "Working on it, same as before."
    history_from_weeks_ago = ["Working on it, same as before."]
    assert detect_copy_paste(today, history_from_weeks_ago) is True


# ── evaluate_comment_quality / JSON reliability (mocked Groq) ────────────

def _mock_groq_response(content: str):
    class FakeChoice:
        def __init__(self, c):
            self.message = type("M", (), {"content": c})()

    class FakeResponse:
        def __init__(self, c):
            self.choices = [FakeChoice(c)]

    return FakeResponse(content)


def test_evaluate_comment_quality_missing_comment_skips_groq():
    result = evaluate_comment_quality("Some task", None, "Jane Doe")
    assert result["quality_label"] == "missing"
    assert result["quality_score"] == 0


@patch("agents.comment_analyst._groq_client")
def test_evaluate_comment_quality_valid_json_first_try(mock_client):
    valid_payload = {
        "quality_score": 8, "quality_label": "good", "blocker_detected": False,
        "progress_detectable": True, "sentiment": "positive",
        "suggested_followup": "What's next?",
    }
    mock_client.chat.completions.create.return_value = _mock_groq_response(json.dumps(valid_payload))

    result = evaluate_comment_quality("Task", "Did real work today.", "Jane")
    assert result["quality_score"] == 8
    assert mock_client.chat.completions.create.call_count == 1


@patch("agents.comment_analyst._groq_client")
def test_evaluate_comment_quality_recovers_from_malformed_json_via_correction(mock_client):
    """Gap: 'LLM Reliability' — one malformed response should trigger a
    single correction retry, not an immediate fallback to a fixed guess."""
    valid_payload = {
        "quality_score": 6, "quality_label": "good", "blocker_detected": False,
        "progress_detectable": True, "sentiment": "neutral",
        "suggested_followup": "Anything blocking you?",
    }
    mock_client.chat.completions.create.side_effect = [
        _mock_groq_response("not json at all, sorry"),
        _mock_groq_response(json.dumps(valid_payload)),
    ]

    result = evaluate_comment_quality("Task", "Did some work.", "Jane")
    assert result["quality_score"] == 6
    assert mock_client.chat.completions.create.call_count == 2


@patch("agents.comment_analyst._groq_client")
def test_evaluate_comment_quality_degrades_gracefully_after_two_failures(mock_client):
    """Gap: 'LLM Reliability' — after both attempts fail, return a clearly
    labeled degraded state rather than raising or silently guessing 'vague'."""
    mock_client.chat.completions.create.side_effect = [
        _mock_groq_response("garbage"),
        _mock_groq_response("still garbage"),
    ]

    result = evaluate_comment_quality("Task", "Did some work.", "Jane")
    assert result["quality_label"] == "skipped"
    assert result["quality_score"] == 0


@patch("agents.comment_analyst._groq_client")
def test_evaluate_comment_quality_degrades_on_groq_exception(mock_client):
    mock_client.chat.completions.create.side_effect = RuntimeError("Groq API down")

    result = evaluate_comment_quality("Task", "Did some work.", "Jane")
    assert result["quality_label"] == "skipped"


@patch("agents.comment_analyst._groq_client")
def test_analyse_task_comment_merges_persisted_history(mock_client):
    valid_payload = {
        "quality_score": 9, "quality_label": "excellent", "blocker_detected": False,
        "progress_detectable": True, "sentiment": "positive", "suggested_followup": "Great work.",
    }
    mock_client.chat.completions.create.return_value = _mock_groq_response(json.dumps(valid_payload))

    task = {
        "id": 1,
        "title": "Task",
        "assignee": {"display_name": "Jane"},
        "has_comment_today": True,
        "today_comment_text": "Same boilerplate update.",
        "recent_comment_texts": [],
    }
    # No in-run history, but persisted history from weeks ago has the exact match.
    result = analyse_task_comment(task, history_comments=["Same boilerplate update."])
    assert result["copy_paste_detected"] is True
