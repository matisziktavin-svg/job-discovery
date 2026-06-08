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


# --- tier 2 (playwright) fallback -------------------------------------------

def test_tier2_called_when_tier1_returns_none(monkeypatch):
    async def fake_run(url, timeout_s):
        return "NO_DESCRIPTION_AVAILABLE"

    calls: list[tuple[str, float]] = []

    def fake_playwright(url, timeout_s):
        calls.append((url, timeout_s))
        return "Real rendered JD text from headless chromium. " * 20

    monkeypatch.setattr(fetch_jd, "_run_fetch_agent", fake_run)
    monkeypatch.setattr(fetch_jd, "_fetch_via_playwright", fake_playwright)

    result = fetch_jd.fetch_job_description("https://x/job/1")
    assert result is not None and "Real rendered JD text" in result
    assert calls and calls[0][0] == "https://x/job/1"


def test_tier2_not_called_when_tier1_succeeds(monkeypatch):
    async def fake_run(url, timeout_s):
        return "Tier 1 returned a real JD."

    called = False

    def fake_playwright(url, timeout_s):
        nonlocal called
        called = True
        return "should not be used"

    monkeypatch.setattr(fetch_jd, "_run_fetch_agent", fake_run)
    monkeypatch.setattr(fetch_jd, "_fetch_via_playwright", fake_playwright)

    result = fetch_jd.fetch_job_description("https://x/job/1")
    assert result == "Tier 1 returned a real JD."
    assert called is False


def test_tier2_called_when_tier1_raises(monkeypatch):
    async def fake_run(url, timeout_s):
        raise RuntimeError("SDK exploded")

    def fake_playwright(url, timeout_s):
        return "Recovered via browser." * 20

    monkeypatch.setattr(fetch_jd, "_run_fetch_agent", fake_run)
    monkeypatch.setattr(fetch_jd, "_fetch_via_playwright", fake_playwright)

    result = fetch_jd.fetch_job_description("https://x/job/1")
    assert result is not None and "Recovered via browser." in result


def test_both_tiers_failing_returns_none(monkeypatch):
    async def fake_run(url, timeout_s):
        return "NO_DESCRIPTION_AVAILABLE"

    def fake_playwright(url, timeout_s):
        return None

    monkeypatch.setattr(fetch_jd, "_run_fetch_agent", fake_run)
    monkeypatch.setattr(fetch_jd, "_fetch_via_playwright", fake_playwright)

    assert fetch_jd.fetch_job_description("https://x/job/1") is None


def test_tier2_returns_none_when_playwright_not_installed(monkeypatch):
    # Simulate the ImportError path by hiding playwright.sync_api.
    import sys
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    # Reset the warn-once flag so the test is order-independent.
    monkeypatch.setattr(fetch_jd, "_playwright_unavailable_warned", False)
    assert fetch_jd._fetch_via_playwright("https://x/job/1", timeout_s=5.0) is None


def test_tier2_returns_none_on_short_render(monkeypatch):
    # Stub sync_playwright to return a context manager yielding a fake
    # playwright API whose body innerText is too short to be a real JD.
    import contextlib

    class _Body:
        @staticmethod
        def goto(*a, **kw): pass
        @staticmethod
        def wait_for_load_state(*a, **kw): pass
        @staticmethod
        def inner_text(sel): return "Sign in to view"

    class _Page(_Body): pass

    class _Context:
        def new_page(self): return _Page()

    class _Browser:
        def new_context(self, **kw): return _Context()
        def close(self): pass

    class _Chromium:
        def launch(self, **kw): return _Browser()

    class _PW:
        chromium = _Chromium()

    @contextlib.contextmanager
    def fake_sync_playwright():
        yield _PW()

    import sys
    fake_module = type(sys)("playwright.sync_api")
    fake_module.sync_playwright = fake_sync_playwright  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", type(sys)("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)

    assert fetch_jd._fetch_via_playwright("https://x/job/1", timeout_s=5.0) is None
