# JD-Recovery Hybrid Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the daily scan gets a listing with no scraped description that still scores >4.0 on blind defaults, WebFetch the real JD and rescore it; if the fetch also fails, soft-downrank and flag it instead of trusting the blind score.

**Architecture:** A new isolated `fetch_jd` module wraps a WebFetch-enabled Claude Agent SDK call (Haiku) behind a sync, never-raising facade. A new pure `score.apply_unverified_penalty` helper mirrors the existing salary penalty. `cmd_scan` gets a small gated branch between the blind score and the salary penalty. The scorer itself stays deterministic and tool-free.

**Tech Stack:** Python 3.12, pytest, `claude_agent_sdk` (imported lazily inside the SDK call, never at module top or in tests).

**Spec:** [docs/superpowers/specs/2026-05-16-jd-recovery-hybrid-design.md](../specs/2026-05-16-jd-recovery-hybrid-design.md)

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `job_discovery/score.py` | Add `apply_unverified_penalty` (pure, mirrors `apply_salary_penalty`) | Modify |
| `job_discovery/fetch_jd.py` | WebFetch JD recovery: pure interpret helper + async SDK driver + sync facade | Create |
| `job_discovery/prompts/fetch_jd_system.txt` | System prompt for the fetch agent | Create |
| `job_discovery/cli.py` | Gated WebFetch+rescore branch in `cmd_scan`; import `fetch_jd` | Modify |
| `tests/test_score.py` | Tests for `apply_unverified_penalty` | Modify |
| `tests/test_fetch_jd.py` | Tests for interpret helper + facade error handling | Create |
| `tests/test_cli.py` | Integration tests for the gate; import `fetch_jd` | Modify |

All commands run from the repo root: `C:/Users/matis/Desktop/DevProjects/job-discovery`. Use the repo venv for pytest: `.venv/Scripts/python.exe -m pytest ...`.

---

### Task 1: `score.apply_unverified_penalty`

**Files:**
- Modify: `job_discovery/score.py` (add function after `apply_salary_penalty`, which ends at line 273)
- Test: `tests/test_score.py` (append; reuses existing `_mk_score_result` helper at line 158)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_score.py`:

```python
# -----------------------------------------------------------------------------
# Unverified-JD handling: high blind score we could not verify gets soft-
# downranked + flagged (mirrors the salary penalty contract).
# -----------------------------------------------------------------------------


def test_apply_unverified_penalty_reduces_overall_and_flags():
    result = score.apply_unverified_penalty(_mk_score_result(4.3))
    assert result["overall"] == 3.8
    assert "unverified" in result["one_line_take"].lower()


def test_apply_unverified_penalty_clamps_at_1():
    result = score.apply_unverified_penalty(_mk_score_result(1.2))
    assert result["overall"] == 1.0


def test_apply_unverified_penalty_flag_not_double_appended():
    once = score.apply_unverified_penalty(_mk_score_result(4.3))
    twice = score.apply_unverified_penalty(once)
    assert twice["one_line_take"].lower().count("unverified") == 1


def test_apply_unverified_penalty_does_not_mutate_input():
    original = _mk_score_result(4.3)
    score.apply_unverified_penalty(original)
    assert original["overall"] == 4.3
    assert "unverified" not in original["one_line_take"].lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_score.py -k unverified -v`
Expected: FAIL — `AttributeError: module 'job_discovery.score' has no attribute 'apply_unverified_penalty'`

- [ ] **Step 3: Implement the function**

In `job_discovery/score.py`, add this immediately after `apply_salary_penalty` (after line 273, before the `# LLM-based scoring` section header at line 276):

