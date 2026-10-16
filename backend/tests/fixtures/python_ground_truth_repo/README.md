# Python ground-truth fixture

This corpus is frozen as `ground-truth-v1`. Its manifest records source-semantic expectations and UTF-8 byte spans; those expectations describe the intended Python behavior and do not encode SCIP symbol formats.

The fixture history test builds the same four commits from this tree using fixed identities and timestamps. The manifest pins each commit id and the test checks the rename, deletion, merge parents, mailmap, and a shallow clone of the merge commit. A source or history change requires a deliberate fixture version bump and regenerated review of every gold span and commit id.

The tests under `tests/unit` and `tests/integration` intentionally share the basename `test_flow.py`. Fixture pytest runs use importlib mode to prove both files collect independently. The optional subprocess test is marked `expected_partial` because static call structure does not establish child-process behavior.
