from pathlib import Path

import pytest

from photo_keeper.config import ConfigError, load_config


def test_load_multiple_chains(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[[chains]]\nname = "first"\nenabled = true\n[[chains]]\nname = "second"\n',
        encoding="utf-8",
    )

    config = load_config(path)

    assert [(chain.name, chain.enabled) for chain in config.chains] == [
        ("first", True),
        ("second", False),
    ]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('chains = "wrong"', "array of tables"),
        ("chains = [1]", "must be a table"),
        ("chain = []", "Unknown configuration keys"),
        ('[[chains]]\nname = "one"\nenabeld = true', "unknown keys"),
        ('[[chains]]\nname = "one"\n[[chains]]\nname = "one"', "Duplicate chain name"),
        ('[[chains]]\nname = ""', "nonempty name"),
        ('[[chains]]\nname = " one "', "surrounding whitespace"),
        ("[[chains]]\nname = 42", "nonempty name"),
        ("[[chains]]", "nonempty name"),
        ('[[chains]]\nname = "one"\nenabled = "false"', "must be a boolean"),
        ('[[chains]]\nname = "one"\nenabled = 1', "must be a boolean"),
        ("[[chains]", "Cannot load"),
    ],
)
def test_reject_invalid_config(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_empty_inventory(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("chains = []\n", encoding="utf-8")

    assert load_config(path).chains == ()


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Cannot load"):
        load_config(tmp_path / "missing.toml")


def test_invalid_encoding(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(b"\xff")

    with pytest.raises(ConfigError, match="Cannot load"):
        load_config(path)
