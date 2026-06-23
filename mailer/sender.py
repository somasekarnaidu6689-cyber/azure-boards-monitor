import logging
import smtplib
import os
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from jinja2 import Environment, FileSystemLoader

from config import Config

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__))
_REPORT_TEMPLATE_FILE = "template.html"
_TASK_TEMPLATE_FILE = "task_template.html"

_env = Environment(loader=FileSystemLoader(_TEMPLATE_DIR), autoescape=True)


def _format_fetched_at(report: dict) -> str:
    fetched_at_str = report.get("fetched_at", "")
    try:
        fetched_dt = datetime.fromisoformat(fetched_at_str)
        return fetched_dt.strftime("%d %b %Y, %I:%M %p UTC")
    except Exception:
        return fetched_at_str


def _render_report_html(report: dict) -> str:
    template = _env.get_template(_REPORT_TEMPLATE_FILE)

    tasks = report["tasks"]
    flagged_tasks = [t for t in tasks if t["needs_attention"]]
    healthy_tasks = [t for t in tasks if not t["needs_attention"]]

    return template.render(
        sprint=report["sprint"],
        summary=report["summary"],
        flagged_tasks=flagged_tasks,
        healthy_tasks=healthy_tasks,
        fetched_at_display=_format_fetched_at(report),
    )


def _render_task_html(report: dict, task: dict) -> str:
    template = _env.get_template(_TASK_TEMPLATE_FILE)

    assignee_name = task["assignee"]["display_name"]
    first_name = (
        assignee_name.split()[0]
        if assignee_name and assignee_name != "Unassigned"
        else "there"
    )

    return template.render(
        sprint=report["sprint"],
        task=task,
        first_name=first_name,
        fetched_at_display=_format_fetched_at(report),
    )


