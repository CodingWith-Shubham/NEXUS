"""The user's language(s) for Gemini Live: speech output and transcription hints.

Pipecat sets Gemini Live's speech ``language_code`` from its settings, but
builds the input transcription config with no language hint, so Gemini
auto-detects the language and can mishear accented English as Spanish. Gemini
Live accepts BCP-47 hints there (``AudioTranscriptionConfig.language_codes``;
checked against gemini-3.8-live), but Pipecat 1.16 has no setting for it, so
:func:`apply_transcription_hints` swaps in a config with the hints. Remove it
once Pipecat exposes the option.
"""

import functools

from google.genai.types import AudioTranscriptionConfig
from loguru import logger


def apply_transcription_hints(languages: list[str]) -> None:
    """Make Gemini Live transcribe with these BCP-47 language hints (e.g. ["en-IN"])."""
    import pipecat.services.google.gemini_live.llm as gemini_live

    if not languages:
        return
    gemini_live.AudioTranscriptionConfig = functools.partial(
        AudioTranscriptionConfig, language_codes=list(languages)
    )
    logger.info(f"Gemini Live transcription language hints: {languages}")