```python
def apply_unverified_penalty(score_result: dict) -> dict:
    """Soft penalty for a high blind-default score we could not verify.

    Triggered when a listing had no scraped description AND scored >4.0 on
    blind defaults AND the WebFetch JD recovery also came back empty. The
    high score is suspect (unknown dims — esp. seniority — defaulted high),
    so downrank it off the top of the brief without dropping it.

    Mirrors apply_salary_penalty: returns a NEW dict, does not mutate.
      - overall reduced by 0.5 (clamped to 1.0 min)
      - append the unverified flag to one_line_take (idempotent), capped 200
    """
    out = {**score_result, "dims": dict(score_result.get("dims", {}))}
    take = (out.get("one_line_take") or "").strip()
    out["overall"] = max(1.0, round(out.get("overall", 0.0) - 0.5, 1))
    flag = "⚠ unverified — JD unreadable"
    if flag not in take:
        take = (take + " — " + flag) if take else flag
        out["one_line_take"] = take[:200]
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_score.py -k unverified -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Run the full score test file (no regressions)**

Run: `.venv/Scripts/python.exe -m pytest tests/test_score.py -v`
Expected: PASS (all prior tests + 4 new)

- [ ] **Step 6: Commit**

```bash
git add job_discovery/score.py tests/test_score.py
git commit -m "feat(score): apply_unverified_penalty for unverifiable high scores"
```

---

### Task 2: `fetch_jd` module + system prompt

**Files:**
- Create: `job_discovery/prompts/fetch_jd_system.txt`
- Create: `job_discovery/fetch_jd.py`
- Create: `tests/test_fetch_jd.py`

- [ ] **Step 1: Create the system prompt file**

Create `job_discovery/prompts/fetch_jd_system.txt`:

```
You are a job-description extraction agent. You will be given a single job-posting URL.

Use the WebFetch tool exactly once on that URL. From the fetched page, extract and return ONLY the job description text: role summary, responsibilities, required and preferred qualifications, years of experience, seniority level, and any clearance or citizenship requirements.

Rules:
- Return the description as plain text. Do not add commentary, invented headings, or analysis.
- Do NOT score, judge, or assess fit. Relay the posting's own description only.
- If the page is a login or auth wall, an expired or removed posting, an empty page, or a JavaScript shell with no readable job description, respond with exactly this token and nothing else: NO_DESCRIPTION_AVAILABLE
- Never fabricate description text. If you are not confident real job-description content was retrieved, return NO_DESCRIPTION_AVAILABLE.
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_fetch_jd.py`:

```python
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_fetch_jd.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'job_discovery.fetch_jd'`

- [ ] **Step 4: Implement the module**

Create `job_discovery/fetch_jd.py`:

```python
"""WebFetch-based job-description recovery.

Public API:
  - fetch_job_description(url): sync facade. Returns recovered JD text, or
    None for any non-success (unreadable page, sentinel, timeout, SDK crash).
    NEVER raises — callers treat None as "could not verify".

Internals mirror score.score_llm's Claude Agent SDK setup (OAuth-forced,
file-based system prompt, bypassPermissions). The wrapping agent only calls
WebFetch and relays text, so it runs on Haiku — the extraction intelligence
lives in WebFetch's own model, not here.
"""
import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# The fetch agent returns this exact token when the page has no usable JD
# (login wall, expired, empty, JS shell). Kept in sync with the system
# prompt in prompts/fetch_jd_system.txt.
_NO_DESC_SENTINEL = "NO_DESCRIPTION_AVAILABLE"


def _interpret_fetch_output(raw: str | None) -> str | None:
    """Pure: turn the agent's raw text into recovered JD, or None.

    None when the output is empty/whitespace, or the no-description
    sentinel appears anywhere in it (the agent may wrap it in a sentence).
    """
    if not raw or not raw.strip():
        return None
    if _NO_DESC_SENTINEL in raw:
        return None
    return raw.strip()


