"""Speech to text, through the same seam as every other model call.

The desktop app records a spoken board request and needs it back as text.
Gemini accepts inline audio the same way it accepts inline PDFs, so this rides
the existing :class:`~silkscreen.agents.model.Model` protocol -- the audio
travels as a :class:`~silkscreen.agents.model.Document` with an audio MIME
type -- and therefore needs no new key, provider, or SDK surface. Keeping the
call here preserves the layering rule: ``agents/`` is the only place a model
call lives, and :class:`ScriptedModel` keeps the tests offline.
"""

from __future__ import annotations

from .model import Document, Model

__all__ = ["transcribe_audio"]

#: The stable marker a ScriptedModel keys on, in the same style as the other
#: stage prompts ("designing a printed circuit board", "reviewing a circuit").
_TASK = "transcribing a spoken request"

_PROMPT = (
    f"You are {_TASK} into text.\n"
    "The attached audio is one short clip from a hardware engineer at their "
    "bench. They may say the wake name “Ada” / “hey Ada” and then a board "
    "request, or just a few words. Reply with ONLY the transcript of what was "
    "said: no commentary, no speaker labels, no timestamps, no quotation marks "
    "around the whole answer. Preserve the wake name as “Ada” when that is "
    "what you hear (including near-misses like Aida/Ada). Preserve technical "
    "vocabulary exactly as spoken (part numbers, voltages, units). If you "
    "cannot make out any words, reply with exactly: (inaudible)"
)


def transcribe_audio(
    model: Model,
    audio: bytes,
    mime_type: str,
    *,
    language: str | None = None,
) -> str:
    """One audio clip in, its transcript out, stripped of whitespace.

    ``language`` is a hint, not a filter: it tells the model what it is
    probably hearing, which matters for short clips where the accent alone
    is ambiguous. A wrong hint degrades to the model's own judgement.

    Whatever the model says *is* the transcript -- silence, noise, or the
    literal ``(inaudible)`` the prompt asks for all come back verbatim.
    Inventing an empty-is-error rule here would turn a quiet room into a
    server fault; a genuinely failed call already raises ``ModelError``.
    """
    prompt = _PROMPT
    if language:
        prompt += f"\nThe speech is most likely in this language: {language}"
    text = model.generate(
        prompt,
        documents=[Document(data=audio, mime_type=mime_type)],
        temperature=0.0,
    )
    return text.strip()
