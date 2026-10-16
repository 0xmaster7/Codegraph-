"""Race-resistant reads of repository files using directory-relative opens."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path, PurePosixPath


class RepositoryPathEscape(ValueError):
    """A repository entry resolves outside its authorized root."""


class RepositoryPathRace(ValueError):
    """A path changed to a symlink or unsafe object while being opened."""


def read_repository_file(root: Path, relative: str) -> tuple[bytes, os.stat_result] | None:
    """Read a regular file beneath root without following raced path components.

    Stable in-repository symlinks are supported by resolving once and then
    securely opening the resolved relative path. The subsequent open walks
    every component from a directory descriptor with O_NOFOLLOW, so replacing
    a symlink or parent directory cannot redirect the read outside root.
    """
    rel = PurePosixPath(relative)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts or ".git" in rel.parts:
        raise RepositoryPathEscape(f"Unsafe repository path: {relative!r}")

    canonical_root = root.resolve(strict=True)
    candidate = root / Path(*rel.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        return None
    if resolved != canonical_root and canonical_root not in resolved.parents:
        raise RepositoryPathEscape(f"Path resolves outside repository root: {relative!r}")
    target_parts = resolved.relative_to(canonical_root).parts
    if not target_parts:
        return None

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(os.sep, directory_flags)
    try:
        for component in canonical_root.parts[1:]:
            child = os.open(component, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        for component in target_parts[:-1]:
            child = os.open(component, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        try:
            file_descriptor = os.open(target_parts[-1], file_flags, dir_fd=descriptor)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise RepositoryPathRace(
                    f"Repository path changed to a symlink while opening {relative!r}"
                ) from exc
            raise
        try:
            metadata = os.fstat(file_descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                return None
            contents = bytearray()
            while chunk := os.read(file_descriptor, 1024 * 1024):
                contents.extend(chunk)
            return bytes(contents), metadata
        finally:
            os.close(file_descriptor)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RepositoryPathRace(
                f"Repository path changed to a symlink while opening {relative!r}"
            ) from exc
        raise
    finally:
        os.close(descriptor)