def _send_smtp_email(
    to_addresses: list[str], subject: str, plain_body: str, html_body: str
) -> None:
    """
    Send a single multipart (plain + html) email via SMTP to the given
    recipient list. Raises RuntimeError with a diagnostic message if the
    SMTP server rejects the sender.
    """
    if not to_addresses:
        logger.warning("_send_smtp_email called with no recipients, skipping.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = Config.EMAIL_FROM
    msg["To"] = ", ".join(to_addresses)

    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    logger.info(
        "Sending email from '%s' to %s via %s:%d | subject: %s",
        Config.EMAIL_FROM,
        to_addresses,
        Config.SMTP_HOST,
        Config.SMTP_PORT,
        subject,
    )

    with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
        try:
            server.sendmail(
                Config.EMAIL_FROM,
                to_addresses,
                msg.as_string(),
            )
        except smtplib.SMTPDataError as exc:
            raise RuntimeError(
                f"SMTP server '{Config.SMTP_HOST}' rejected the message "
                f"(EMAIL_FROM='{Config.EMAIL_FROM}', SMTP_USER='{Config.SMTP_USER}'). "
                "This usually means EMAIL_FROM does not match an address the SMTP "
                "server allows SMTP_USER to send as. Set EMAIL_FROM to the same "
                "address as SMTP_USER, or to an address explicitly authorized as "
                "a 'send as' alias on this mail server. "
                f"Original error: {exc}"
            ) from exc

    logger.info("Email sent successfully to %s.", to_addresses)


def send_report(report: dict) -> None:
    """
    Render the full HTML report and send it via SMTP to all addresses in
    Config.EMAIL_TO.
    """
    html_body = _render_report_html(report)

    sprint_name = report["sprint"].get("name", "Sprint")
    today_str = datetime.now(timezone.utc).strftime("%d %b %Y")
    subject = f"EOD Task Report - {sprint_name} - {today_str}"

    total = report["summary"]["total_tasks"]
    flagged = report["summary"]["flagged_tasks"]
    plain = (
        f"EOD Task Report for {sprint_name} ({today_str})\n\n"
        f"Total tasks: {total}\n"
        f"Needs attention: {flagged}\n"
        f"Healthy: {total - flagged}\n\n"
        f"Open the HTML version to see full details and nudge messages."
    )

    _send_smtp_email(Config.EMAIL_TO, subject, plain, html_body)


def send_individual_task_emails(report: dict) -> None:
    """
    Send a focused single-task reminder email to each flagged task's assignee.

    Quality gate logic (EMAIL_QUALITY_GATE_STATE):
      Tasks in this state only get an individual email if ANY of:
        - No comment was added today
        - Comment quality score < EMAIL_GOOD_QUALITY_THRESHOLD
        - Comment was flagged as copy-pasted (regardless of score)
      Once a task in this state has a genuine, non-copy-pasted comment
      scoring >= threshold, no email is sent.

    Tasks in other notify states follow the normal flow.
    """
    sprint_name = report["sprint"].get("name", "Sprint")
    today_str = datetime.now(timezone.utc).strftime("%d %b %Y")

    flagged_tasks = [t for t in report["tasks"] if t["needs_attention"]]

    if not flagged_tasks:
        logger.info("No flagged tasks — skipping individual task emails.")
        return

    email_to_lower = {addr.strip().lower() for addr in Config.EMAIL_TO}
    quality_gate_state = Config.EMAIL_QUALITY_GATE_STATE.strip().lower()
    good_threshold = Config.EMAIL_GOOD_QUALITY_THRESHOLD
    # Always include the quality gate state so it isn't dropped before
    # reaching the quality check — it has its own stricter rule.
    notify_states = {s.strip().lower() for s in Config.EMAIL_NOTIFY_STATES} | {quality_gate_state}

    sent_count = 0
    skipped_no_email = 0
    skipped_good_quality = 0
    skipped_state = 0

    for task in flagged_tasks:
        task_state = task.get("state", "").strip().lower()
        analysis = task["analysis"]
        quality_score = analysis.get("quality_score", 0)
        has_comment = analysis.get("has_comment_today", False)
        copy_pasted = analysis.get("copy_paste_detected", False)

        # ── State filter ──────────────────────────────────────────────────
        if task_state not in notify_states:
            logger.info(
                "Task #%d ('%s') state '%s' not in EMAIL_NOTIFY_STATES %s — skipping.",
                task["id"], task["title"], task.get("state"), Config.EMAIL_NOTIFY_STATES,
            )
            skipped_state += 1
            continue

        # ── Quality gate: only for EMAIL_QUALITY_GATE_STATE ───────────────
        if task_state == quality_gate_state:
            # Email is suppressed only when comment exists, is not copy-pasted,
            # and scores at or above the good threshold.
            comment_is_good = (
                has_comment
                and not copy_pasted
                and quality_score >= good_threshold
            )
            if comment_is_good:
                logger.info(
                    "Task #%d ('%s') [%s] quality score %d/10, not copy-pasted — "
                    "comment is good enough, skipping individual email.",
                    task["id"], task["title"], task.get("state"), quality_score,
                )
                skipped_good_quality += 1
                continue
            else:
                reasons = []
                if not has_comment:
                    reasons.append("no comment today")
                elif copy_pasted:
                    reasons.append(f"copy-pasted comment (score {quality_score}/10)")
                elif quality_score < good_threshold:
                    reasons.append(f"quality score {quality_score}/10 below threshold {good_threshold}")
                logger.info(
                    "Task #%d ('%s') [%s] — sending individual email (%s).",
                    task["id"], task["title"], task.get("state"), ", ".join(reasons),
                )

        # ── Assignee email check ───────────────────────────────────────────
        assignee_email = task["assignee"].get("email", "").strip()

        if not assignee_email:
            logger.warning(
                "Task #%d ('%s') has no assignee email — skipping individual notification.",
                task["id"], task["title"],
            )
            skipped_no_email += 1
            continue

        if assignee_email.lower() in email_to_lower:
            logger.info(
                "Task #%d assignee email '%s' is already in EMAIL_TO — "
                "skipping to avoid duplicate.",
                task["id"], assignee_email,
            )
            continue

        # ── Build and send ────────────────────────────────────────────────
        html_body = _render_task_html(report, task)

        subject = (
            f"EOD Reminder - #{task['id']} {task['title']} - "
            f"{sprint_name} - {today_str}"
        )

        assignee_name = task["assignee"]["display_name"]
        plain = (
            f"Hi {assignee_name},\n\n"
            f"Your task #{task['id']} '{task['title']}' needs attention "
            f"(risk: {task['risk']['risk_label']}, score: {task['risk']['risk_score']}).\n\n"
        )
        if not has_comment:
            plain += "No EOD comment was added today.\n\n"
        elif copy_pasted:
            plain += (
                f"Today's comment appears identical to a recent one (score {quality_score}/10). "
                "Please add a meaningful update describing what actually changed today.\n\n"
            )
        elif quality_score < good_threshold:
            plain += (
                f"Today's comment scored {quality_score}/10 — "
                "a more specific update (what was done, what remains, any risks) would help.\n\n"
            )
        if task.get("nudge"):
            plain += f"Suggested next step: {task['nudge']['message']}\n\n"
        plain += "Open the HTML version of this email for full details."

        _send_smtp_email([assignee_email], subject, plain, html_body)
        sent_count += 1

    logger.info(
        "Individual task emails: %d sent | %d skipped (state) | "
        "%d skipped (good quality) | %d skipped (no assignee email).",
        sent_count, skipped_state, skipped_good_quality, skipped_no_email,
    )