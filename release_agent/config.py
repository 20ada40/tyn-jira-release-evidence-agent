"""Environment-driven settings. Credentials are read from the environment / .env only."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_MODEL = "claude-opus-5-5"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class JiraSettings:
    base_url: str
    email: str
    api_token: str


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Missing environment variable {name}. Copy .env.example to .env and fill it in."
        )
    return value


def load_jira_settings() -> JiraSettings:
    load_dotenv()
    return JiraSettings(
        base_url=_require("JIRA_BASE_URL").rstrip("/"),
        email=_require("JIRA_EMAIL"),
        api_token=_require("JIRA_API_TOKEN"),
    )


def default_project() -> str | None:
    load_dotenv()
    return os.environ.get("JIRA_PROJECT_KEY") or None


def agent_model() -> str:
    load_dotenv()
    return os.environ.get("AGENT_MODEL") or DEFAULT_MODEL
