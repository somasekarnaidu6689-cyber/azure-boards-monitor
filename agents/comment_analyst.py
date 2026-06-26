import json
import logging
from jsonschema import validate as jsonschema_validate, ValidationError
from groq import Groq
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from config import Config
from utils.retry import retryable_any_exception

logger = logging.getLogger(__name__)

_groq_client = Groq(api_key=Config.GROQ_API_KEY)


QUALITY_SYSTEM_PROMPT = """You are an EOD (end-of-day) comment quality evaluator for a software engineering team.

Your job is to analyse a developer's task comment and return a structured JSON response.

Evaluate the comment on:
- Quality score: 0 to 10 (0 = missing or completely useless, 10 = detailed, specific, and actionable)
- Quality label: one of "excellent", "good", "vague", "copy-pasted", "missing"
- Blocker detected: true if the comment mentions being blocked, waiting on someone, dependency, or any obstacle
- Progress detectable: true if the comment shows measurable or describable progress
- Sentiment: one of "positive", "neutral", "negative"
- Suggested follow-up question: a single specific question a manager could ask this developer tomorrow

Rules:
- Comments like "done", "in progress", "working on it", "same as yesterday" score 0 to 3
- A good comment describes what was done, what is left, and any risks
- A score below 5 means the comment needs attention
- If no comment text is provided, set quality_label to "missing" and score to 0

Return ONLY valid JSON with these exact keys:
{
  "quality_score": <int 0-10>,
  "quality_label": "<str>",
  "blocker_detected": <bool>,
  "progress_detectable": <bool>,
  "sentiment": "<str>",
  "suggested_followup": "<str>"
}"""

# Gap: "LLM Reliability" (Med) — explicit JSON Schema so malformed/partial
# responses are caught deterministically rather than only via a bare
# json.JSONDecodeError, and a degraded state is always well-formed.
_QUALITY_SCHEMA = {
    "type": "object",
    "required": [
        "quality_score", "quality_label", "blocker_detected",
        "progress_detectable", "sentiment", "suggested_followup",
    ],
    "properties": {
        "quality_score": {"type": "integer", "minimum": 0, "maximum": 10},
        "quality_label": {"type": "string", "enum": ["excellent", "good", "vague", "copy-pasted", "missing"]},
        "blocker_detected": {"type": "boolean"},
        "progress_detectable": {"type": "boolean"},
        "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
        "suggested_followup": {"type": "string"},
    },
}

_DEGRADED_QUALITY = {
    "quality_score": 0,
    "quality_label": "skipped",
    "blocker_detected": False,
    "progress_detectable": False,
    "sentiment": "neutral",
    "suggested_followup": "",
}


def _strip_code_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()


@retryable_any_exception("groq-quality-eval")
def _call_groq_quality(messages: list[dict]) -> str:
    """Raw Groq call, retried on transient API/network errors (rate limits,
    5xx, connection drops). JSON-shape problems are handled separately by
    _evaluate_with_correction, since retrying the exact same prompt would
    just reproduce the same malformed output."""
    response = _groq_client.chat.completions.create(
        model=Config.GROQ_MODEL,
        messages=messages,
        temperature=0.1,
        max_tokens=400,
    )
    return response.choices[0].message.content.strip()


def _evaluate_with_correction(user_message: str, task_title: str) -> dict | None:
    """
    Call Groq, validate the JSON shape, and if it's malformed make ONE
    correction attempt with the bad output fed back to the model and an
    explicit instruction to fix it. Returns None (caller substitutes the
    degraded state) if both attempts fail.

    Gap addressed: "LLM Reliability" (Med) — previously a single malformed
    response silently fell back to a fixed "vague" guess with no attempt
    to recover, and a Groq outage nullified AI analysis for the whole run
    without a clearly labeled degraded state.
    """
    messages = [
        {"role": "system", "content": QUALITY_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    for attempt in range(2):
        try:
            raw = _call_groq_quality(messages)
        except Exception as exc:
            logger.warning(
                "Groq quality-eval call failed for task '%s' after retries: %s",
                task_title, exc,
            )
            return None

        try:
            parsed = json.loads(_strip_code_fences(raw))
            jsonschema_validate(parsed, _QUALITY_SCHEMA)
            return parsed
        except (json.JSONDecodeError, ValidationError) as exc:
            if attempt == 0:
                logger.info(
                    "Groq quality-eval response for task '%s' failed schema "
                    "validation (%s) — retrying with correction prompt.",
                    task_title, exc,
                )
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": (
                        "That response was not valid JSON matching the required "
                        "schema. Return ONLY the corrected JSON object with no "
                        "extra text, markdown, or commentary."
                    ),
                })
                continue
            logger.warning(
                "Groq quality-eval response for task '%s' still invalid after "
                "correction attempt (%s) — using degraded state.",
                task_title, exc,
            )
            return None

    return None


