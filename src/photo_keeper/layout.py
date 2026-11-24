"""Local folder destinations and their date placeholders."""

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from photo_keeper.storage import safe_path

FIELDS = frozenset({"{year}", "{month}"})


@dataclass(frozen=True)
class Folder:
    path: Path
    year: int | None = None
    month: int | None = None


@dataclass(frozen=True)
class Layout:
    path: Path
    base: Path | None = None

    @property
    def fields(self) -> frozenset[str]:
        return FIELDS.intersection(self.path.parts)

    @property
    def root(self) -> Path:
        if self.base is not None:
            return self.base
        for i, part in enumerate(self.path.parts):
            if part in FIELDS:
                return Path(*self.path.parts[:i])
        return self.path.parent

    def render(self, year: int, month: int) -> Path:
        values = {"{year}": f"{year:04d}", "{month}": f"{month:02d}"}
        return Path(*(values.get(part, part) for part in self.path.parts))

    def folders(self) -> Iterator[Folder]:
        safe_path(Path(self.root.anchor), self.root)
        parts = self.path.relative_to(self.root).parts
        yield from _folders(self.root, Folder(self.root), parts)

    def overlaps(self, other: "Layout") -> bool:
        # Sharing an ancestor matters even on case-insensitive destination drives.
        for a, b in zip(self.path.parts, other.path.parts, strict=False):
            if a.casefold() == b.casefold():
                continue
            if a in FIELDS and b in FIELDS:
                return False  # Years have four digits, months have two.
            if (a in FIELDS and _date_part(a, b)) or (b in FIELDS and _date_part(b, a)):
                continue
            return False
        return True


def _date_part(field: str, name: str) -> bool:
    if not (name.isascii() and name.isdecimal()):
        return False
    return len(name) == 4 if field == "{year}" else len(name) == 2 and 1 <= int(name) <= 12


def _folders(root: Path, current: Folder, parts: tuple[str, ...]) -> Iterator[Folder]:
    if not parts:
        if current.path.is_dir():
            yield current
        return
    part, *tail = parts
    remaining = tuple(tail)
    if part not in FIELDS:
        child = current.path / part
        if not child.is_dir() or child.is_symlink():
            return
        yield from _folders(root, Folder(child, current.year, current.month), remaining)
        return
    safe_path(root, current.path)
    with os.scandir(current.path) as entries:
        for entry in entries:
            if not _date_part(part, entry.name) or not entry.is_dir(follow_symlinks=False):
                continue
            value = int(entry.name)
            if part == "{year}":
                yield from _folders(root, Folder(Path(entry.path), value, current.month), remaining)
            else:
                yield from _folders(root, Folder(Path(entry.path), current.year, value), remaining)
