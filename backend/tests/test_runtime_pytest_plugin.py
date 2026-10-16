from __future__ import annotations

from backend.app import runtime_pytest_plugin


def test_unconfigure_disables_profile_callbacks_before_shutdown(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        runtime_pytest_plugin.sys,
        "setprofile",
        lambda profile: calls.append(("sys", profile)),
    )
    monkeypatch.setattr(
        runtime_pytest_plugin.threading,
        "setprofile",
        lambda profile: calls.append(("threading", profile)),
    )

    runtime_pytest_plugin.pytest_unconfigure(None)

    assert calls == [("sys", None), ("threading", None)]
