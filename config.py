"""Navo configuration. Secrets come from the environment, never from the client."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.app_name = "Navo"
        self.secret_key = _str("SECRET_KEY")
        self.admin_password = _str("ADMIN_PASSWORD")
        self.app_db_path = _str("APP_DB_PATH", str(ROOT / "data" / "navo.sqlite"))
        self.hosted_db_dir = _str("HOSTED_DB_DIR", str(ROOT / "data" / "hosted"))
        self.base_url = _str("BASE_URL")
        self.contact_email = _str("CONTACT_EMAIL")
        self.discord_username = _str("DISCORD_USERNAME", "tsun_106")
        self.discord_server_url = _str("DISCORD_SERVER_URL")
        self.discord_profile_url = _str("DISCORD_PROFILE_URL")
        self.google_client_id = _str("GOOGLE_CLIENT_ID")
        self.google_client_secret = _str("GOOGLE_CLIENT_SECRET")
        self.turso_api_token = _str("TURSO_API_TOKEN")
        self.turso_organization = _str("TURSO_ORGANIZATION")
        self.turso_group = _str("TURSO_GROUP", "default")
        self.turso_api_base = _str("TURSO_API_BASE", "https://api.turso.tech/v1")
        self.session_https_only = _bool("SESSION_HTTPS_ONLY", False)
        self.query_timeout_seconds = int(os.getenv("QUERY_TIMEOUT_SECONDS", "8"))
        self.query_row_limit = int(os.getenv("QUERY_ROW_LIMIT", "500"))
        self.testing = _bool("NAVO_TESTING", False)

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def external_hosting_enabled(self) -> bool:
        return bool(self.turso_api_token and self.turso_organization)

    def public(self) -> dict:
        return {
            "app_name": self.app_name,
            "contact_email": self.contact_email,
            "discord_username": self.discord_username,
            "discord_server_url": self.discord_server_url,
            "discord_profile_url": self.discord_profile_url,
            "google_enabled": self.google_enabled,
        }


settings = Settings()


def reload_settings() -> Settings:
    global settings
    settings = Settings()
    return settings
