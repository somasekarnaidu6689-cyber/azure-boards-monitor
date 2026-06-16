import json
import logging
from groq import Groq
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from config import Config

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


def evaluate_comment_quality(
    task_title: str,
    comment_text: str | None,
    assignee_name: str,
) -> dict:
    """
    Send comment to Groq LLM and get structured quality evaluation.
    Returns the parsed JSON response dict.
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

    try:
        response = _groq_client.chat.completions.create(
            model=Config.GROQ_MODEL,
            messages=[
                {"role": "system", "content": QUALITY_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.1,
            max_tokens=400,
        )
        raw = response.choices[0].message.content.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except (json.JSONDecodeError, Exception) as exc:
        logger.warning("Comment quality eval failed for task '%s': %s", task_title, exc)
        return {
            "quality_score": 0,
            "quality_label": "vague",
            "blocker_detected": False,
            "progress_detectable": False,
            "sentiment": "neutral",
            "suggested_followup": f"What progress did you make on '{task_title}' today?",
        }


def detect_copy_paste(
    today_comment: str | None,
    recent_comments: list[str],
) -> bool:
    """
    Compare today's comment against the last few days of comments using
    TF-IDF vectorisation + cosine similarity.
    If similarity > COPY_PASTE_THRESHOLD (0.92), flag as copy-pasted.
    From document section 6, Agent 2 spec ("sentence embedding similarity"
    implemented here as TF-IDF cosine similarity for lightweight, dependency-free
    short-text comparison).
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


def analyse_task_comment(task: dict) -> dict:
    """
    Run both quality evaluation and copy-paste detection for a single task.
    Returns an analysis dict attached to the task.
    """
    assignee_name = task["assignee"]["display_name"]
    task_title = task["title"]
    today_comment = task.get("today_comment_text")
    recent_comments = task.get("recent_comment_texts", [])

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