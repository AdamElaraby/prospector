"""Config loading. YAML for campaign settings, .env for secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

SOURCE_ROOT = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get("PROSPECTOR_DATA_DIR", SOURCE_ROOT)).resolve()

load_dotenv(ROOT / ".env")


class ConfigError(RuntimeError):
    pass


TEMPLATE_CONFIG = SOURCE_ROOT / "config.yaml"
LOCAL_CONFIG = ROOT / "config.local.yaml"


def default_config_path() -> Path:
    """Which config is in charge, in priority order:

    1. $PROSPECTOR_CONFIG — run a second campaign (or a demo) side by side.
       Inherited by the web UI's background jobs, so they stay on it too.
    2. config.local.yaml — gitignored, your real settings.
    3. config.yaml — the committed template.

    Keeping 2 apart from 3 is what stops a personal phone number or campaign
    from ending up in a public commit."""
    override = os.environ.get("PROSPECTOR_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    return LOCAL_CONFIG if LOCAL_CONFIG.exists() else TEMPLATE_CONFIG


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)
    path: Path = field(default_factory=default_config_path)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        value = self.get(dotted)
        if value in (None, "", []):
            raise ConfigError(f"{self.path.name} is missing a value for '{dotted}'")
        return value

    @property
    def db_path(self) -> Path:
        return ROOT / str(self.get("database", "prospector.db"))

    def check_sender_ready(self) -> list[str]:
        """What still blocks drafting. Deliberately just one thing: your name.

        You do not need a company, a domain, or an address to message someone
        about building them a website — those are email's requirements, and
        they are checked separately, only when email is actually being used.
        """
        name = self.get("sender.name")
        if not name or name == "Your Name":
            return ["sender.name"]
        return []

    @property
    def email_enabled(self) -> bool:
        return bool(self.get("email_channel.enabled", False))

    def check_email_ready(self) -> list[str]:
        """What email specifically needs, over and above the above.

        The postal address is not optional politeness: US CAN-SPAM requires a
        real physical address in commercial email, individuals included.
        """
        problems = []
        for key in ("email_channel.from_email", "email_channel.postal_address"):
            value = self.get(key)
            if not value or "@yourdomain" in str(value) or "Example St" in str(value):
                problems.append(key)
        return problems


def load_config(path: Path | None = None) -> Config:
    path = path or default_config_path()
    if not path.exists():
        raise ConfigError(f"No config file at {path}")
    with path.open() as fh:
        raw = yaml.safe_load(fh) or {}
    return Config(raw=raw, path=path)


def env(name: str, required: bool = False) -> str | None:
    value = os.environ.get(name) or None
    if required and not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def out_dir() -> Path:
    d = ROOT / "out"
    d.mkdir(exist_ok=True)
    return d
