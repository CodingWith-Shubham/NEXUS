"""generate_content with retries for Gemini's transient overload errors.

Gemini returns 503 ("high demand") and occasionally 500/504 under load. Those
are retried with backoff, then the call moves on to the fallback model. A quota
error (429) skips straight to the fallback model, since free-tier limits are per
model. Other request errors (4xx) are raised at once: retrying won't fix them.
"""

import asyncio

from google import genai
from google.genai import errors, types
from loguru import logger

TRANSIENT_CODES = {500, 503, 504}
QUOTA_CODE = 429


async def generate_content(
    client: genai.Client,
    *,
    model: str,
    contents,
    config: types.GenerateContentConfig | None = None,
    fallback_model: str | None = None,
    attempts: int = 3,
    backoff_secs: float = 1.5,
) -> types.GenerateContentResponse:
    models = [model] + ([fallback_model] if fallback_model and fallback_model != model else [])
    last_error: Exception | None = None
    for current in models:
        for attempt in range(attempts):
            try:
                return await client.aio.models.generate_content(
                    model=current, contents=contents, config=config
                )
            except errors.APIError as e:
                if e.code == QUOTA_CODE:
                    logger.warning(f"{current} quota exhausted (429); trying the next model")
                    last_error = e
                    break
                if e.code not in TRANSIENT_CODES:
                    raise
                last_error = e
                logger.warning(f"{current} unavailable ({e.code}), attempt {attempt + 1}/{attempts}")
                if attempt + 1 < attempts:
                    await asyncio.sleep(backoff_secs * (2**attempt))
    assert last_error is not None
    raise last_error
