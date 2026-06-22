import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Azure DevOps
    AZURE_ORG = os.getenv("AZURE_DEVOPS_ORG")
    AZURE_PROJECT = os.getenv("AZURE_DEVOPS_PROJECT")
    AZURE_PAT = os.getenv("AZURE_DEVOPS_PAT")
    AZURE_TEAM = os.getenv("AZURE_DEVOPS_TEAM")

    # Derived Azure base URL
    AZURE_BASE_URL = (
        f"https://dev.azure.com/{AZURE_ORG}/{AZURE_PROJECT}/_apis"
    )

    # Databricks SQL Warehouse
    DATABRICKS_SERVER_HOSTNAME = os.getenv("DATABRICKS_SERVER_HOSTNAME")
    DATABRICKS_HTTP_PATH = os.getenv("DATABRICKS_HTTP_PATH")
    DATABRICKS_ACCESS_TOKEN = os.getenv("DATABRICKS_ACCESS_TOKEN")
    DATABRICKS_CATALOG = os.getenv("DATABRICKS_CATALOG", "hive_metastore")
    DATABRICKS_SCHEMA = os.getenv("DATABRICKS_SCHEMA", "eod_task_monitor")

    # Derived fully-qualified table names
    @classmethod
    def db_table(cls, name: str) -> str:
        return f"{cls.DATABRICKS_CATALOG}.{cls.DATABRICKS_SCHEMA}.{name}"

    # Groq
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    # Email
    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
    SMTP_USER = os.getenv("SMTP_USER")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
    EMAIL_FROM = os.getenv("EMAIL_FROM")
    EMAIL_TO = [e.strip() for e in os.getenv("EMAIL_TO", "").split(",") if e.strip()]
    EMAIL_NOTIFY_STATES = [
        s.strip().lower()
        for s in os.getenv("EMAIL_NOTIFY_STATES", "New,Active,Resolved").split(",")
        if s.strip()
    ]
    EMAIL_QUALITY_GATE_STATE = os.getenv("EMAIL_QUALITY_GATE_STATE", "Active").strip().lower()
    EMAIL_GOOD_QUALITY_THRESHOLD = int(os.getenv("EMAIL_GOOD_QUALITY_THRESHOLD", 7))

    # Scheduler
    RUN_HOUR = int(os.getenv("RUN_HOUR", 18))
    RUN_MINUTE = int(os.getenv("RUN_MINUTE", 0))

    # Business rules (from scope section of use case doc)
    COMMENT_LOOKBACK_DAYS = int(os.getenv("COMMENT_LOOKBACK_DAYS", 3))
    COPY_PASTE_THRESHOLD = float(os.getenv("COPY_PASTE_THRESHOLD", 0.92))
    RISK_FLAG_THRESHOLD = int(os.getenv("RISK_FLAG_THRESHOLD", 60))

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

    # Databricks SQL Warehouse
    DATABRICKS_SERVER_HOSTNAME = os.getenv("DATABRICKS_SERVER_HOSTNAME")
    DATABRICKS_HTTP_PATH = os.getenv("DATABRICKS_HTTP_PATH")
    DATABRICKS_ACCESS_TOKEN = os.getenv("DATABRICKS_ACCESS_TOKEN")
    DATABRICKS_CATALOG = os.getenv("DATABRICKS_CATALOG", "hive_metastore")
    DATABRICKS_SCHEMA = os.getenv("DATABRICKS_SCHEMA", "eod_task_monitor")

    @classmethod
    def db_table(cls, name: str) -> str:
        """Return fully-qualified Databricks table name: catalog.schema.table"""
        return f"{cls.DATABRICKS_CATALOG}.{cls.DATABRICKS_SCHEMA}.{name}"

    @classmethod
    def validate(cls):
        required = [
            ("AZURE_DEVOPS_ORG", cls.AZURE_ORG),
            ("AZURE_DEVOPS_PROJECT", cls.AZURE_PROJECT),
            ("AZURE_DEVOPS_PAT", cls.AZURE_PAT),
            ("GROQ_API_KEY", cls.GROQ_API_KEY),
            ("SMTP_USER", cls.SMTP_USER),
            ("SMTP_PASSWORD", cls.SMTP_PASSWORD),
            ("EMAIL_FROM", cls.EMAIL_FROM),
            ("EMAIL_TO", cls.EMAIL_TO),
            ("DATABRICKS_SERVER_HOSTNAME", cls.DATABRICKS_SERVER_HOSTNAME),
            ("DATABRICKS_HTTP_PATH", cls.DATABRICKS_HTTP_PATH),
            ("DATABRICKS_ACCESS_TOKEN", cls.DATABRICKS_ACCESS_TOKEN),
        ]
        missing = [name for name, val in required if not val]
        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}"
            )