import os
from dotenv import load_dotenv

from utils.secrets import get_secret, using_key_vault

load_dotenv()


class Config:
    # ── Secrets & Authentication ─────────────────────────────────────────
    # Gap: "Security & Secrets" (High) — secrets now resolve through
    # utils.secrets.get_secret(), which checks Azure Key Vault first when
    # KEY_VAULT_URL is set, and falls back to .env otherwise. See
    # README "Secrets & Key Vault" for the rotation/expiry policy.
    KEY_VAULT_URL = os.getenv("KEY_VAULT_URL", "").strip()

    # Gap: "Authentication" (High) — AUTH_MODE selects between the legacy
    # user-bound PAT and a service principal / managed identity. "pat"
    # remains the default for backward compatibility, but service_principal
    # is recommended for production (see README "Authentication" section).
    AUTH_MODE = os.getenv("AZURE_AUTH_MODE", "pat").strip().lower()  # "pat" | "service_principal"

    # Azure DevOps
    AZURE_ORG = os.getenv("AZURE_DEVOPS_ORG")
    AZURE_PROJECT = os.getenv("AZURE_DEVOPS_PROJECT")
    AZURE_PAT = get_secret("AZURE_DEVOPS_PAT")
    AZURE_TEAM = os.getenv("AZURE_DEVOPS_TEAM")

    # Service principal / managed identity credentials (used when
    # AUTH_MODE=service_principal). AZURE_DEVOPS_SP_CLIENT_SECRET is a
    # secret and goes through Key Vault resolution; tenant/client IDs are
    # not secrets and stay as plain env vars.
    AZURE_SP_TENANT_ID = os.getenv("AZURE_DEVOPS_SP_TENANT_ID")
    AZURE_SP_CLIENT_ID = os.getenv("AZURE_DEVOPS_SP_CLIENT_ID")
    AZURE_SP_CLIENT_SECRET = get_secret("AZURE_DEVOPS_SP_CLIENT_SECRET")
    # Azure DevOps' resource ID for AAD token requests (fixed, well-known value)
    AZURE_DEVOPS_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"

    # Derived Azure base URL
    AZURE_BASE_URL = (
        f"https://dev.azure.com/{AZURE_ORG}/{AZURE_PROJECT}/_apis"
    )

    # ── Multi-team support ───────────────────────────────────────────────
    # Gap: "Multi-team Support" (Low) — TEAMS_CONFIG is an optional JSON
    # list of per-team configs so one pipeline instance can process several
    # Azure DevOps teams/projects in a single run instead of duplicating
    # the whole deployment. Each entry only needs to override what differs
    # from the single-team env vars above; anything omitted falls back to
    # the top-level AZURE_* values. If TEAMS_CONFIG is unset, the pipeline
    # behaves exactly as before: one team, defined by the env vars above.
    #
    # Example TEAMS_CONFIG (JSON, set as a single env var or loaded from a
    # file — see README "Multi-team Support"):
    # [
    #   {"name": "platform", "azure_project": "Platform", "azure_team": "Platform Team"},
    #   {"name": "growth",   "azure_project": "Growth",   "azure_team": "Growth Team"}
    # ]
    TEAMS_CONFIG_RAW = os.getenv("TEAMS_CONFIG", "").strip()

    @classmethod
    def teams(cls) -> list[dict]:
        """
        Return the list of teams to process this run. Falls back to a
        single synthetic team built from the top-level AZURE_* vars when
        TEAMS_CONFIG is not set, so existing single-team deployments need
        no changes.
        """
        import json

        if not cls.TEAMS_CONFIG_RAW:
            return [{
                "name": cls.AZURE_TEAM or "default",
                "azure_org": cls.AZURE_ORG,
                "azure_project": cls.AZURE_PROJECT,
                "azure_team": cls.AZURE_TEAM,
            }]

        try:
            raw_teams = json.loads(cls.TEAMS_CONFIG_RAW)
        except json.JSONDecodeError as exc:
            raise EnvironmentError(f"TEAMS_CONFIG is not valid JSON: {exc}") from exc

        teams = []
        for entry in raw_teams:
            teams.append({
                "name": entry.get("name", entry.get("azure_team", "unnamed")),
                "azure_org": entry.get("azure_org", cls.AZURE_ORG),
                "azure_project": entry.get("azure_project", cls.AZURE_PROJECT),
                "azure_team": entry.get("azure_team", cls.AZURE_TEAM),
            })
        return teams

    # Databricks SQL Warehouse
    DATABRICKS_SERVER_HOSTNAME = os.getenv("DATABRICKS_SERVER_HOSTNAME")
    DATABRICKS_HTTP_PATH = os.getenv("DATABRICKS_HTTP_PATH")
    DATABRICKS_ACCESS_TOKEN = get_secret("DATABRICKS_ACCESS_TOKEN")
    DATABRICKS_CATALOG = os.getenv("DATABRICKS_CATALOG", "hive_metastore")
    DATABRICKS_SCHEMA = os.getenv("DATABRICKS_SCHEMA", "eod_task_monitor")

    @classmethod
    def db_table(cls, name: str) -> str:
        """Return fully-qualified Databricks table name: catalog.schema.table"""
        return f"{cls.DATABRICKS_CATALOG}.{cls.DATABRICKS_SCHEMA}.{name}"

    # Groq
    GROQ_API_KEY = get_secret("GROQ_API_KEY")
    GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    # ── Email delivery ───────────────────────────────────────────────────
    # Gap: "Email Delivery" (Med) — EMAIL_PROVIDER selects the transport.
    # "smtp" (Gmail App Password) remains the default for backward
    # compatibility but is not recommended past small teams; "sendgrid" and
    # "acs" (Azure Communication Services) provide delivery receipts and
    # bounce callbacks. See README "Email Delivery" for setup.
    EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "smtp").strip().lower()  # "smtp" | "sendgrid" | "acs"

    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
    SMTP_USER = os.getenv("SMTP_USER")
    SMTP_PASSWORD = get_secret("SMTP_PASSWORD")

    SENDGRID_API_KEY = get_secret("SENDGRID_API_KEY")
    AZURE_COMMUNICATION_CONNECTION_STRING = get_secret("AZURE_COMMUNICATION_CONNECTION_STRING")

    EMAIL_FROM = os.getenv("EMAIL_FROM")
    EMAIL_TO = [e.strip() for e in os.getenv("EMAIL_TO", "").split(",") if e.strip()]
    EMAIL_NOTIFY_STATES = [
        s.strip().lower()
        for s in os.getenv("EMAIL_NOTIFY_STATES", "Active,Resolved").split(",")
        if s.strip()
    ]
    EMAIL_QUALITY_GATE_STATE = os.getenv("EMAIL_QUALITY_GATE_STATE", "Active").strip().lower()
    EMAIL_GOOD_QUALITY_THRESHOLD = int(os.getenv("EMAIL_GOOD_QUALITY_THRESHOLD", 7))

    # ── Teams integration ────────────────────────────────────────────────
    # Gap: "Teams Integration" (Med) — replaces the undocumented Office
    # Script approach with a direct Incoming Webhook / Adaptive Card POST
    # from Python. Optional: if TEAMS_WEBHOOK_URL is unset, Teams delivery
    # is simply skipped (email-only), so this is backward compatible.
    TEAMS_WEBHOOK_URL = get_secret("TEAMS_WEBHOOK_URL")

    # ── Scheduling ───────────────────────────────────────────────────────
    # Gap: "Scheduling & Deployment" (High) — the in-process `schedule`
    # loop is kept ONLY as a local-dev / fallback mode. Production should
    # invoke `python main.py` once per business day from an external
    # trigger (Azure Functions timer trigger, Databricks Workflow, or
    # cron) — see README "Scheduling & Deployment" for the full runbook.
    RUN_HOUR = int(os.getenv("RUN_HOUR", 18))
    RUN_MINUTE = int(os.getenv("RUN_MINUTE", 0))
    SKIP_WEEKENDS = os.getenv("SKIP_WEEKENDS", "true").strip().lower() == "true"
    # Comma-separated ISO dates (YYYY-MM-DD) treated as holidays in addition
    # to weekends, e.g. HOLIDAYS=2026-12-25,2026-01-01
    HOLIDAYS = {
        d.strip() for d in os.getenv("HOLIDAYS", "").split(",") if d.strip()
    }
    # If set (e.g. "US"), also skip public holidays for that country using
    # the `holidays` package, in addition to the explicit HOLIDAYS list.
    HOLIDAY_COUNTRY = os.getenv("HOLIDAY_COUNTRY", "").strip().upper()

    # ── Business rules (from scope section of use case doc) ─────────────
    COMMENT_LOOKBACK_DAYS = int(os.getenv("COMMENT_LOOKBACK_DAYS", 3))
    COPY_PASTE_THRESHOLD = float(os.getenv("COPY_PASTE_THRESHOLD", 0.92))
    RISK_FLAG_THRESHOLD = int(os.getenv("RISK_FLAG_THRESHOLD", 60))

    # Gap: "Data Quality" (Med) — when true, copy-paste detection widens its
    # comparison corpus with historical comment vectors per assignee loaded
    # from Databricks, rather than only the current lookback window.
    PERSIST_COMMENT_HISTORY = os.getenv("PERSIST_COMMENT_HISTORY", "true").strip().lower() == "true"
    COMMENT_HISTORY_MAX_PER_ASSIGNEE = int(os.getenv("COMMENT_HISTORY_MAX_PER_ASSIGNEE", 60))

    # Gap: "Agent Architecture" (Med) / "Scalability" (Low) — per-task
    # Groq work (Agents 2-4) runs concurrently up to this many workers
    # instead of as a strict sequential chain. Keep this comfortably under
    # Groq's free-tier requests-per-minute limit (see README "Scalability").
    MAX_CONCURRENT_TASK_WORKERS = int(os.getenv("MAX_CONCURRENT_TASK_WORKERS", 8))

    # Warn (not block) if a single run has more tasks than this, since the
    # spec was only benchmarked at 4 tasks.
    TASK_COUNT_WARNING_THRESHOLD = int(os.getenv("TASK_COUNT_WARNING_THRESHOLD", 50))

    # Risk score weights (Agent 3 spec from the document)
    RISK_WEIGHTS = {
        "days_since_state_change": 0.25,
        "remaining_hours_unchanged": 0.20,
        "comment_quality_score": 0.20,
        "copy_paste_detected": 0.15,
        "blocker_detected": 0.10,
        "days_remaining_in_sprint": 0.10,
    }

    # Risk labels (from document section 6)
    RISK_LABELS = [
        (30, "Healthy"),
        (60, "Watch"),
        (80, "At Risk"),
        (100, "Critical"),
    ]

    # Parent work item types that live directly in the sprint (section 5.1
    # of the use case doc treats these as containers, not the items being
    # monitored). EOD comments/risk scoring happen on their CHILD Task items.
    PARENT_WORK_ITEM_TYPES = ["User Story"]

    # In-scope work item type for EOD monitoring (section 5.1).
    # These are the child work items linked under the parent stories above
    # via System.LinkTypes.Hierarchy-Forward.
    ALLOWED_WORK_ITEM_TYPES = ["Task"]

    # ── Observability ────────────────────────────────────────────────────
    # Gap: "Observability" (High) — run-level metrics (tasks processed,
    # Groq calls made, emails sent, duration) are written to Databricks
    # (RunMetrics table) every run, and a Teams alert fires if the run
    # fails or does not complete by RUN_DEADLINE_MINUTES after it starts.
    RUN_DEADLINE_MINUTES = int(os.getenv("RUN_DEADLINE_MINUTES", 30))
    METRICS_ALERT_ON_FAILURE = os.getenv("METRICS_ALERT_ON_FAILURE", "true").strip().lower() == "true"

    @classmethod
    def validate(cls):
        required = [
            ("AZURE_DEVOPS_ORG", cls.AZURE_ORG),
            ("AZURE_DEVOPS_PROJECT", cls.AZURE_PROJECT),
            ("GROQ_API_KEY", cls.GROQ_API_KEY),
            ("DATABRICKS_SERVER_HOSTNAME", cls.DATABRICKS_SERVER_HOSTNAME),
            ("DATABRICKS_HTTP_PATH", cls.DATABRICKS_HTTP_PATH),
            ("DATABRICKS_ACCESS_TOKEN", cls.DATABRICKS_ACCESS_TOKEN),
            ("EMAIL_FROM", cls.EMAIL_FROM),
            ("EMAIL_TO", cls.EMAIL_TO),
        ]

        # Auth: exactly one of PAT or service principal must be configured
        if cls.AUTH_MODE == "service_principal":
            required += [
                ("AZURE_DEVOPS_SP_TENANT_ID", cls.AZURE_SP_TENANT_ID),
                ("AZURE_DEVOPS_SP_CLIENT_ID", cls.AZURE_SP_CLIENT_ID),
                ("AZURE_DEVOPS_SP_CLIENT_SECRET", cls.AZURE_SP_CLIENT_SECRET),
            ]
        elif cls.AUTH_MODE == "pat":
            required.append(("AZURE_DEVOPS_PAT", cls.AZURE_PAT))
        else:
            raise EnvironmentError(
                f"Invalid AZURE_AUTH_MODE '{cls.AUTH_MODE}'. Must be 'pat' or 'service_principal'."
            )

        # Email provider-specific requirements
        if cls.EMAIL_PROVIDER == "smtp":
            required += [
                ("SMTP_USER", cls.SMTP_USER),
                ("SMTP_PASSWORD", cls.SMTP_PASSWORD),
            ]
        elif cls.EMAIL_PROVIDER == "sendgrid":
            required.append(("SENDGRID_API_KEY", cls.SENDGRID_API_KEY))
        elif cls.EMAIL_PROVIDER == "acs":
            required.append(
                ("AZURE_COMMUNICATION_CONNECTION_STRING", cls.AZURE_COMMUNICATION_CONNECTION_STRING)
            )
        else:
            raise EnvironmentError(
                f"Invalid EMAIL_PROVIDER '{cls.EMAIL_PROVIDER}'. "
                "Must be one of: smtp, sendgrid, acs."
            )

        missing = [name for name, val in required if not val]
        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}"
            )

    @classmethod
    def using_key_vault(cls) -> bool:
        return using_key_vault()
