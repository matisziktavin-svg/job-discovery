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
