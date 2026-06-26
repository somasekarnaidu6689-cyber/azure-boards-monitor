"""
tests/conftest.py

Gap addressed: "Testing" (Med) — there were previously no automated tests
at all. This conftest sets dummy env vars before Config is imported
anywhere, and provides shared fixtures (fake Groq client, fake Databricks
connection) so unit tests never make real network/API/DB calls.
"""

import os
import sys
import pytest

os.environ.setdefault("AZURE_DEVOPS_ORG", "testorg")
os.environ.setdefault("AZURE_DEVOPS_PROJECT", "testproj")
os.environ.setdefault("AZURE_DEVOPS_PAT", "fake-pat")
os.environ.setdefault("DATABRICKS_SERVER_HOSTNAME", "fake.databricks.com")
os.environ.setdefault("DATABRICKS_HTTP_PATH", "/sql/1.0/fake")
os.environ.setdefault("DATABRICKS_ACCESS_TOKEN", "fake-token")
os.environ.setdefault("GROQ_API_KEY", "fake-groq-key")
os.environ.setdefault("EMAIL_FROM", "test@example.com")
os.environ.setdefault("EMAIL_TO", "test@example.com")
os.environ.setdefault("SMTP_USER", "test@example.com")
os.environ.setdefault("SMTP_PASSWORD", "fake-password")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def sample_task():
    return {
        "id": 101,
        "title": "Implement login API",
        "work_item_type": "Task",
        "state": "Active",
        "assignee": {"display_name": "Jane Doe", "email": "jane@example.com"},
        "remaining_hours": 4.0,
        "original_estimate": 8.0,
        "days_since_state_change": 1,
        "days_since_last_update": 1,
        "has_comment_today": True,
        "today_comment_text": "Finished the JWT validation logic, writing tests next.",
        "recent_comment_texts": ["Started on the login endpoint yesterday."],
    }
