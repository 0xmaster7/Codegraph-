from pathlib import Path

import pytest

from backend.app.config import Settings
from backend.app.ingestion import RepositoryConfigurationError
from scripts import benchmark_embeddings


def test_benchmark_argument_does_not_self_authorize_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = tmp_path / "requested-repository"
    repository.mkdir()
    monkeypatch.setattr(benchmark_embeddings, "Settings", lambda: Settings(_env_file=None))

    with pytest.raises(RepositoryConfigurationError, match="ALLOWED_REPOSITORY_ROOTS"):
        benchmark_embeddings._analyze_with_configured_roots(repository)
