from pathlib import Path

import pytest

from backend.app.securefs import RepositoryPathRace, read_repository_file


def test_symlink_retargeted_after_resolution_reads_only_the_resolved_inside_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    inside = root / "inside.txt"
    outside = tmp_path / "outside.txt"
    link = root / "link.txt"
    inside.write_text("inside", encoding="utf-8")
    outside.write_text("outside secret", encoding="utf-8")
    link.symlink_to(inside)
    original_resolve = Path.resolve

    def resolve_then_retarget(path: Path, *args: object, **kwargs: object) -> Path:
        resolved = original_resolve(path, *args, **kwargs)
        if path == link:
            link.unlink()
            link.symlink_to(outside)
        return resolved

    monkeypatch.setattr(Path, "resolve", resolve_then_retarget)
    result = read_repository_file(root, "link.txt")

    assert result is not None
    assert result[0] == b"inside"


def test_replacing_resolved_file_with_external_symlink_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    tracked = root / "tracked.txt"
    outside = tmp_path / "outside.txt"
    tracked.write_text("inside", encoding="utf-8")
    outside.write_text("outside secret", encoding="utf-8")
    original_resolve = Path.resolve

    def resolve_then_replace(path: Path, *args: object, **kwargs: object) -> Path:
        resolved = original_resolve(path, *args, **kwargs)
        if path == tracked:
            tracked.unlink()
            tracked.symlink_to(outside)
        return resolved

    monkeypatch.setattr(Path, "resolve", resolve_then_replace)
    with pytest.raises(RepositoryPathRace, match="symlink"):
        read_repository_file(root, "tracked.txt")
