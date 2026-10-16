from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from tree_sitter_language_pack import get_parser

from .config import Settings
from .ids import stable_id
from .models import RetrievalChunk, SnapshotAnalysis, SourceEntity

CHUNK_STRATEGY_VERSION = "source-chunks-v1"
MAX_CHUNK_CHARS = 4000
SOURCE_SUFFIXES = {".py", ".pyi"}
DOCUMENT_SUFFIXES = {".md", ".markdown"}


class SourceChunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    snapshot_id: str
    source_node_id: str
    file_path: str
    chunk_kind: str
    source_start_byte: int
    source_end_byte: int
    text: str
    content_hash: str
    chunk_strategy_version: str = CHUNK_STRATEGY_VERSION


class CachedEmbedding(BaseModel):
    model_config = ConfigDict(frozen=True)

    cache_key: str
    content_hash: str
    embedding_model_version: str
    chunk_strategy_version: str
    provider: str = "legacy"
    dimensions: int | None = None
    configuration_hash: str = ""
    vector: tuple[float, ...]


class EmbeddingCache:
    """Immutable local cache keyed by content and the embedding/chunk versions."""

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def get(
        self,
        content_hash: str,
        embedding_model_version: str,
        chunk_strategy_version: str = CHUNK_STRATEGY_VERSION,
        *,
        provider: str = "legacy",
        dimensions: int | None = None,
        configuration_hash: str = "",
    ) -> CachedEmbedding | None:
        cache_key = embedding_cache_key(
            content_hash,
            embedding_model_version,
            chunk_strategy_version,
            provider=provider,
            dimensions=dimensions,
            configuration_hash=configuration_hash,
        )
        path = self.root / f"{cache_key}.json"
        if not path.is_file():
            return None
        record = CachedEmbedding.model_validate_json(path.read_text(encoding="utf-8"))
        if (
            record.cache_key != cache_key
            or record.content_hash != content_hash
            or record.embedding_model_version != embedding_model_version
            or record.chunk_strategy_version != chunk_strategy_version
            or record.provider != provider
            or record.dimensions != dimensions
            or record.configuration_hash != configuration_hash
        ):
            raise ValueError("Embedding cache record does not match its content/model identity")
        return record

    def put(
        self,
        content_hash: str,
        embedding_model_version: str,
        vector: list[float],
        chunk_strategy_version: str = CHUNK_STRATEGY_VERSION,
        *,
        provider: str = "legacy",
        dimensions: int | None = None,
        configuration_hash: str = "",
    ) -> CachedEmbedding:
        if not vector:
            raise ValueError("Embedding vector cannot be empty")
        if any(not math.isfinite(value) for value in vector):
            raise ValueError("Embedding vector values must be finite")
        record = CachedEmbedding(
            cache_key=embedding_cache_key(
                content_hash,
                embedding_model_version,
                chunk_strategy_version,
                provider=provider,
                dimensions=dimensions,
                configuration_hash=configuration_hash,
            ),
            content_hash=content_hash,
            embedding_model_version=embedding_model_version,
            chunk_strategy_version=chunk_strategy_version,
            provider=provider,
            dimensions=dimensions,
            configuration_hash=configuration_hash,
            vector=tuple(vector),
        )
        path = self.root / f"{record.cache_key}.json"
        if path.exists():
            existing = CachedEmbedding.model_validate_json(path.read_text(encoding="utf-8"))
            if existing != record:
                raise ValueError(
                    "Embedding cache key already contains a different vector"
                ) from None
            return existing
        temporary = self.root / f".{record.cache_key}.{os.urandom(8).hex()}.tmp"
        temporary.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        try:
            os.link(temporary, path)
        except FileExistsError:
            existing = CachedEmbedding.model_validate_json(path.read_text(encoding="utf-8"))
            if existing != record:
                raise ValueError(
                    "Embedding cache key already contains a different vector"
                ) from None
            return existing
        finally:
            temporary.unlink(missing_ok=True)
        return record


