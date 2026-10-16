from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from .ids import stable_id
from .models import ChangedPath, GitChangeStatus, GitCommit, GitDeveloper

RENAME_THRESHOLD = 50


def _git_bytes(root: Path, *args: str, check: bool = True) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    if check and result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace").strip() or "Git history failed")
    return result.stdout


def _decode(value: bytes) -> str:
    return value.decode("utf-8", "replace")


def _parse_identity(value: str) -> tuple[str, str]:
    match = re.fullmatch(r"(.*) <([^<>]*)>", value.strip())
    if not match:
        return value.strip(), ""
    return match.group(1).strip(), match.group(2).strip()


def _mailmap(root: Path, name: str, email: str) -> tuple[str, str]:
    value = f"{name} <{email}>"
    output = (
        _git_bytes(root, "check-mailmap", "--", value, check=False)
        .decode("utf-8", "replace")
        .strip()
    )
    return _parse_identity(output or value)


def _read_commits(root: Path) -> list[dict[str, str | list[str]]]:
    ids = [
        line
        for line in _git_bytes(root, "rev-list", "--topo-order", "--reverse", "HEAD")
        .decode("ascii", "replace")
        .splitlines()
        if line
    ]
    records: list[dict[str, str | list[str]]] = []
    for commit_id in ids:
        fields = _git_bytes(
            root,
            "show",
            "-s",
            "--format=%P%x00%an%x00%ae%x00%cn%x00%ce%x00%aI%x00%cI%x00%B",
            commit_id,
        ).split(b"\x00", 7)
        if len(fields) != 8:
            continue
        (
            parents,
            author_name,
            author_email,
            committer_name,
            committer_email,
            authored,
            committed,
            message,
        ) = fields
        object_headers = _git_bytes(root, "cat-file", "commit", commit_id).split(b"\n\n", 1)[0]
        stored_parents = [
            _decode(line.removeprefix(b"parent "))
            for line in object_headers.splitlines()
            if line.startswith(b"parent ")
        ]
        records.append(
            {
                "commit_id": commit_id,
                "parent_commit_ids": stored_parents or _decode(parents).split(),
                "author_name": _decode(author_name),
                "author_email": _decode(author_email),
                "committer_name": _decode(committer_name),
                "committer_email": _decode(committer_email),
                "authored_at": _decode(authored),
                "committed_at": _decode(committed),
                "message": _decode(message).rstrip("\n"),
            }
        )
    return records


def _changed_paths(
    root: Path,
    repository_id: str,
    commit_sha: str,
    parents: list[str],
) -> list[ChangedPath]:
    changes: list[ChangedPath] = []
    commit_id = f"{repository_id}::commit::{commit_sha}"
    parent_diffs: list[str | None] = list(parents) if parents else [None]
    for parent_index, parent_id in enumerate(parent_diffs):
        if (
            parent_id is not None
            and subprocess.run(
                ["git", "-C", str(root), "cat-file", "-e", f"{parent_id}^{{commit}}"],
                capture_output=True,
                check=False,
            ).returncode
        ):
            # A shallow boundary records its parent id without carrying the parent tree.
            continue
        args = [
            "diff-tree",
            "--no-commit-id",
            "-r",
            "--name-status",
            "-z",
            f"-M{RENAME_THRESHOLD}%",
        ]
        args.extend([parent_id, commit_sha] if parent_id else ["--root", commit_sha])
        fields = _git_bytes(root, *args).decode("utf-8", "replace").split("\0")
        if fields and not fields[-1]:
            fields.pop()
        cursor = 0
        while cursor < len(fields):
            status = fields[cursor]
            cursor += 1
            if not status:
                continue
            change_status = {
                "A": GitChangeStatus.ADDED,
                "M": GitChangeStatus.MODIFIED,
                "D": GitChangeStatus.DELETED,
                "R": GitChangeStatus.RENAMED,
                "C": GitChangeStatus.COPIED,
                "T": GitChangeStatus.TYPE_CHANGED,
            }.get(status[0])
            if change_status is None:
                continue
            similarity = int(status[1:]) if status[1:].isdigit() else None
            old_path: str | None
            new_path: str | None
            if change_status in {GitChangeStatus.RENAMED, GitChangeStatus.COPIED}:
                old_path = fields[cursor]
                new_path = fields[cursor + 1]
                cursor += 2
            else:
                path = fields[cursor]
                cursor += 1
                old_path = path if change_status == GitChangeStatus.DELETED else None
                new_path = path if change_status != GitChangeStatus.DELETED else None
            changes.append(
                ChangedPath(
                    change_id=stable_id(
                        commit_id,
                        parent_id or "ROOT",
                        parent_index,
                        status,
                        old_path or "",
                        new_path or "",
                        prefix="change_",
                    ),
                    commit_id=commit_id,
                    parent_commit_sha=parent_id,
                    parent_index=parent_index,
                    status=change_status,
                    old_path=old_path,
                    new_path=new_path,
                    similarity=similarity,
                )
            )
    return changes


def extract_git_history(
    root: Path,
    repository_id: str,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[list[GitDeveloper], list[GitCommit], list[ChangedPath]]:
    raw_commits = _read_commits(root)
    developers_by_identity: dict[tuple[str, str], GitDeveloper] = {}
    commits: list[GitCommit] = []
    changed_paths: list[ChangedPath] = []

    def developer(raw_name: str, raw_email: str) -> GitDeveloper:
        name, email = _mailmap(root, raw_name, raw_email)
        key = (name.casefold(), email.casefold())
        if key not in developers_by_identity:
            developers_by_identity[key] = GitDeveloper(
                developer_id=stable_id(name.casefold(), email.casefold()), name=name, email=email
            )
        return developers_by_identity[key]

    for item in raw_commits:
        if check_cancelled:
            check_cancelled()
        commit_sha = str(item["commit_id"])
        commit_id = f"{repository_id}::commit::{commit_sha}"
        parent_shas = list(item["parent_commit_ids"])
        parent_ids = [f"{repository_id}::commit::{sha}" for sha in parent_shas]
        author = developer(str(item["author_name"]), str(item["author_email"]))
        committer = developer(str(item["committer_name"]), str(item["committer_email"]))
        commits.append(
            GitCommit(
                commit_id=commit_id,
                commit_sha=commit_sha,
                parent_commit_ids=parent_ids,
                parent_commit_shas=parent_shas,
                author_id=author.developer_id,
                committer_id=committer.developer_id,
                authored_at=str(item["authored_at"]),
                committed_at=str(item["committed_at"]),
                message=str(item["message"]),
            )
        )
        changed_paths.extend(_changed_paths(root, repository_id, commit_sha, parent_shas))

    return (
        sorted(developers_by_identity.values(), key=lambda item: item.developer_id),
        commits,
        changed_paths,
    )
