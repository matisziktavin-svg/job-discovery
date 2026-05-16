import asyncio
import os

from job_discovery import fetch_jd


# --- pure interpret helper ---------------------------------------------------

def test_interpret_returns_text_on_normal_output():
    assert (
        fetch_jd._interpret_fetch_output("7+ years required. Senior role.")
        == "7+ years required. Senior role."
    )


def test_interpret_strips_whitespace():
    assert fetch_jd._interpret_fetch_output("  hello desc  ") == "hello desc"


def test_interpret_returns_none_on_sentinel():
    assert fetch_jd._interpret_fetch_output("Sorry, NO_DESCRIPTION_AVAILABLE") is None


def test_interpret_returns_none_on_empty():
    assert fetch_jd._interpret_fetch_output("") is None
    assert fetch_jd._interpret_fetch_output("   \n ") is None
    assert fetch_jd._interpret_fetch_output(None) is None


# --- sync facade error handling (mock the async SDK driver) ------------------

def test_fetch_returns_interpreted_text(monkeypatch):
    async def fake_run(url, timeout_s):
        return "  Mechanical design, 7-15 years.  "
    monkeypatch.setattr(fetch_jd, "_run_fetch_agent", fake_run)
    assert (
        fetch_jd.fetch_job_description("https://x/job/1")
        == "Mechanical design, 7-15 years."
    )


def test_fetch_returns_none_on_sentinel(monkeypatch):
    async def fake_run(url, timeout_s):
        return "NO_DESCRIPTION_AVAILABLE"
    monkeypatch.setattr(fetch_jd, "_run_fetch_agent", fake_run)
    assert fetch_jd.fetch_job_description("https://x/job/1") is None


def test_fetch_returns_none_on_exception(monkeypatch):
    async def fake_run(url, timeout_s):
        raise RuntimeError("SDK exploded")
    monkeypatch.setattr(fetch_jd, "_run_fetch_agent", fake_run)
    assert fetch_jd.fetch_job_description("https://x/job/1") is None


def test_fetch_returns_none_on_timeout(monkeypatch):
    async def fake_run(url, timeout_s):
        raise asyncio.TimeoutError()
    monkeypatch.setattr(fetch_jd, "_run_fetch_agent", fake_run)
    assert fetch_jd.fetch_job_description("https://x/job/1") is None


def test_fetch_pops_anthropic_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-removed")

    async def fake_run(url, timeout_s):
        return "ok desc"
    monkeypatch.setattr(fetch_jd, "_run_fetch_agent", fake_run)
    fetch_jd.fetch_job_description("https://x/job/1")
    assert "ANTHROPIC_API_KEY" not in os.environ
