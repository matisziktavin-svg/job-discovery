import json
from pathlib import Path

import pytest

from job_discovery import state


def test_load_matches_returns_empty_list_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    assert state.load_matches() == []


def test_save_then_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    items = [
        {"id": "jm_abc12345", "title": "Mech Eng", "company": "Acme", "status": "surfaced"},
    ]
    state.save_matches(items)
    assert state.load_matches() == items


def test_load_matches_returns_empty_on_corrupt_json(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    path = tmp_path / ".mizzix_state" / "job_matches.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert state.load_matches() == []
    # File must be left alone — Tavin may hand-edit
    assert path.read_text(encoding="utf-8") == "{not valid json"


def test_save_matches_writes_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    state.save_matches([{"id": "jm_111"}])
    state.save_matches([{"id": "jm_222"}])
    # No leftover .tmp file
    assert not (tmp_path / ".mizzix_state" / "job_matches.json.tmp").exists()
    assert state.load_matches() == [{"id": "jm_222"}]


def test_history_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    state.save_history([{"id": "jm_999", "status": "passed"}])
    assert state.load_history() == [{"id": "jm_999", "status": "passed"}]


def test_new_match_id_format():
    mid = state.new_match_id()
    assert mid.startswith("jm_")
    assert len(mid) == 11  # "jm_" + 8 hex chars
