"""Canonical text cleanup before model output becomes durable company truth.

Founder-authored text is preserved semantically, but provider/control artifacts
must never be frozen into Project/Work authority.  This module intentionally
removes only non-content Unicode/control characters and normalizes line endings;
it does not rewrite wording or invent missing terms.
"""
from __future__ import annotations

import unicodedata
from typing import Iterable, Any

# U+FFFC OBJECT REPLACEMENT CHARACTER and U+FFFD REPLACEMENT CHARACTER were
# observed in live Project #20 acceptance criteria after provider truncation.
_EXPLICIT_NOISE = {"\ufffc", "\ufffd", "\ufeff", "\u200b", "\u200c", "\u200d", "\u2060"}


def clean_text(value: Any, *, multiline: bool = True) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for char in text:
        if char in _EXPLICIT_NOISE:
            continue
        if char == "\n":
            out.append("\n" if multiline else " ")
            continue
        if char == "\t":
            out.append("\t" if multiline else " ")
            continue
        category = unicodedata.category(char)
        # Cc/Cf/Cs are transport/control artifacts, not business authority.
        if category in {"Cc", "Cf", "Cs"}:
            continue
        out.append(char)
    cleaned = "".join(out)
    if multiline:
        lines = [" ".join(line.split()) for line in cleaned.split("\n")]
        cleaned = "\n".join(line for line in lines if line)
    else:
        cleaned = " ".join(cleaned.split())
    return cleaned.strip()


def clean_rows(values: Iterable[Any] | str | None) -> list[str]:
    if values is None:
        return []
    raw = values.splitlines() if isinstance(values, str) else list(values)
    result: list[str] = []
    for value in raw:
        text = clean_text(value, multiline=False).strip(" -\t")
        if text:
            result.append(text)
    return result


def contains_forbidden_control_text(value: Any) -> bool:
    text = str(value or "")
    return clean_text(text, multiline=True) != text.replace("\r\n", "\n").replace("\r", "\n").strip()
