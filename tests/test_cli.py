import logging
from pathlib import Path

import pytest

from photo_keeper.cli import main


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        '[[chains]]\nname = "first"\nenabled = true\n[[chains]]\nname = "second"\n',
        encoding="utf-8",
    )
    return path


def test_check_config(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(config_path), "check-config"]) == 0
    assert "Configuration valid: 2 chain(s)." in capsys.readouterr().out


def test_list_chains(config_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(config_path), "list-chains"]) == 0
    assert capsys.readouterr().out == "first\tenabled\nsecond\tdisabled\n"


@pytest.mark.parametrize(
    ("chain", "status", "message"),
    [
        ("first", 1, "not implemented"),
        ("second", 2, "disabled"),
        ("missing", 2, "Unknown chain"),
    ],
)
def test_run_reports_failure(
    config_path: Path,
    caplog: pytest.LogCaptureFixture,
    chain: str,
    status: int,
    message: str,
) -> None:
    with caplog.at_level(logging.ERROR):
        assert main(["--config", str(config_path), "run", chain]) == status
    assert message in caplog.text


def test_bad_config_returns_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        assert main(["--config", str(tmp_path / "missing.toml"), "check-config"]) == 2
    assert "Cannot load" in caplog.text
