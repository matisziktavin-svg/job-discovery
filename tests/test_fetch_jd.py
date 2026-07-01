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


# --- tier 2 (firecrawl) fallback --------------------------------------------

def test_tier2_called_when_tier1_returns_none(monkeypatch):
    async def fake_run(url, timeout_s):
        return "NO_DESCRIPTION_AVAILABLE"

    calls: list[tuple[str, float]] = []

    def fake_firecrawl(url, timeout_s):
        calls.append((url, timeout_s))
        return "Real JD markdown from firecrawl. " * 20

    monkeypatch.setattr(fetch_jd, "_run_fetch_agent", fake_run)
    monkeypatch.setattr(fetch_jd, "_fetch_via_firecrawl", fake_firecrawl)

    result = fetch_jd.fetch_job_description("https://x/job/1")
    assert result is not None and "Real JD markdown" in result
    assert calls and calls[0][0] == "https://x/job/1"


def test_tier2_not_called_when_tier1_succeeds(monkeypatch):
    async def fake_run(url, timeout_s):
        return "Tier 1 returned a real JD."

    called = False

    def fake_firecrawl(url, timeout_s):
        nonlocal called
        called = True
        return "should not be used"

    monkeypatch.setattr(fetch_jd, "_run_fetch_agent", fake_run)
    monkeypatch.setattr(fetch_jd, "_fetch_via_firecrawl", fake_firecrawl)

    result = fetch_jd.fetch_job_description("https://x/job/1")
    assert result == "Tier 1 returned a real JD."
    assert called is False


def test_tier2_called_when_tier1_raises(monkeypatch):
    async def fake_run(url, timeout_s):
        raise RuntimeError("SDK exploded")

    def fake_firecrawl(url, timeout_s):
        return "Recovered via firecrawl." * 20

    monkeypatch.setattr(fetch_jd, "_run_fetch_agent", fake_run)
    monkeypatch.setattr(fetch_jd, "_fetch_via_firecrawl", fake_firecrawl)

    result = fetch_jd.fetch_job_description("https://x/job/1")
    assert result is not None and "Recovered via firecrawl." in result


def test_both_tiers_failing_returns_none(monkeypatch):
    async def fake_run(url, timeout_s):
        return "NO_DESCRIPTION_AVAILABLE"

    def fake_firecrawl(url, timeout_s):
        return None

    monkeypatch.setattr(fetch_jd, "_run_fetch_agent", fake_run)
    monkeypatch.setattr(fetch_jd, "_fetch_via_firecrawl", fake_firecrawl)

    assert fetch_jd.fetch_job_description("https://x/job/1") is None


# --- tier 2 internals: markdown extraction + guards -------------------------

def test_extract_markdown_from_object():
    class _Doc:
        markdown = "# JD\nreal content"
    assert fetch_jd._extract_markdown(_Doc()) == "# JD\nreal content"


def test_extract_markdown_from_dict():
    assert fetch_jd._extract_markdown({"markdown": "hello"}) == "hello"


def test_extract_markdown_none_when_absent():
    assert fetch_jd._extract_markdown({"html": "<p>x</p>"}) is None
    assert fetch_jd._extract_markdown(None) is None


def test_tier2_returns_none_when_firecrawl_not_installed(monkeypatch):
    # Simulate the ImportError path by hiding the firecrawl module.
    import sys
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setitem(sys.modules, "firecrawl", None)
    monkeypatch.setattr(fetch_jd, "_firecrawl_unavailable_warned", False)
    assert fetch_jd._fetch_via_firecrawl("https://x/job/1", timeout_s=5.0) is None


def test_tier2_returns_none_when_api_key_unset(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setattr(fetch_jd, "_firecrawl_unavailable_warned", False)
    assert fetch_jd._fetch_via_firecrawl("https://x/job/1", timeout_s=5.0) is None


def test_tier2_returns_none_on_short_scrape(monkeypatch):
    # Stub the firecrawl module so Firecrawl(...).scrape() yields markdown
    # too short to be a real JD.
    import sys
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    class _Client:
        def __init__(self, *a, **kw): pass
        def scrape(self, url, **kw): return {"markdown": "Sign in to view"}

    fake_module = type(sys)("firecrawl")
    fake_module.Firecrawl = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "firecrawl", fake_module)
    monkeypatch.setattr(fetch_jd, "_firecrawl_unavailable_warned", False)

    assert fetch_jd._fetch_via_firecrawl("https://x/job/1", timeout_s=5.0) is None


def test_tier2_returns_markdown_on_good_scrape(monkeypatch):
    import sys
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    good = "# Mechanical Design Engineer\n" + ("Responsibilities and reqs. " * 30)

    class _Client:
        def __init__(self, *a, **kw): pass
        def scrape(self, url, **kw): return {"markdown": good}

    fake_module = type(sys)("firecrawl")
    fake_module.Firecrawl = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "firecrawl", fake_module)

    result = fetch_jd._fetch_via_firecrawl("https://x/job/1", timeout_s=5.0)
    assert result is not None and "Mechanical Design Engineer" in result


def test_tier2_returns_none_when_scrape_raises(monkeypatch):
    import sys
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    class _Client:
        def __init__(self, *a, **kw): pass
        def scrape(self, url, **kw): raise RuntimeError("blocked")

    fake_module = type(sys)("firecrawl")
    fake_module.Firecrawl = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "firecrawl", fake_module)

    assert fetch_jd._fetch_via_firecrawl("https://x/job/1", timeout_s=5.0) is None