async def _run_fetch_agent(url: str, timeout_s: float) -> str:
    """Run the WebFetch-enabled SDK agent; return raw assistant text.

    Mirrors score.score_llm's SDK orchestration. May raise (timeout, SDK
    error) — the sync facade is responsible for catching everything.
    """
    from claude_agent_sdk import (
        AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, TextBlock,
    )

    # File-based system prompt: dodge Windows' 32KB CreateProcess cmdline
    # limit (same pattern as score.score_llm).
    prompt_dir = Path(os.environ["VAULT_PATH"]) / ".mizzix_state"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    system_prompt_text = (
        Path(__file__).parent / "prompts" / "fetch_jd_system.txt"
    ).read_text(encoding="utf-8")
    system_path = prompt_dir / "job_discovery_fetch_jd_prompt.txt"
    system_path.write_text(system_prompt_text, encoding="utf-8")

    options = ClaudeAgentOptions(
        system_prompt={"type": "file", "path": str(system_path)},
        cwd=os.environ["VAULT_PATH"],
        allowed_tools=["WebFetch"],
        permission_mode="bypassPermissions",
        model=os.environ.get("MIZZIX_FETCH_MODEL", "claude-haiku-4-5"),
    )

    async def _drive() -> str:
        client = ClaudeSDKClient(options=options)
        await client.connect()
        try:
            await client.query(
                f"Fetch and extract the job description at: {url}"
            )
            chunks: list[str] = []
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
            return "".join(chunks)
        finally:
            await client.disconnect()

    return await asyncio.wait_for(_drive(), timeout=timeout_s)


def fetch_job_description(url: str, timeout_s: float = 45.0) -> str | None:
    """Recover a job description via WebFetch. None on any failure.

    Forces the Claude Max OAuth path (pops ANTHROPIC_API_KEY) so the fetch
    bills the subscription, not a stray key — same rule as score.score_llm.
    Never raises: timeout / SDK crash / unreadable page all collapse to None.
    """
    os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        raw = asyncio.run(_run_fetch_agent(url, timeout_s))
    except Exception:
        logger.warning(
            "fetch_job_description: fetch failed for %s", url, exc_info=True
        )
        return None
    return _interpret_fetch_output(raw)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_fetch_jd.py -v`
Expected: PASS (10 passed)

- [ ] **Step 6: Commit**

```bash
git add job_discovery/fetch_jd.py job_discovery/prompts/fetch_jd_system.txt tests/test_fetch_jd.py
git commit -m "feat(fetch_jd): WebFetch JD recovery module (Haiku, never-raises facade)"
```

---

### Task 3: Gate integration in `cmd_scan`

**Files:**
- Modify: `job_discovery/cli.py` (import line 17; the per-listing loop body around lines 162-167)
- Test: `tests/test_cli.py` (import line 5; append integration tests)

- [ ] **Step 1: Write the failing tests**

In `tests/test_cli.py`, change the import line (currently line 5):

```python
from job_discovery import cli, score, state, search
```

to:

```python
from job_discovery import cli, score, state, search, fetch_jd
```

Then append to `tests/test_cli.py`:

```python
# -----------------------------------------------------------------------------
# Hybrid JD-recovery gate in cmd_scan
# -----------------------------------------------------------------------------


def _write_criteria(tmp_path):
    crit_path = tmp_path / "projects" / "Job_Search" / "discovery" / "criteria.md"
    crit_path.parent.mkdir(parents=True, exist_ok=True)
    crit_path.write_text(
        "# Job Search Criteria\n\n## Roles\n- Mech Eng\n\n"
        "## Locations\n- Chicago, IL\n",
        encoding="utf-8",
    )


def _gate_listing(url, description=""):
    return {
        "title": "Mechanical Design Engineer", "company": "Akkodis",
        "location": "Denver, CO", "url": url, "salary": "$80K-$100K",
        "posted_date": "2026-05-16", "source": "linkedin",
        "description": description,
    }


