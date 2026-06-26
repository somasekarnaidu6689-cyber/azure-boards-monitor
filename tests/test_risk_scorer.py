from agents.risk_scorer import calculate_risk_score


def _base_task(**overrides):
    task = {
        "id": 1,
        "title": "Sample task",
        "days_since_state_change": 0,
        "days_since_last_update": 0,
        "remaining_hours": 5.0,
        "original_estimate": 8.0,
    }
    task.update(overrides)
    return task


def _base_analysis(**overrides):
    analysis = {
        "has_comment_today": True,
        "quality_score": 8,
        "copy_paste_detected": False,
        "blocker_detected": False,
    }
    analysis.update(overrides)
    return analysis


def test_healthy_task_scores_low():
    task = _base_task()
    analysis = _base_analysis()
    result = calculate_risk_score(task, analysis, days_remaining_in_sprint=10)
    assert result["risk_score"] < 30
    assert result["risk_label"] == "Healthy"


def test_missing_comment_is_never_labeled_healthy():
    """Gap-analysis-relevant invariant: a task with no comment today must
    never read as 'Healthy' even if every other signal is clean, since the
    team has zero visibility into status (see risk_scorer.py override)."""
    task = _base_task()
    analysis = _base_analysis(has_comment_today=False, quality_score=0)
    result = calculate_risk_score(task, analysis, days_remaining_in_sprint=10)
    assert result["risk_label"] != "Healthy"


def test_blocker_and_copy_paste_increase_score():
    task = _base_task()
    clean = calculate_risk_score(task, _base_analysis(), days_remaining_in_sprint=10)
    risky = calculate_risk_score(
        task,
        _base_analysis(blocker_detected=True, copy_paste_detected=True, quality_score=2),
        days_remaining_in_sprint=10,
    )
    assert risky["risk_score"] > clean["risk_score"]


def test_stale_task_near_max_days_increases_score():
    fresh = calculate_risk_score(
        _base_task(days_since_state_change=0), _base_analysis(), days_remaining_in_sprint=10
    )
    stale = calculate_risk_score(
        _base_task(days_since_state_change=10), _base_analysis(), days_remaining_in_sprint=10
    )
    assert stale["risk_score"] > fresh["risk_score"]


def test_zero_days_remaining_increases_urgency_when_dates_configured():
    far = calculate_risk_score(
        _base_task(), _base_analysis(quality_score=5), days_remaining_in_sprint=10,
        sprint_dates_configured=True,
    )
    urgent = calculate_risk_score(
        _base_task(), _base_analysis(quality_score=5), days_remaining_in_sprint=0,
        sprint_dates_configured=True,
    )
    assert urgent["risk_score"] > far["risk_score"]


def test_sprint_urgency_ignored_when_dates_not_configured():
    """If the iteration has no start/finish date in Azure DevOps,
    days_remaining_in_sprint defaults to 0 but must NOT be treated as
    urgent — see sprint_dates_configured flag."""
    result = calculate_risk_score(
        _base_task(), _base_analysis(quality_score=8), days_remaining_in_sprint=0,
        sprint_dates_configured=False,
    )
    assert result["signals"]["days_remaining_in_sprint"] == 0
    # With dates not configured, sprint urgency signal contributes 0 — score
    # should be low for an otherwise-healthy task.
    assert result["risk_score"] < 20


def test_risk_score_bounded_0_to_100():
    worst = calculate_risk_score(
        _base_task(days_since_state_change=999, days_since_last_update=999),
        _base_analysis(
            has_comment_today=False, quality_score=0,
            copy_paste_detected=True, blocker_detected=True,
        ),
        days_remaining_in_sprint=0,
    )
    assert 0 <= worst["risk_score"] <= 100
