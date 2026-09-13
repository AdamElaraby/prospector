"""Reading and writing your config (config.local.yaml) and .env from the web UI.

config.yaml is full of explanatory comments that are worth more than the values
they document, so edits go through ruamel's round-trip loader rather than a
plain dump, which would flatten every comment on first save.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from .config import ROOT

def _writable_config() -> Path:
    """Where the web UI saves. Always the private local file: saving your name
    or phone into the committed template would publish it with the next push.
    First save creates config.local.yaml from the template."""
    from .config import LOCAL_CONFIG, TEMPLATE_CONFIG
    import shutil

    override = os.environ.get("PROSPECTOR_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    if not LOCAL_CONFIG.exists():
        shutil.copyfile(TEMPLATE_CONFIG, LOCAL_CONFIG)
    return LOCAL_CONFIG
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"

KNOWN_KEYS = ("GOOGLE_PLACES_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "RESEND_API_KEY")

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096
# Match config.yaml's existing style, otherwise saving one field reflows every
# list in the file and the diff is unreadable.
_yaml.indent(mapping=2, sequence=4, offset=2)


def load_doc(path: Path | None = None):
    from .config import default_config_path

    path = path or default_config_path()
    with path.open() as fh:
        return _yaml.load(fh)


def save_doc(doc, path: Path | None = None) -> None:
    path = path or _writable_config()
    with path.open("w") as fh:
        _yaml.dump(doc, fh)


def update_config(updates: dict[str, Any], path: Path | None = None) -> list[str]:
    """Apply {"sender.name": "...", "search.niches": [...]} to config.local.yaml.

    Returns the dotted keys actually changed. Unknown paths are ignored rather
    than silently creating new branches the rest of the code never reads.
    """
    path = path or _writable_config()
    doc = load_doc(path)
    changed: list[str] = []

    for dotted, value in updates.items():
        parts = dotted.split(".")
        node = doc
        ok = True
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                ok = False
                break
            node = node[part]
        leaf = parts[-1]
        if not ok or not isinstance(node, dict) or leaf not in node:
            continue
        if node[leaf] != value:
            node[leaf] = value
            changed.append(dotted)

    if changed:
        save_doc(doc, path)
    return changed


# --------------------------------------------------------------------------- env


def read_env_keys(path: Path | None = None) -> dict[str, bool]:
    """Which keys are set. Never returns the values themselves."""
    path = path or ENV_PATH
    present = {key: False for key in KNOWN_KEYS}
    if not path.exists():
        # A key exported in the shell counts as set too.
        for key in KNOWN_KEYS:
            present[key] = bool(os.environ.get(key))
        return present

    text = path.read_text(encoding="utf-8", errors="replace")
    for key in KNOWN_KEYS:
        match = re.search(rf"^\s*{key}\s*=\s*(.*)$", text, re.M)
        value = (match.group(1).strip().strip("'\"") if match else "") or os.environ.get(key, "")
        present[key] = bool(value)
    return present


def write_env_keys(values: dict[str, str], path: Path | None = None) -> list[str]:
    """Set or replace keys in .env, leaving other lines and comments alone.

    An empty string clears a key; a key omitted from `values` is left untouched,
    so the UI can save one field without wiping the others.
    """
    path = path or ENV_PATH
    if not path.exists():
        text = ENV_EXAMPLE.read_text(encoding="utf-8") if ENV_EXAMPLE.exists() else ""
    else:
        text = path.read_text(encoding="utf-8", errors="replace")

    written: list[str] = []
    for key, value in values.items():
        if key not in KNOWN_KEYS:
            continue
        value = (value or "").strip()
        line = f"{key}={value}"
        pattern = rf"^\s*{key}\s*=.*$"
        if re.search(pattern, text, re.M):
            text = re.sub(pattern, line, text, count=1, flags=re.M)
        else:
            text = text.rstrip("\n") + f"\n{line}\n"
        written.append(key)
        # Keep the running process in sync so a job started right after saving
        # picks the key up without a restart.
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)

    path.write_text(text, encoding="utf-8")
    # Secrets file: owner read/write only.
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return written
