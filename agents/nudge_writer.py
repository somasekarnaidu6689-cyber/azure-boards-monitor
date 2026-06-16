import json
import logging
from groq import Groq
from config import Config

logger = logging.getLogger(__name__)

_groq_client = Groq(api_key=Config.GROQ_API_KEY)

NUDGE_SYSTEM_PROMPT = """You are a supportive but direct engineering team lead writing an end-of-day nudge message to a developer.

Your tone changes based on how many consecutive days the issue has been present:
- 1st occurrence: gentle, friendly reminder
- 2nd consecutive occurrence: direct, professional, more specific
- 3rd or more: firm, clear expectation setting

Rules:
- Use the developer's first name only
- Reference the specific task title
- Mention the actual issue (no comment, vague comment, blocker, stale hours, etc.)
- End with exactly ONE specific question the developer should answer
- Keep the message under 80 words
- Do not use emojis or exclamation marks
- Write in plain text, not HTML

Return ONLY valid JSON with this structure:
{
  "message": "<the nudge message>",
  "tone": "<gentle|direct|firm>"
}"""


def write_nudge_message(
    task: dict,
    analysis: dict,
    risk: dict,
    consecutive_days: int = 1,
) -> dict:
    """
    Generate a personalized nudge message for a developer whose task needs attention.
    consecutive_days: how many days in a row this task has been flagged.
    """
    assignee = task["assignee"]
    first_name = assignee["display_name"].split()[0] if assignee["display_name"] else "Developer"

    issues = []
    if not analysis["has_comment_today"]:
        issues.append("no EOD comment was added today")
    elif analysis["quality_label"] == "missing":
        issues.append("no EOD comment was added today")
    elif analysis["quality_label"] == "copy-pasted":
        issues.append("the EOD comment appears identical to previous days")
    elif analysis["quality_label"] == "vague":
        issues.append("the EOD comment is too vague to indicate real progress")
    if analysis["blocker_detected"]:
        issues.append("a blocker was mentioned in the comment")
    if risk["signals"]["hours_stale"]:
        issues.append("remaining hours have not been updated")
    if risk["risk_label"] in ("At Risk", "Critical"):
        issues.append(f"this task is scored {risk['risk_label']} for sprint completion")

    issues_str = "; ".join(issues) if issues else "the task needs attention"

    user_message = (
        f"Developer first name: {first_name}\n"
        f"Task title: {task['title']}\n"
        f"Current state: {task['state']}\n"
        f"Remaining hours: {task.get('remaining_hours', 'not set')}\n"
        f"Sprint days remaining: {risk['signals']['days_remaining_in_sprint']}\n"
        f"Issues identified: {issues_str}\n"
        f"Consecutive days flagged: {consecutive_days}\n"
        f"Suggested follow-up question from analysis: {analysis.get('suggested_followup', '')}"
    )

    try:
        response = _groq_client.chat.completions.create(
            model=Config.GROQ_MODEL,
            messages=[
                {"role": "system", "content": NUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.4,
            max_tokens=300,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
        return {
            "message": result.get("message", ""),
            "tone": result.get("tone", "gentle"),
        }
    except Exception as exc:
        logger.warning("Nudge generation failed for task '%s': %s", task["title"], exc)
        return {
            "message": (
                f"Hi {first_name}, a quick note on {task['title']}: {issues_str}. "
                f"Can you share an update before EOD?"
            ),
            "tone": "gentle",
        }
