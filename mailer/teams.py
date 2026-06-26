"""
mailer/teams.py

Gap addressed: "Teams Integration" (Med) — the previous design routed Teams
notifications through an undocumented Office Script in Excel on the Web,
with no detail on triggering, connection to the Python pipeline, or
failure mode. This module replaces that with a direct POST from Python to
a Teams Incoming Webhook using the Adaptive Card format, making Teams
delivery a first-class, observable pipeline step (Agent 5 / delivery
layer) instead of an external dependency.

Setup (see README "Teams Integration" for the full walkthrough):
  1. In the target Teams channel: ... > Connectors > Incoming Webhook
     (or Workflows > "Post to a channel when a webhook request is
     received" in newer Teams).
  2. Copy the generated URL into TEAMS_WEBHOOK_URL (treat it as a secret —
     resolved through utils.secrets / Key Vault like other credentials).
  3. If TEAMS_WEBHOOK_URL is unset, Teams delivery is skipped entirely and
     the pipeline continues with email-only delivery (no hard dependency).
"""

import logging
import requests
from config import Config
from utils.retry import retryable

logger = logging.getLogger(__name__)

_TEAMS_TIMEOUT_SECONDS = 15


def teams_enabled() -> bool:
    return bool(Config.TEAMS_WEBHOOK_URL)


def _build_adaptive_card(report: dict) -> dict:
    summary = report["summary"]
    sprint_name = report["sprint"].get("name", "Sprint")
    flagged_tasks = [t for t in report["tasks"] if t.get("needs_attention")][:8]

    facts = [
        {"title": "Total tasks", "value": str(summary["total_tasks"])},
        {"title": "Flagged", "value": str(summary["flagged_tasks"])},
        {"title": "Healthy", "value": str(summary["healthy_tasks"])},
        {"title": "Blockers", "value": str(summary["tasks_with_blockers"])},
    ]

    body = [
        {
            "type": "TextBlock",
            "text": f"EOD Task Report — {sprint_name}",
            "weight": "Bolder",
            "size": "Medium",
        },
        {"type": "FactSet", "facts": facts},
    ]

    if flagged_tasks:
        body.append({
            "type": "TextBlock",
            "text": "Top flagged tasks:",
            "weight": "Bolder",
            "spacing": "Medium",
        })
        for t in flagged_tasks:
            body.append({
                "type": "TextBlock",
                "text": f"#{t['id']} {t['title']} — {t['risk']['risk_label']} "
                        f"({t['risk']['risk_score']}) — {t['assignee']['display_name']}",
                "wrap": True,
            })

    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": body,
            },
        }],
    }


@retryable("teams-webhook")
def _post_webhook(payload: dict) -> None:
    response = requests.post(
        Config.TEAMS_WEBHOOK_URL, json=payload, timeout=_TEAMS_TIMEOUT_SECONDS
    )
    response.raise_for_status()


def send_teams_report(report: dict) -> str:
    """
    Post the EOD summary as an Adaptive Card to the configured Teams
    webhook. Returns a delivery status string: "sent" | "skipped" | "failed".
    Never raises — a Teams outage must not take down email delivery.
    """
    if not teams_enabled():
        logger.info("TEAMS_WEBHOOK_URL not configured — skipping Teams notification.")
        return "disabled"

    try:
        _post_webhook(_build_adaptive_card(report))
        logger.info("Teams notification sent successfully.")
        return "sent"
    except Exception as exc:
        logger.warning("Teams notification failed (non-fatal): %s", exc)
        return "failed"


def send_teams_alert(message: str) -> str:
    """
    Post a plain-text pipeline-health alert (e.g. run failed, run did not
    complete by deadline). Used by main.py / utils.metrics for the
    Observability gap's "alert if the run does not complete" requirement.
    """
    if not teams_enabled():
        return "disabled"

    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "text": "EOD Task Monitor — Alert", "weight": "Bolder", "color": "Attention"},
                    {"type": "TextBlock", "text": message, "wrap": True},
                ],
            },
        }],
    }

    try:
        _post_webhook(payload)
        return "sent"
    except Exception as exc:
        logger.warning("Teams alert failed (non-fatal): %s", exc)
        return "failed"
