"""
utils/secrets.py

Gap addressed: "Security & Secrets" (High) — PATs and Databricks tokens were
loaded only from a flat .env file with no rotation, expiry enforcement, or
secret-scanning guard.

This module lets config.py pull secrets from Azure Key Vault when
KEY_VAULT_URL is set, falling back to .env / process environment otherwise.
This is opt-in and backward compatible: if KEY_VAULT_URL is unset, behavior
is identical to before (plain os.getenv from .env).

Deployment notes (see README "Secrets & Key Vault" section for full
walkthrough):
  - Create an Azure Key Vault and store each secret under a name matching
    the env var with underscores replaced by hyphens, e.g.
    AZURE_DEVOPS_PAT -> azure-devops-pat.
  - Grant the pipeline's managed identity or service principal the
    "Key Vault Secrets User" role (RBAC) or an access policy with
    get/list permissions.
  - Set KEY_VAULT_URL=https://<your-vault-name>.vault.azure.net/ in the
    deployment environment (NOT in committed .env files).
  - Credential resolution uses azure-identity's DefaultAzureCredential,
    which transparently picks up managed identity in Azure, or
    `az login` / environment credentials locally.
"""

import os
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

KEY_VAULT_URL = os.getenv("KEY_VAULT_URL", "").strip()


@lru_cache(maxsize=1)
def _get_secret_client():
    """Lazily construct the Key Vault client. Cached for the process lifetime."""
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    credential = DefaultAzureCredential()
    return SecretClient(vault_url=KEY_VAULT_URL, credential=credential)


@lru_cache(maxsize=64)
def _fetch_from_vault(secret_name: str) -> str | None:
    try:
        client = _get_secret_client()
        return client.get_secret(secret_name).value
    except Exception as exc:
        logger.warning("Key Vault lookup failed for '%s': %s", secret_name, exc)
        return None


def get_secret(env_var_name: str, default: str | None = None) -> str | None:
    """
    Resolve a secret value:
      1. If KEY_VAULT_URL is configured, try Key Vault first
         (secret name = env var name, lowercased, underscores -> hyphens).
      2. Fall back to the process environment / .env (python-dotenv already
         loaded it by the time config.py calls this).
      3. Fall back to `default`.
    """
    if KEY_VAULT_URL:
        vault_secret_name = env_var_name.lower().replace("_", "-")
        value = _fetch_from_vault(vault_secret_name)
        if value:
            return value

    return os.getenv(env_var_name, default)


def using_key_vault() -> bool:
    return bool(KEY_VAULT_URL)
