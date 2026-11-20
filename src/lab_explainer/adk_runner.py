"""Thin wrapper around an ADK run: send parts, get text and token counts back.

Both agents need the same three things — run once, capture the final text, capture
usage — so it lives here rather than twice.
"""

from __future__ import annotations

import asyncio
import random
import sys

from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.genai import types

APP = "lab_explainer"

# The free tier returns these while it is busy. They say nothing about the request, so
# the only correct response is to wait and ask again.
RETRYABLE = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 5
BASE_DELAY = 4.0


def _status(exc: Exception) -> int | None:
    return getattr(exc, "code", None) or getattr(exc, "status_code", None)


class DailyQuotaExhausted(RuntimeError):
    """The free tier's per-day request budget is gone.

    A 429 can mean "too fast, slow down" or "that is all you get today". Only the first
    is worth retrying; backing off for a minute against a daily cap just wastes a minute
    and then fails anyway. The message says which one it is, so this distinguishes them
    and stops immediately on the second.
    """


def _is_daily_quota(exc: Exception) -> bool:
    text = str(exc)
    return "429" in text and (
        "PerDay" in text or "per day" in text.lower() or "requests per day" in text.lower()
    )


async def run_agent(agent: LlmAgent, parts: list[types.Part]) -> tuple[str, int, int]:
    """Return (final text, prompt tokens, output tokens), retrying transient failures."""
    last: Exception | None = None

    for attempt in range(MAX_ATTEMPTS):
        try:
            return await _run_once(agent, parts)
        except Exception as exc:  # noqa: BLE001 - re-raised below if not retryable
            if _is_daily_quota(exc):
                raise DailyQuotaExhausted(
                    "Gemini free-tier daily request quota is exhausted for this model "
                    "and project. Waiting will not help until it resets. Options: run "
                    "with --fixtures (no API calls), set LAB_MODEL to a different model "
                    "(the quota is per model), or use a key from a different project."
                ) from exc
            if _status(exc) not in RETRYABLE or attempt == MAX_ATTEMPTS - 1:
                raise
            last = exc
            # Exponential, with jitter so parallel callers do not retry in lockstep.
            delay = BASE_DELAY * (2**attempt) + random.uniform(0, 1)
            # Say so. A silent backoff looks like a slow model, and the difference
            # matters: one is the free tier being busy, the other is a reason to change
            # models. A first run that takes 100 seconds is almost always this.
            print(
                f"  [retry {attempt + 1}/{MAX_ATTEMPTS - 1}] {agent.name}: "
                f"{_status(exc)} from the API, waiting {delay:.0f}s",
                file=sys.stderr,
            )
            await asyncio.sleep(delay)

    raise last  # unreachable; kept so the type checker sees a return on every path


async def _run_once(agent: LlmAgent, parts: list[types.Part]) -> tuple[str, int, int]:
    runner = InMemoryRunner(agent=agent, app_name=APP)
    session = await runner.session_service.create_session(app_name=APP, user_id="u")

    text, tokens_in, tokens_out = "", 0, 0
    try:
        async for event in runner.run_async(
            user_id="u",
            session_id=session.id,
            new_message=types.Content(role="user", parts=parts),
        ):
            if usage := getattr(event, "usage_metadata", None):
                tokens_in += usage.prompt_token_count or 0
                tokens_out += usage.candidates_token_count or 0
            if event.is_final_response() and event.content and event.content.parts:
                text = "".join(p.text or "" for p in event.content.parts)
    finally:
        await runner.close()

    return text, tokens_in, tokens_out