def test_cmd_scan_gate_recovers_jd_and_rescores(tmp_path, monkeypatch):
    """No description + blind score >4 → WebFetch fires, listing is rescored
    on the recovered JD (here the rescore correctly drops it)."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    _write_criteria(tmp_path)
    listing = _gate_listing("https://linkedin.com/jobs/view/1")
    monkeypatch.setattr(
        search, "fetch_all",
        lambda criteria, results_per_board=50: ([listing], {"linkedin": "ok"}),
    )

    calls = {"score": 0}

    def fake_score_listing(l, c, p, b, model=None):
        calls["score"] += 1
        if l.get("description"):  # rescore on recovered JD → correct (low)
            return {"overall": 2.5, "dims": {"role_fit": 2, "skills_match": 2,
                    "seniority": 2, "domain": 2, "location": 2,
                    "responsibilities": 2},
                    "one_line_take": "senior, not a fit", "method": "llm"}
        return {"overall": 4.3, "dims": {"role_fit": 5, "skills_match": 4,
                "seniority": 5, "domain": 4, "location": 4,
                "responsibilities": 4},
                "one_line_take": "looks great", "method": "llm"}

    monkeypatch.setattr(score, "score_listing", fake_score_listing)
    monkeypatch.setattr(score, "apply_salary_penalty", lambda r, l, c: r)

    fetch_calls = []
    monkeypatch.setattr(
        fetch_jd, "fetch_job_description",
        lambda url, timeout_s=45.0: (fetch_calls.append(url)
                                     or "7-15 yrs, senior missile defense."),
    )

    rc = cli.cmd_scan(_Args(dry_run=False, top_n=5, threshold=3.0))
    assert rc == 0
    assert fetch_calls == ["https://linkedin.com/jobs/view/1"]
    assert calls["score"] == 2  # blind score + rescore
    assert state.load_matches() == []  # rescored 2.5 < 3.0 → not surfaced


def test_cmd_scan_gate_penalizes_when_fetch_fails(tmp_path, monkeypatch):
    """No description + blind score >4 + WebFetch returns None →
    apply_unverified_penalty: -0.5 and flagged, still surfaced."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    _write_criteria(tmp_path)
    listing = _gate_listing("https://linkedin.com/jobs/view/2")
    monkeypatch.setattr(
        search, "fetch_all",
        lambda criteria, results_per_board=50: ([listing], {"linkedin": "ok"}),
    )
    monkeypatch.setattr(
        score, "score_listing",
        lambda l, c, p, b, model=None: {
            "overall": 4.4, "dims": {"role_fit": 5, "skills_match": 4,
            "seniority": 5, "domain": 4, "location": 4,
            "responsibilities": 4},
            "one_line_take": "looks great", "method": "llm"},
    )
    monkeypatch.setattr(score, "apply_salary_penalty", lambda r, l, c: r)
    monkeypatch.setattr(
        fetch_jd, "fetch_job_description", lambda url, timeout_s=45.0: None
    )

    rc = cli.cmd_scan(_Args(dry_run=False, top_n=5, threshold=3.0))
    assert rc == 0
    matches = state.load_matches()
    assert len(matches) == 1
    assert matches[0]["score"]["overall"] == 3.9  # 4.4 - 0.5
    assert "unverified" in matches[0]["one_line_take"].lower()


