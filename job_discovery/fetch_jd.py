"""Tiered job-description recovery.

Public API:
  - fetch_job_description(url): sync facade. Returns recovered JD text, or
    None for any non-success (unreadable page, sentinel, timeout, SDK crash).
    NEVER raises — callers treat None as "could not verify".

Recovery is staged:
  Tier 1 — WebFetch via the Claude Agent SDK (Haiku). Fast and free when it
    works; defeated by JS-rendered SPAs (Workday, Greenhouse, Lever, iCIMS).
  Tier 2 — Firecrawl scrape → clean Markdown. Firecrawl renders JS and defeats
    the bot protection that stops both WebFetch and a plain headless browser
    (LinkedIn is still a coin-flip, but Workday/Greenhouse/Lever/iCIMS crack).
    No extra LLM call: the downstream batched rescore already does
    extraction-via-LLM against the recovered text.

Tier 1 internals mirror score.score_llm's Claude Agent SDK setup (OAuth-forced,
file-based system prompt, bypassPermissions). The wrapping agent only calls
WebFetch and relays text, so it runs on Haiku — the extraction intelligence
lives in WebFetch's own model, not here.

Tier 2 requires the `firecrawl-py` SDK and a Firecrawl API key:
    pip install firecrawl-py            # (declared in pyproject deps)
    export FIRECRAWL_API_KEY=fc-...     # free tier: ~1000 scrapes/month
Firecrawl only runs when Tier 1 (free WebFetch) fails, so a normal scan burns
few credits. If the SDK isn't importable or the key is unset, tier 2 is
silently skipped (logged once per process) and fetch behaves as tier-1-only.
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

# Tier 2 (Firecrawl) trim and floor.
_TIER2_MAX_CHARS = 30_000
_TIER2_MIN_CHARS = 200

# Whether we've already warned about Firecrawl being unavailable (SDK missing
# or API key unset). Avoids spamming the log once per recovery attempt — one
# line per process is enough.
_firecrawl_unavailable_warned = False


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


def _extract_markdown(result: object) -> str | None:
    """Pull the markdown string out of a Firecrawl scrape result.

    firecrawl-py has returned both a dict (`{"markdown": ...}`) and a
    Document-style object (`result.markdown`) across versions, so accept
    either. Returns None if no markdown field is present.
    """
    if result is None:
        return None
    md = getattr(result, "markdown", None)
    if md is None and isinstance(result, dict):
        md = result.get("markdown")
    return md if isinstance(md, str) else None


def _fetch_via_firecrawl(url: str, timeout_s: float) -> str | None:
    """Tier 2: scrape with Firecrawl, return clean Markdown or None.

    Never raises. Returns None when:
      - firecrawl-py isn't installed or FIRECRAWL_API_KEY is unset.
      - The scrape errors out (timeout, HTTP error, blocked page).
      - The recovered text is too short to be a real JD (<_TIER2_MIN_CHARS).

    Output is trimmed to _TIER2_MAX_CHARS to keep the downstream LLM rescore
    cheap; nav/footer noise is left in — the scorer handles it.
    """
    global _firecrawl_unavailable_warned
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    try:
        from firecrawl import Firecrawl  # type: ignore
    except ImportError:
        if not _firecrawl_unavailable_warned:
            logger.info(
                "fetch_job_description: tier 2 skipped — firecrawl-py not installed"
            )
            _firecrawl_unavailable_warned = True
        return None
    if not api_key:
        if not _firecrawl_unavailable_warned:
            logger.info(
                "fetch_job_description: tier 2 skipped — FIRECRAWL_API_KEY unset"
            )
            _firecrawl_unavailable_warned = True
        return None

    try:
        client = Firecrawl(api_key=api_key)
        # Firecrawl's timeout is milliseconds; give it the same budget the
        # caller allotted this tier. onlyMainContent trims nav/footer server
        # side so we spend fewer of the _TIER2_MAX_CHARS on chrome.
        result = client.scrape(
            url,
            formats=["markdown"],
            only_main_content=True,
            timeout=int(timeout_s * 1000),
        )
    except Exception:
        logger.warning(
            "fetch_job_description: tier 2 firecrawl failed for %s",
            url, exc_info=True,
        )
        return None

    text = _extract_markdown(result)
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
    (Firecrawl scrape). Forces the Claude Max OAuth path (pops
    ANTHROPIC_API_KEY) so the SDK fetch bills the subscription, not a stray
    key — same rule as score.score_llm. (Firecrawl bills its own
    FIRECRAWL_API_KEY, unrelated to Anthropic.) Never raises: every failure
    path collapses to None.
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
    # Tier 1 came back empty / sentinel / errored — pay for a Firecrawl scrape.
    # Cap tier 2 at a tighter timeout: by the time we get here, total time
    # spent on the listing is already meaningful and we don't want one stuck
    # scrape to blow the scan budget.
    return _fetch_via_firecrawl(url, timeout_s=min(timeout_s, 25.0))
