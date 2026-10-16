from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from pkg.auth import verify_token
from pkg.base_dispatch import Child
from pkg.checkout import callback


@pytest.fixture
def token() -> str:
    return "fixture-token"


@pytest.mark.parametrize("value", ["a", "b"])
def test_verifies_token(value: str) -> None:
    assert verify_token(value)


def test_fixture_value(token: str) -> None:
    assert verify_token(token)


def test_monkeypatch_case(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pkg.auth.verify_token", lambda _value: True)
    import pkg.auth

    assert pkg.auth.verify_token("patched")


def test_threaded_callback() -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(callback, ["a", "b"])) == [True, True]


def test_multiple_inheritance_has_deterministic_c3_mro() -> None:
    assert [base.__name__ for base in Child.__mro__] == [
        "Child",
        "Left",
        "Right",
        "Grandparent",
        "object",
    ]


def test_imports_module_without_entering_unvisited() -> None:
    import pkg.unvisited

    assert pkg.unvisited.PRESENT


@pytest.mark.expected_partial
def test_optional_subprocess_case() -> None:
    """Subprocess observations are intentionally outside static call certainty."""
    import subprocess
    import sys

    assert subprocess.run([sys.executable, "-c", "print('child')"], check=True, capture_output=True)