def load_snapshot_sources(analysis: SnapshotAnalysis, settings: Settings) -> dict[str, bytes]:
    """Load verified Python source bytes from the immutable snapshot blob store."""
    cache_root = settings.snapshot_cache_dir.expanduser().resolve() / analysis.snapshot_id
    blobs_root = (cache_root / "blobs").resolve(strict=True)
    sources: dict[str, bytes] = {}
    for entry in analysis.manifest:
        if not entry.analyzed or Path(entry.path).suffix not in SOURCE_SUFFIXES:
            continue
        blob_path = blobs_root / entry.blob_hash
        resolved_blob = blob_path.resolve(strict=True)
        if blobs_root not in resolved_blob.parents:
            raise ValueError(f"Snapshot source blob escapes its cache: {entry.path}")
        content = resolved_blob.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != entry.content_hash or digest != entry.blob_hash:
            raise ValueError(f"Snapshot source blob failed hash verification: {entry.path}")
        sources[entry.path] = content
    return sources


def load_snapshot_retrieval_sources(
    analysis: SnapshotAnalysis, settings: Settings
) -> dict[str, bytes]:
    """Load hash-verified Python and Markdown only from immutable snapshot blobs."""
    cache_root = settings.snapshot_cache_dir.expanduser().resolve() / analysis.snapshot_id
    blobs_root = (cache_root / "blobs").resolve(strict=True)
    sources: dict[str, bytes] = {}
    for entry in analysis.manifest:
        suffix = Path(entry.path).suffix.lower()
        if not entry.analyzed or (
            suffix not in SOURCE_SUFFIXES and suffix not in DOCUMENT_SUFFIXES
        ):
            continue
        blob_path = (blobs_root / entry.blob_hash).resolve(strict=True)
        if blobs_root not in blob_path.parents:
            raise ValueError(f"Snapshot source blob escapes its cache: {entry.path}")
        content = blob_path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != entry.content_hash or digest != entry.blob_hash:
            raise ValueError(f"Snapshot source blob failed hash verification: {entry.path}")
        sources[entry.path] = content
    return sources


def build_source_chunks(
    analysis: SnapshotAnalysis,
    sources: dict[str, bytes],
    *,
    max_chunk_chars: int = MAX_CHUNK_CHARS,
) -> list[SourceChunk]:
    """Create deterministic code chunks linked to their existing source entities."""
    if max_chunk_chars < 1:
        raise ValueError("max_chunk_chars must be positive")
    entities_by_file: dict[str, list[SourceEntity]] = {}
    for entity in analysis.entities:
        if entity.kind != "File":
            entities_by_file.setdefault(entity.file_path, []).append(entity)

    chunks: list[SourceChunk] = []
    for file_path, content in sorted(sources.items()):
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        source_entities = entities_by_file.get(file_path, [])
        if not source_entities:
            continue
        module = next((entity for entity in source_entities if entity.kind == "Module"), None)
        if module is not None:
            header_end = _module_header_end(content)
            if header_end > 0:
                chunks.extend(
                    _make_chunks(
                        analysis.snapshot_id,
                        module,
                        content[:header_end],
                        0,
                        "MODULE_HEADER",
                        max_chunk_chars,
                    )
                )
        for entity in sorted(
            source_entities,
            key=lambda item: (item.span.start_byte, item.span.end_byte, item.entity_id),
        ):
            if entity.kind not in {"Class", "Function", "Method"}:
                continue
            if (
                entity.span.end_byte > len(content)
                or entity.span.start_byte >= entity.span.end_byte
            ):
                continue
            kind = {
                "Class": "CLASS_SOURCE",
                "Function": "FUNCTION_BODY",
                "Method": "METHOD_BODY",
            }[entity.kind]
            chunks.extend(
                _make_chunks(
                    analysis.snapshot_id,
                    entity,
                    content[entity.span.start_byte : entity.span.end_byte],
                    entity.span.start_byte,
                    kind,
                    max_chunk_chars,
                )
            )
    return sorted(
        chunks,
        key=lambda item: (
            item.file_path,
            item.source_start_byte,
            item.source_end_byte,
            item.source_node_id,
            item.chunk_id,
        ),
    )


