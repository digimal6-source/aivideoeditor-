"""Word-level transcript types and caption phrase grouping.

The grouper is pure and side-effect free so it can be unit-tested without any
speech model. The hard rule it enforces is the one the product spec calls out:
**never emit more words in a phrase than ``max_words_per_phrase``.**
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .models import CaptionSettings

#: Characters that end a sentence - always force a phrase break after these.
SENTENCE_ENDINGS = ".!?\u2026"
#: Characters that mark a natural clause boundary - a soft break.
CLAUSE_ENDINGS = ",;:\u2014-"

#: A pause longer than this between two words is treated as a natural boundary.
PAUSE_BREAK_SECONDS = 0.34

#: Words that read badly as the last word of a caption card.
_WEAK_TRAILING = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for",
        "with", "is", "was", "are", "were", "be", "as", "by", "that", "this",
        "it", "its", "my", "your", "our", "their", "his", "her",
    }
)


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float

    def to_dict(self) -> dict:
        return {"text": self.text, "start": self.start, "end": self.end}

    @staticmethod
    def from_dict(data: dict) -> "Word":
        return Word(
            text=str(data.get("text") or "").strip(),
            start=float(data.get("start") or 0.0),
            end=float(data.get("end") or 0.0),
        )


@dataclass(frozen=True)
class Phrase:
    text: str
    start: float
    end: float
    words: tuple[Word, ...] = ()

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "words": [w.to_dict() for w in self.words],
        }


def _strip_word(text: str) -> str:
    return text.strip().strip("\u200b")


def _ends_sentence(text: str) -> bool:
    return bool(text) and text.rstrip('"\u201d\')')[-1:] in SENTENCE_ENDINGS


def _ends_clause(text: str) -> bool:
    return bool(text) and text.rstrip('"\u201d\')')[-1:] in CLAUSE_ENDINGS


def _core(text: str) -> str:
    return text.strip().strip(".,!?:;\"'\u2014-").lower()


def group_words(words: Sequence[Word], settings: CaptionSettings) -> list[Phrase]:
    """Group word timings into short, readable caption cards.

    Break priority, highest first:
      1. the configured maximum word count (a hard limit, never exceeded)
      2. sentence-ending punctuation
      3. a speech pause longer than :data:`PAUSE_BREAK_SECONDS`
      4. clause punctuation, when the card already has enough words to stand alone
      5. the maximum character budget, so long words never overflow the viewport
    """
    cleaned = [
        Word(_strip_word(w.text), float(w.start), float(max(w.end, w.start)))
        for w in words
        if _strip_word(w.text)
    ]
    if not cleaned:
        return []

    max_words = max(1, settings.max_words_per_phrase)
    max_chars = max(6, settings.max_chars_per_phrase)

    phrases: list[Phrase] = []
    buffer: list[Word] = []

    def flush() -> None:
        if not buffer:
            return
        text = " ".join(w.text for w in buffer)
        if settings.uppercase:
            text = text.upper()
        phrases.append(
            Phrase(text=text, start=buffer[0].start, end=buffer[-1].end, words=tuple(buffer))
        )
        buffer.clear()

    for index, word in enumerate(cleaned):
        prospective = len(" ".join(w.text for w in buffer + [word]))
        if buffer and prospective > max_chars and len(buffer) >= 1:
            flush()
        buffer.append(word)

        if len(buffer) >= max_words:
            flush()
            continue

        if _ends_sentence(word.text):
            flush()
            continue

        nxt = cleaned[index + 1] if index + 1 < len(cleaned) else None
        if nxt is not None and (nxt.start - word.end) >= PAUSE_BREAK_SECONDS:
            flush()
            continue

        if (
            _ends_clause(word.text)
            and len(buffer) >= max(2, max_words - 1)
            and _core(word.text) not in _WEAK_TRAILING
        ):
            flush()

    flush()
    return _tidy(phrases, max_words)


def _tidy(phrases: list[Phrase], max_words: int) -> list[Phrase]:
    """Pull a stranded single weak word back onto the previous card when it fits."""
    if max_words < 2:
        return phrases
    result: list[Phrase] = []
    for phrase in phrases:
        if (
            result
            and phrase.word_count == 1
            and _core(phrase.text) in _WEAK_TRAILING
            and result[-1].word_count < max_words
        ):
            previous = result[-1]
            merged_words = previous.words + phrase.words
            result[-1] = Phrase(
                text=f"{previous.text} {phrase.text}",
                start=previous.start,
                end=phrase.end,
                words=merged_words,
            )
            continue
        result.append(phrase)
    return result


def enforce_minimum_display(phrases: Iterable[Phrase], minimum: float = 0.30) -> list[Phrase]:
    """Stretch very short cards so they stay readable, without overlapping."""
    items = list(phrases)
    for index, phrase in enumerate(items):
        if phrase.end - phrase.start >= minimum:
            continue
        limit = items[index + 1].start if index + 1 < len(items) else phrase.start + minimum
        new_end = min(max(phrase.end, phrase.start + minimum), max(limit, phrase.start + 0.05))
        items[index] = Phrase(phrase.text, phrase.start, new_end, phrase.words)
    return items


def words_from_text(text: str, duration: float) -> list[Word]:
    """Evenly time a manually supplied transcript across the clip.

    Used by the 'manual' transcription backend, which lets the app produce real
    captions on machines where no speech model is installed.
    """
    tokens = [t for t in text.split() if t]
    if not tokens or duration <= 0:
        return []
    per_word = duration / len(tokens)
    return [
        Word(token, round(index * per_word, 3), round((index + 1) * per_word, 3))
        for index, token in enumerate(tokens)
    ]
