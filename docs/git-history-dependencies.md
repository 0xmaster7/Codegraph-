# Git history and dependency metadata

Snapshot inspection returns Git history facts in `developers`, `commits`, and
`changed_paths`, alongside `dependency_metadata`. A commit ID is scoped as
`<repository_id>::commit::<full_sha>`; raw commit and parent SHAs remain
available as separate fields. Changed paths use the plan's canonical statuses
(`ADDED`, `MODIFIED`, `DELETED`, `RENAMED`, `COPIED`, and `TYPE_CHANGED`) and
store one row per parent comparison.

History follows commits reachable from `HEAD`. Each commit carries its raw parent
IDs, mailmap-resolved author and committer identities, timestamps, and message.
Changed paths are compared separately with every available parent of a merge
commit. Rename detection is pinned to Git's 50% similarity threshold. A deleted
path remains in history as `old_path`; a rename carries both `old_path` and
`new_path`. Shallow repositories retain their shallow status and known parent
IDs, while changes requiring missing parent objects are omitted because they
cannot be verified from that clone. Detached `HEAD` is reported on the
repository record.

Dependency facts come from the snapshot's materialized `pyproject.toml` and
requirements files, including static build-system requirements. The raw
declaration is retained separately from its parsed name, PEP 508 specifier,
marker, and extras. VCS egg declarations are retained as raw strings with a
normalized name when they provide one. Recursive `-r` and `-c` includes are
retained. Include cycles, missing includes, unsupported
options, invalid requirements, and dynamic metadata are reported as
diagnostics. `setup.py` is never executed and is explicitly marked unsupported.

Each `pyproject.toml` forms a project component. Source paths are associated
with the nearest containing project root. Dependency records retain their
source manifest and component ID; declarations outside a configured project
root stay unassigned with a diagnostic. This keeps monorepo packages separate.

Installed distribution versions and import names are separate resolved facts
linked to declarations by requirement ID. They come from the backend
environment's `importlib.metadata` records. CodeGraph does not guess an import
package from a distribution name; a mapping such as `yaml` to `PyYAML` appears
only when installed metadata supplies that relationship. These environment
observations describe the analyzer environment, not an installation into the
inspected repository.