def build_document_chunks(
    snapshot_id: str,
    documents: dict[str, bytes],
    *,
    max_chunk_chars: int = MAX_CHUNK_CHARS,
) -> list[RetrievalChunk]:
    """Chunk Markdown as retrieval evidence, without parsing it into code facts."""
    if max_chunk_chars < 1:
        raise ValueError("max_chunk_chars must be positive")
    chunks: list[RetrievalChunk] = []
    for file_path, content in sorted(documents.items()):
        if Path(file_path).suffix.lower() not in DOCUMENT_SUFFIXES:
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not text:
            continue
        source_node_id = stable_id(snapshot_id, "document-source", file_path, prefix="docsrc_")
        start = 0
        while start < len(text):
            end = min(start + max_chunk_chars, len(text))
            if end < len(text):
                boundary = text.rfind("\n\n", start, end)
                if boundary >= start:
                    end = boundary + 2
            part = text[start:end]
            part_bytes = part.encode("utf-8")
            byte_start = len(text[:start].encode("utf-8"))
            content_hash = hashlib.sha256(part_bytes).hexdigest()
            chunks.append(
                RetrievalChunk(
                    chunk_id=stable_id(
                        snapshot_id,
                        file_path,
                        "DOCUMENT",
                        byte_start,
                        byte_start + len(part_bytes),
                        content_hash,
                        CHUNK_STRATEGY_VERSION,
                        prefix="chunk_",
                    ),
                    snapshot_id=snapshot_id,
                    source_node_id=source_node_id,
                    file_path=file_path,
                    chunk_kind="DOCUMENT",
                    source_start_byte=byte_start,
                    source_end_byte=byte_start + len(part_bytes),
                    text=part,
                    content_hash=content_hash,
                    chunk_strategy_version=CHUNK_STRATEGY_VERSION,
                )
            )
            start = end
    return sorted(
        chunks,
        key=lambda item: (item.file_path, item.source_start_byte, item.source_end_byte),
    )


def embedding_cache_key(
    content_hash: str,
    embedding_model_version: str,
    chunk_strategy_version: str = CHUNK_STRATEGY_VERSION,
    *,
    provider: str = "legacy",
    dimensions: int | None = None,
    configuration_hash: str = "",
) -> str:
    """Scope reusable embedding values by content, model, and chunk strategy."""
    return stable_id(
        content_hash,
        embedding_model_version,
        chunk_strategy_version,
        provider,
        dimensions,
        configuration_hash,
        prefix="embcache_",
    )


def _module_header_end(content: bytes) -> int:
    tree = get_parser("python").parse(content)
    declarations = {"function_definition", "class_definition", "decorated_definition"}
    return next(
        (child.start_byte for child in tree.root_node.named_children if child.type in declarations),
        len(content),
    )


def _make_chunks(
    snapshot_id: str,
    entity: SourceEntity,
    content: bytes,
    source_start_byte: int,
    chunk_kind: str,
    max_chunk_chars: int,
) -> list[SourceChunk]:
    text = content.decode("utf-8")
    result: list[SourceChunk] = []
    char_start = 0
    while char_start < len(text):
        char_end = min(char_start + max_chunk_chars, len(text))
        if char_end < len(text):
            line_end = text.rfind("\n", char_start, char_end)
            if line_end >= char_start:
                char_end = line_end + 1
        part = text[char_start:char_end]
        part_bytes = part.encode("utf-8")
        byte_start = source_start_byte + len(text[:char_start].encode("utf-8"))
        byte_end = byte_start + len(part_bytes)
        content_hash = hashlib.sha256(part_bytes).hexdigest()
        chunk_id = stable_id(
            snapshot_id,
            entity.entity_id,
            chunk_kind,
            byte_start,
            byte_end,
            content_hash,
            CHUNK_STRATEGY_VERSION,
            prefix="chunk_",
        )
        result.append(
            SourceChunk(
                chunk_id=chunk_id,
                snapshot_id=snapshot_id,
                source_node_id=entity.entity_id,
                file_path=entity.file_path,
                chunk_kind=chunk_kind,
                source_start_byte=byte_start,
                source_end_byte=byte_end,
                text=part,
                content_hash=content_hash,
            )
        )
        char_start = char_end
    return result
