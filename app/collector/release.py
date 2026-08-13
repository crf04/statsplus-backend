"""Immutable collector release metadata and checksum verification."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ReleaseMetadata:
    version: str
    checksum: str
    files: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"version": self.version, "checksum": self.checksum, "files": list(self.files)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def _files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(
        path for path in root.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and "__pycache__" not in path.parts
        and not any(part in {".pytest_cache", ".ruff_cache", ".mypy_cache", ".cache"} for part in path.parts)
        and path.suffix.casefold() not in {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key"}
        and path.name != ".coverage"
        and not path.name.endswith(".instructions.json")
        and path.name != ".env"
        and not path.name.startswith(".env.")
        and path.name.casefold() not in {"collector.env.ps1", "current.txt", "previous.txt"}
    ))


def release_metadata(root: str | Path, *, version: str) -> ReleaseMetadata:
    base = Path(root).resolve()
    if not version.strip():
        raise ValueError("release version is required")
    paths = _files(base)
    digest = hashlib.sha256()
    names: list[str] = []
    for path in paths:
        relative = path.relative_to(base).as_posix()
        names.append(relative)
        data = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return ReleaseMetadata(version=version.strip(), checksum=digest.hexdigest(), files=tuple(names))


def verify_release(root: str | Path, metadata: ReleaseMetadata) -> bool:
    actual = release_metadata(root, version=metadata.version)
    return actual.checksum == metadata.checksum and actual.files == metadata.files


__all__ = ["ReleaseMetadata", "release_metadata", "verify_release"]
