"""State I/O for job_matches.json and job_matches_history.json.

Mirrors Mizzix's follow_ups.py patterns:
  - atomic write via .tmp + replace()
  - corruption-tolerant load (returns [], leaves file alone)
  - never destructive on parse failure (Tavin may hand-edit)

VAULT_PATH env var must point at the Second Brain vault root.
"""
import json
import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)


def _vault() -> Path:
    return Path(os.environ["VAULT_PATH"])


def _matches_path() -> Path:
    return _vault() / ".mizzix_state" / "job_matches.json"


def _history_path() -> Path:
    return _vault() / ".mizzix_state" / "job_matches_history.json"


def new_match_id() -> str:
    return "jm_" + secrets.token_hex(4)


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("%s corrupt — leaving file alone, returning []", path.name)
        return []
    return items if isinstance(items, list) else []


def _save(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_matches() -> list[dict]:
    return _load(_matches_path())


def save_matches(items: list[dict]) -> None:
    _save(_matches_path(), items)


def load_history() -> list[dict]:
    return _load(_history_path())


def save_history(items: list[dict]) -> None:
    _save(_history_path(), items)
