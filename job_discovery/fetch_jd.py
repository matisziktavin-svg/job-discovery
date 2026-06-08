"""Tiered job-description recovery.

Public API:
  - fetch_job_description(url): sync facade. Returns recovered JD text, or
    None for any non-success (unreadable page, sentinel, timeout, SDK crash).
    NEVER raises — callers treat None as "could not verify".

Recovery is staged:
  Tier 1 — WebFetch via the Claude Agent SDK (Haiku). Fast and free when it
    works; defeated by JS-rendered SPAs (Workday, Greenhouse, Lever, iCIMS).
  Tier 2 — Playwright headless Chromium render → grab body innerText. Cracks
    SPAs that tier 1 can't see. No extra LLM call: the downstream batched
    rescore already does extraction-via-LLM against the recovered text.

Tier 1 internals mirror score.score_llm's Claude Agent SDK setup (OAuth-forced,
file-based system prompt, bypassPermissions). The wrapping agent only calls
WebFetch and relays text, so it runs on Haiku — the extraction intelligence
lives in WebFetch's own model, not here.

Tier 2 requires `playwright` and its Chromium install:
    pip install playwright && playwright install chromium
If Playwright is not importable, tier 2 is silently skipped (logged once per
process) and fetch behaves as tier-1-only.
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

# Tier 2 (Playwright) trim and floor.
_TIER2_MAX_CHARS = 30_000
_TIER2_MIN_CHARS = 200

# Whether we've already warned about Playwright being unavailable. Avoids
# spamming the log once per recovery attempt — one line per process is enough.
_playwright_unavailable_warned = False


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


def _fetch_via_playwright(url: str, timeout_s: float) -> str | None:
    """Tier 2: render with headless Chromium, return body innerText or None.

    Never raises. Returns None when:
      - Playwright (or its browser) isn't installed.
      - Navigation, render, or extraction errors out.
      - The recovered text is too short to be a real JD (<_TIER2_MIN_CHARS).

    Output is trimmed to _TIER2_MAX_CHARS to keep the downstream LLM rescore
    cheap; nav/footer noise is left in — the scorer handles it.
    """
    global _playwright_unavailable_warned
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        if not _playwright_unavailable_warned:
            logger.info(
                "fetch_job_description: tier 2 skipped — playwright not installed"
            )
            _playwright_unavailable_warned = True
        return None

    timeout_ms = int(timeout_s * 1000)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    )
                )
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                # Best-effort networkidle wait — SPAs need it to finish XHRs,
                # but plenty of pages never reach idle. Treat the timeout as
                # "good enough, take what's rendered".
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                text = page.inner_text("body")
            finally:
                browser.close()
    except Exception:
        logger.warning(
            "fetch_job_description: tier 2 playwright failed for %s",
            url, exc_info=True,
        )
        return None

    if not text:
        return None
    text = text.strip()
    if len(text) < _TIER2_MIN_CHARS:
        return None
    if len(text) > _TIER2_MAX_CHARS:
        text = text[:_TIER2_MAX_CHARS]
    return text


def fetch_job_description(url: str, timeout_s: float = 45.0) -> str | None:
    """Recover a job description. None on any failure.

    Tries tier 1 (WebFetch via SDK) first; on None, falls back to tier 2
    (Playwright render). Forces the Claude Max OAuth path (pops
    ANTHROPIC_API_KEY) so the SDK fetch bills the subscription, not a stray
    key — same rule as score.score_llm. Never raises: every failure path
    collapses to None.
    """
    os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        raw = asyncio.run(_run_fetch_agent(url, timeout_s))
    except Exception:
        logger.warning(
            "fetch_job_description: tier 1 fetch failed for %s",
            url, exc_info=True,
        )
        raw = None
    text = _interpret_fetch_output(raw)
    if text:
        return text
    # Tier 1 came back empty / sentinel / errored — try the browser render.
    # Cap tier 2 at a tighter timeout: by the time we get here, total time
    # spent on the listing is already meaningful and we don't want one stuck
    # render to blow the scan budget.
    return _fetch_via_playwright(url, timeout_s=min(timeout_s, 25.0))