def test_cmd_scan_gate_skips_when_description_present(tmp_path, monkeypatch):
    """Has a description → gate never fires even if score >4."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    _write_criteria(tmp_path)
    listing = _gate_listing("u", description="Full real description here.")
    monkeypatch.setattr(
        search, "fetch_all",
        lambda criteria, results_per_board=50: ([listing], {"linkedin": "ok"}),
    )
    monkeypatch.setattr(
        score, "score_listing",
        lambda l, c, p, b, model=None: {
            "overall": 4.6, "dims": {"role_fit": 5, "skills_match": 5,
            "seniority": 4, "domain": 4, "location": 5,
            "responsibilities": 4},
            "one_line_take": "great", "method": "llm"},
    )
    monkeypatch.setattr(score, "apply_salary_penalty", lambda r, l, c: r)
    called = []
    monkeypatch.setattr(
        fetch_jd, "fetch_job_description",
        lambda url, timeout_s=45.0: called.append(url),
    )

    rc = cli.cmd_scan(_Args(dry_run=False, top_n=5, threshold=3.0))
    assert rc == 0
    assert called == []  # description present → no fetch


def test_cmd_scan_gate_skips_when_score_not_above_4(tmp_path, monkeypatch):
    """No description but score is exactly 4.0 (not >4.0) → gate must NOT
    fire. Verifies the strict > boundary."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    _write_criteria(tmp_path)
    listing = _gate_listing("u")
    monkeypatch.setattr(
        search, "fetch_all",
        lambda criteria, results_per_board=50: ([listing], {"linkedin": "ok"}),
    )
    monkeypatch.setattr(
        score, "score_listing",
        lambda l, c, p, b, model=None: {
            "overall": 4.0, "dims": {"role_fit": 4, "skills_match": 4,
            "seniority": 4, "domain": 4, "location": 4,
            "responsibilities": 4},
            "one_line_take": "ok", "method": "llm"},
    )
    monkeypatch.setattr(score, "apply_salary_penalty", lambda r, l, c: r)
    called = []
    monkeypatch.setattr(
        fetch_jd, "fetch_job_description",
        lambda url, timeout_s=45.0: called.append(url),
    )

    rc = cli.cmd_scan(_Args(dry_run=False, top_n=5, threshold=3.0))
    assert rc == 0
    assert called == []  # 4.0 is not > 4.0 → no fetch
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli.py -k gate -v`
Expected: FAIL — `AttributeError: module 'job_discovery.fetch_jd' has no attribute 'fetch_job_description'` is already implemented (Task 2), so failures here are: `test_cmd_scan_gate_recovers_jd_and_rescores` asserts `calls["score"] == 2` but the unmodified `cmd_scan` scores once → AssertionError; `test_cmd_scan_gate_penalizes_when_fetch_fails` expects 3.9 but gets 4.4 → AssertionError. (The two skip tests may already pass since the gate doesn't exist yet — that's fine; Step 4 must keep them green.)

- [ ] **Step 3: Implement the gate**

In `job_discovery/cli.py`, change the import (line 17):

```python
from job_discovery import score, search, state
```

to:

```python
from job_discovery import fetch_jd, score, search, state
```

Then, in `cmd_scan`, replace this exact block (lines 162-167):

```python
        try:
            result = score.score_listing(listing, criteria, preferences, profile_blob)
            # Salary is a deterministic post-step (orchestrator-applied so both
            # LLM and rule-based paths get the same treatment): missing salary
            # gets flagged; below-floor gets soft-penalized 0.5.
            result = score.apply_salary_penalty(result, listing, criteria)
```

with:

```python
        try:
            result = score.score_listing(listing, criteria, preferences, profile_blob)
            # Hybrid JD recovery: a no-description listing that still scored
            # >4 is suspect — unknown dims (esp. seniority) defaulted high.
            # Spend a WebFetch to recover the real JD and rescore. If the
            # fetch also fails, soft-downrank + flag rather than trust the
            # blind score or silently drop a wall-blocked posting.
            if (not (listing.get("description") or "").strip()
                    and result["overall"] > 4.0):
                jd = fetch_jd.fetch_job_description(listing["url"])
                if jd:
                    listing["description"] = jd
                    result = score.score_listing(
                        listing, criteria, preferences, profile_blob
                    )
                else:
                    result = score.apply_unverified_penalty(result)
            # Salary is a deterministic post-step (orchestrator-applied so both
            # LLM and rule-based paths get the same treatment): missing salary
            # gets flagged; below-floor gets soft-penalized 0.5.
            result = score.apply_salary_penalty(result, listing, criteria)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli.py -k gate -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add job_discovery/cli.py tests/test_cli.py
