"""docker/prepare.py: a container has no keyring, so its first start makes the vault a folder of files (a settings file
that already says so is left alone, and one that can't be written is said)."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import yaml

import orion

PREPARE = Path(orion.__file__).resolve().parent.parent / "docker" / "prepare.py"


def prepare() -> ModuleType:
    spec = importlib.util.spec_from_file_location("docker_prepare", PREPARE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def no_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORION_FETCH_MODELS", "0")


def test_the_first_start_keeps_secrets_in_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    prepare().main()
    config = tmp_path / "data" / "config.yaml"
    assert yaml.safe_load(config.read_text())["secrets"]["store"] == "files"
    assert "secrets.store is files" in capsys.readouterr().err


def test_a_settings_file_that_already_says_files_is_left_alone(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "data" / "config.yaml"
    config.parent.mkdir()
    config.write_text("secrets:\n  store: files\n# my notes\n")
    before = config.read_text()
    prepare().main()
    assert config.read_text() == before and "secrets.store" not in capsys.readouterr().err


def test_a_file_that_cannot_be_written_is_said_and_nothing_is_forced(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "data" / "config.yaml"
    config.parent.mkdir()
    config.write_text("secrets: [this is not a section\n")  # broken
    prepare().main()
    assert config.read_text() == "secrets: [this is not a section\n"
    assert "can't be written, so his secrets can't be kept" in capsys.readouterr().err
