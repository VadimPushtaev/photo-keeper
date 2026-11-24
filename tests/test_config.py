from pathlib import Path

import pytest

from photo_keeper.config import ConfigError, load_config


def test_load_multiple_chains(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[[chains]]\nname = "first"\nenabled = true\nsource = "dropbox:Camera Uploads"\n'
        'destination = "/photos"\n[[chains]]\nname = "second"\n',
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


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ('source = "relative"', "named rclone remote"),
        ('source = "dropbox:a/../b"', "relative path"),
        ('source = "dropbox:a//b"', "relative path"),
        ('source = "dropbox:a/"', "relative path"),
        ('source = "dropbox:a/./b"', "relative path"),
        ("source = 42", "nonempty"),
        ('source = ""', "nonempty"),
        ('destination = "relative"', "absolute local"),
        ("destination = false", "nonempty"),
        ('destination = ""', "nonempty"),
        ('subdirectory = "/outside"', "relative path"),
        ('subdirectory = "../outside"', "relative path"),
        ('subdirectory = "a/../outside"', "relative path"),
        ('subdirectory = "a//b"', "relative path"),
        ('subdirectory = "C:/outside"', "relative path"),
        ("subdirectory = 'a\\b'", "relative path"),
        ('subdirectory = ""', "nonempty"),
        ("subdirectory = []", "nonempty"),
        ('recursive = "true"', "boolean"),
        ("recursive = 1", "boolean"),
        ('date_source = "upload"', "date_source"),
        ("date_source = []", "date_source"),
        ("date_source = 1", "date_source"),
        ('name = "line\\nfeed"', "control characters"),
    ],
)
def test_copy_settings_are_validated(tmp_path: Path, field: str, message: str) -> None:
    path = tmp_path / "config.toml"
    base = "[[chains]]\n" + ("" if field.startswith("name =") else 'name = "test"\n')
    path.write_text(base + field, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(path)


@pytest.mark.parametrize("settings", ["", 'source = "dropbox:"', 'destination = "/photos"'])
def test_enabled_chain_requires_source_and_destination(tmp_path: Path, settings: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[[chains]]\nname = "test"\nenabled = true\n' + settings)
    with pytest.raises(ConfigError, match="require source and destination"):
        load_config(path)


def test_root_source_and_unicode_destination(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[[chains]]\nname = "test"\nenabled = true\nsource = "dropbox:"\n'
        'destination = "/photos/Фото"\nsubdirectory = "Камера/__raw__"\n'
        'recursive = false\ndate_source = "modified"',
        encoding="utf-8",
    )
    chain = load_config(path).chains[0]
    assert chain.source == "dropbox:"
    assert chain.destination == Path("/photos/Фото")
    assert chain.subdirectory == "Камера/__raw__"
    assert chain.recursive is False
    assert chain.date_source == "modified"


def test_rclone_config_path_is_relative_to_config_not_cwd(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "config-dir"
    folder.mkdir()
    path = folder / "settings.toml"
    path.write_text('[rclone]\nconfig_file = "private/token.json"')
    monkeypatch.chdir(tmp_path)
    assert load_config(path).rclone_config == folder / "private/token.json"


def test_default_rclone_config_path(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("")
    assert load_config(path).rclone_config is None


def test_absolute_rclone_config_path(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[rclone]\nconfig_file = "/private/token.json"')
    assert load_config(path).rclone_config == Path("/private/token.json")


@pytest.mark.parametrize(
    "content",
    [
        'rclone = "wrong"',
        "[rclone]\nconfig_file = 42",
        '[rclone]\nconfig_file = ""',
        '[rclone]\nrefresh_token = "secret"',
    ],
)
def test_invalid_rclone_settings(tmp_path: Path, content: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(content)
    with pytest.raises(ConfigError):
        load_config(path)


def test_committed_example_is_valid_and_disabled() -> None:
    config = load_config(Path(__file__).parents[1] / "config.example.toml")
    assert config.chains
    assert all(not chain.enabled for chain in config.chains)


def test_old_dropbox_settings_have_migration_message(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[dropbox]\n")
    with pytest.raises(ConfigError, match=r"Replace \[dropbox\]"):
        load_config(path)


@pytest.mark.parametrize(
    "source,settings,match",
    [
        ("cloud:folder", 'sort_by_month = "yes"', "sort_by_month must be a boolean"),
        ("cloud:folder", 'sort_by_month = true\nsubdirectory = "other"', "ends? in|to end in"),
        ("cloud:folder", "delete = true", "delete must be a table"),
        ("cloud:folder", 'delete = {source = "true"}', "delete.source"),
        ("cloud:folder", "delete = {wrong = true}", "delete.wrong"),
        ("cloud:", "delete = {source = true}", "requires a named folder"),
        ("cloud:/.photo-keeper-cleanup/abc", "", "reserved cleanup holding area"),
    ],
)
def test_month_and_cleanup_settings_validation(tmp_path, source, settings, match):
    config = tmp_path / "config.toml"
    config.write_text(f'[[chains]]\nname = "phone"\nsource = "{source}"\n{settings}')
    with pytest.raises(ConfigError, match=match):
        load_config(config)


def test_each_step_needs_explicit_deletion_permission(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[[chains]]\nname = "phone"\nsource = "cloud:folder"\nsort_by_month = true\n'
        "[chains.delete]\nraw = true\narchive = true\n"
    )
    (chain,) = load_config(config).chains
    assert chain.sort_by_month
    assert not chain.delete.source
    assert chain.delete.raw and chain.delete.archive