def evaluate_comment_quality(
    task_title: str,
    comment_text: str | None,
    assignee_name: str,
) -> dict:
    """
    Send comment to Groq LLM and get structured quality evaluation.
    Returns the parsed JSON response dict, or a clearly labeled degraded
    state (quality_label="skipped") if Groq is unavailable or never
    returns valid JSON — never raises.
    """
    if not comment_text or not comment_text.strip():
        return {
            "quality_score": 0,
            "quality_label": "missing",
            "blocker_detected": False,
            "progress_detectable": False,
            "sentiment": "neutral",
            "suggested_followup": (
                f"Can you share what you worked on today for '{task_title}'?"
            ),
        }

    user_message = (
        f"Task title: {task_title}\n"
        f"Developer: {assignee_name}\n"
        f"EOD comment: {comment_text}"
    )

    result = _evaluate_with_correction(user_message, task_title)
    if result is not None:
        return result

    degraded = _DEGRADED_QUALITY.copy()
    degraded["suggested_followup"] = f"What progress did you make on '{task_title}' today?"
    return degraded


def detect_copy_paste(
    today_comment: str | None,
    recent_comments: list[str],
) -> bool:
    """
    Compare today's comment against recent comments using TF-IDF
    vectorisation + cosine similarity. If similarity > COPY_PASTE_THRESHOLD
    (0.92), flag as copy-pasted.

    `recent_comments` may now include persisted historical comments for
    this assignee (see storage/comment_history.py), not just the current
    COMMENT_LOOKBACK_DAYS window — this is what lets detection catch
    boilerplate repeated across weeks (Gap: "Data Quality", Med).
    """
    if not today_comment or not recent_comments:
        return False

    today_clean = today_comment.strip().lower()
    past_clean = [c.strip().lower() for c in recent_comments if c and c.strip()]

    if not today_clean or not past_clean:
        return False

    corpus = [today_clean] + past_clean

    try:
        vectorizer = TfidfVectorizer().fit(corpus)
        vectors = vectorizer.transform(corpus)
    except ValueError:
        # Happens if corpus has no meaningful tokens (e.g. only stopwords/punctuation)
        return today_clean in past_clean

    today_vec = vectors[0:1]
    past_vecs = vectors[1:]

    similarities = cosine_similarity(today_vec, past_vecs)[0]
    max_sim = float(similarities.max())

    logger.debug(
        "Max comment similarity: %.4f (threshold: %.2f)",
        max_sim,
        Config.COPY_PASTE_THRESHOLD,
    )

    # Exact match always counts as copy-paste even if TF-IDF similarity
    # is slightly below threshold due to vector normalisation edge cases
    if today_clean in past_clean:
        return True

    return max_sim >= Config.COPY_PASTE_THRESHOLD


def analyse_task_comment(task: dict, history_comments: list[str] | None = None) -> dict:
    """
    Run both quality evaluation and copy-paste detection for a single task.

    history_comments: optional persisted comment history for this task's
    assignee (Gap: "Data Quality"), merged with the in-run lookback window
    so copy-paste detection compares against more than just the last
    COMMENT_LOOKBACK_DAYS.

    Returns an analysis dict attached to the task.
    """
    assignee_name = task["assignee"]["display_name"]
    task_title = task["title"]
    today_comment = task.get("today_comment_text")
    recent_comments = list(task.get("recent_comment_texts", []))
    if history_comments:
        recent_comments.extend(history_comments)

    quality = evaluate_comment_quality(task_title, today_comment, assignee_name)
    copy_pasted = detect_copy_paste(today_comment, recent_comments)

    # Override label if copy-paste detected and quality was not already flagged
    if copy_pasted and quality["quality_label"] not in ("missing",):
        quality["quality_label"] = "copy-pasted"

    return {
        "has_comment_today": task["has_comment_today"],
        "quality_score": quality["quality_score"],
        "quality_label": quality["quality_label"],
        "blocker_detected": quality["blocker_detected"],
        "progress_detectable": quality["progress_detectable"],
        "sentiment": quality["sentiment"],
        "suggested_followup": quality["suggested_followup"],
        "copy_paste_detected": copy_pasted,
    }
