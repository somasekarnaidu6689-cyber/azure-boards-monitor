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
    For every flagged task that has an assignee email (System.AssignedTo
    -> uniqueName), send a focused single-task reminder email directly to
    that person.

    Tasks with no assignee email, or whose assignee email is also present
    in Config.EMAIL_TO (to avoid duplicate sends to the same address), are
    skipped.
    """
    sprint_name = report["sprint"].get("name", "Sprint")
    today_str = datetime.now(timezone.utc).strftime("%d %b %Y")

    flagged_tasks = [t for t in report["tasks"] if t["needs_attention"]]

    if not flagged_tasks:
        logger.info("No flagged tasks - skipping individual task emails.")
        return

    email_to_lower = {addr.strip().lower() for addr in Config.EMAIL_TO}

    sent_count = 0
    skipped_no_email = 0

    for task in flagged_tasks:
        assignee_email = task["assignee"].get("email", "").strip()

        if not assignee_email:
            logger.warning(
                "Task #%d ('%s') has no assignee email - skipping individual notification.",
                task["id"],
                task["title"],
            )
            skipped_no_email += 1
            continue

        if assignee_email.lower() in email_to_lower:
            logger.info(
                "Task #%d assignee email '%s' is already in EMAIL_TO - "
                "skipping individual email to avoid duplicate.",
                task["id"],
                assignee_email,
            )
            continue

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
        if not task["analysis"]["has_comment_today"]:
            plain += "No EOD comment was added today.\n\n"
        if task.get("nudge"):
            plain += f"Suggested next step: {task['nudge']['message']}\n\n"
        plain += "Open the HTML version of this email for full details."

        _send_smtp_email([assignee_email], subject, plain, html_body)
        sent_count += 1

    logger.info(
        "Individual task emails: %d sent, %d skipped (no assignee email).",
        sent_count,
        skipped_no_email,
    )