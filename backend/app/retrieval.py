from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .identifiers import fulltext_terms, normalize_identifier
from .models import SnapshotAnalysis, SourceEntity, SourceSite


class SymbolResolution(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str
    status: Literal["UNIQUE", "AMBIGUOUS", "NOT_FOUND"]
    matched_by: Literal["ID", "QUALIFIED_NAME", "NAME", "NORMALIZED", "FULL_TEXT"] | None
    results: list[SourceEntity]
    snapshot_id: str | None = None
    result_limit: int = 100
    candidate_count_before_limit: int = 0
    truncated: bool = False


def resolve_symbol(analysis: SnapshotAnalysis, query: str, *, limit: int = 100) -> SymbolResolution:
    """Resolve exact identities first, then normalized identifiers and token matches."""
    if not 1 <= limit <= 500:
        raise ValueError("Result limit must be between 1 and 500")
    value = query.strip()
    if not value:
        return SymbolResolution(
            query=query,
            status="NOT_FOUND",
            matched_by=None,
            results=[],
            snapshot_id=analysis.snapshot_id,
            result_limit=limit,
            candidate_count_before_limit=0,
            truncated=False,
        )

    matches = [entity for entity in analysis.entities if entity.entity_id == value]
    matched_by: Literal["ID", "QUALIFIED_NAME", "NAME", "NORMALIZED", "FULL_TEXT"] | None = (
        "ID" if matches else None
    )
    if not matches:
        matches = [entity for entity in analysis.entities if entity.qualified_name == value]
        matched_by = "QUALIFIED_NAME" if matches else None
    if not matches:
        matches = [entity for entity in analysis.entities if entity.name == value]
        matched_by = "NAME" if matches else None
    if not matches:
        normalized = normalize_identifier(value)
        matches = [
            entity for entity in analysis.entities if entity.identifier_search_text == normalized
        ]
        matched_by = "NORMALIZED" if matches else None
    if not matches:
        terms = fulltext_terms(value).split()
        if terms:
            matches = [
                entity
                for entity in analysis.entities
                if all(term in entity.identifier_search_text.split() for term in terms)
            ]
            matched_by = "FULL_TEXT" if matches else None
    matches.sort(
        key=lambda entity: (entity.qualified_name, entity.file_path, entity.span.start_byte)
    )
    candidate_count = len(matches)
    status: Literal["UNIQUE", "AMBIGUOUS", "NOT_FOUND"]
    status = "NOT_FOUND" if not matches else "UNIQUE" if candidate_count == 1 else "AMBIGUOUS"
    return SymbolResolution(
        query=value,
        status=status,
        matched_by=matched_by,
        results=matches[:limit],
        snapshot_id=analysis.snapshot_id,
        result_limit=limit,
        candidate_count_before_limit=candidate_count,
        truncated=candidate_count > limit,
    )


def find_site(analysis: SnapshotAnalysis, site_id: str) -> SourceSite | None:
    return next((site for site in analysis.sites if site.site_id == site_id), None)


class SourceSpanResult(BaseModel):
    snapshot_id: str
    file_path: str
    file_content_hash: str
    start_byte: int
    end_byte: int
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    text: str


def get_source_span(
    analysis: SnapshotAnalysis,
    file_path: str,
    start_byte: int,
    end_byte: int,
    content: bytes,
) -> SourceSpanResult:
    if start_byte < 0 or end_byte < start_byte or end_byte > len(content):
        raise ValueError("Source span is outside the file")
    start_prefix = content[:start_byte]
    end_prefix = content[:end_byte]
    return SourceSpanResult(
        snapshot_id=analysis.snapshot_id,
        file_path=file_path,
        file_content_hash=next(
            item.content_hash for item in analysis.manifest if item.path == file_path
        ),
        start_byte=start_byte,
        end_byte=end_byte,
        start_line=start_prefix.count(b"\n") + 1,
        start_column=len(start_prefix.rsplit(b"\n", 1)[-1]),
        end_line=end_prefix.count(b"\n") + 1,
        end_column=len(end_prefix.rsplit(b"\n", 1)[-1]),
        text=content[start_byte:end_byte].decode("utf-8", "replace"),
    )
