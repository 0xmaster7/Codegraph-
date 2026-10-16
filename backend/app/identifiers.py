from __future__ import annotations

import re

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_WORD_SEPARATOR = re.compile(r"[^\w]+|_+", flags=re.UNICODE)


def normalize_identifier(value: str) -> str:
    """Split Python-style snake/camel identifiers into stable lowercase words."""
    camel_split = _CAMEL_BOUNDARY.sub(" ", value.strip())
    return " ".join(
        part.casefold() for part in _WORD_SEPARATOR.split(camel_split) if part and part != "_"
    )


def fulltext_terms(value: str) -> str:
    """Build a plain AND query from identifier words, excluding query operators."""
    return normalize_identifier(value)