git commit -m "feat(scan): gated WebFetch JD recovery + rescore in cmd_scan"
```

---

### Task 4: Full-suite verification + live smoke

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `.venv/Scripts/python.exe -m pytest -v`
Expected: PASS — all pre-existing tests plus the new `test_score.py` (4), `test_fetch_jd.py` (10), `test_cli.py` (4) tests. Zero failures.

- [ ] **Step 2: Confirm deployment model (no Mizzix restart needed)**

Run: `.venv/Scripts/python.exe -c "import job_discovery, pathlib; print(pathlib.Path(job_discovery.__file__).resolve())"`
Expected: a path **inside** `C:/Users/matis/Desktop/DevProjects/job-discovery/job_discovery/` → the package is installed editable, so the 3am `MizzixJobDiscovery` cron picks up these changes on its next run with no reinstall, no Mizzix bot restart, and no dashboard version bump (this lives in a separate repo from the Mizzix bot; no vault/personality/skill files were touched).
If the path points into `site-packages` instead, reinstall before relying on the cron: `.venv/Scripts/python.exe -m pip install -e .`

- [ ] **Step 3: Live smoke against the real regression case (manual, not CI)**

This makes one real WebFetch (Haiku) call. Requires `VAULT_PATH` set.

Run:
```bash
VAULT_PATH="C:/Users/matis/Desktop/Second Brain" .venv/Scripts/python.exe -c "from job_discovery import fetch_jd; print((fetch_jd.fetch_job_description('https://www.linkedin.com/jobs/view/4412004839') or 'NONE')[:600])"
```
Expected: real Akkodis JD text mentioning roughly "7–15+ years" and senior/mid-senior level (matches the 2026-05-16 spike). If it returns `NONE`, that is an acceptable graceful outcome (the gate's `apply_unverified_penalty` fallback covers it) — note it but it is not a failure of this implementation.

- [ ] **Step 4: Final commit (plan completion marker, if any working-tree changes remain)**

```bash
git status --porcelain
```
If clean, nothing to do — Tasks 1-3 already committed. If anything is staged/modified, review before committing.

---

## Self-Review

**1. Spec coverage:**
- Gate criterion 1 (empty description) → Task 3 condition `not (listing.get("description") or "").strip()`; tested by `test_cmd_scan_gate_skips_when_description_present`. ✓
- Gate criterion 2 (strictly >4.0) → Task 3 condition `result["overall"] > 4.0`; boundary tested by `test_cmd_scan_gate_skips_when_score_not_above_4`. ✓
- WebFetch success → set description + rescore → Task 3; tested by `test_cmd_scan_gate_recovers_jd_and_rescores`. ✓
- WebFetch failure → soft −0.5 + flag, still surfaced → Task 1 (`apply_unverified_penalty`) + Task 3 wiring; tested by `test_apply_unverified_penalty_*` and `test_cmd_scan_gate_penalizes_when_fetch_fails` (asserts 3.9 ≥ 3.0 → still in matches). ✓
- `fetch_jd.py` isolated, OAuth-forced, Haiku, file-based prompt, never-raises, timeout → Task 2; tested by `test_fetch_jd.py`. ✓
- Scorer stays tool-free/deterministic → no change to `score_llm`/rule-based; only an additive pure helper. ✓
- No-double-fetch via existing carry-forward → unchanged behavior; no task needed (documented in spec). ✓
- 3am robustness (per-listing try/except is outer net) → gate code sits inside the existing `try` at cli.py:162; preserved. ✓

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to". Every code step has complete code. ✓

**3. Type consistency:** `fetch_jd.fetch_job_description(url, timeout_s=45.0) -> str | None` and `fetch_jd._interpret_fetch_output` / `fetch_jd._run_fetch_agent` names are identical across Task 2 definition and Task 3 usage and all tests. `score.apply_unverified_penalty(score_result) -> dict` identical across Task 1 definition, Task 3 call, and tests. The flag substring `"unverified"` asserted in tests matches the literal in the implementation (`⚠ unverified — JD unreadable`). ✓
