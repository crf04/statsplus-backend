"""Persisted-artifact attribution for one Analysis Run.

This module owns everything the Analysis Run persistence path needs that is
not statistical modeling or identity construction: PNG identity stamping and
strict PNG structure verification, self-attributing CSV/PNG checks, and the
content-addressed persisted-artifact manifest plus its fail-closed verifier.
``AnalysisRunBuilder`` in ``matchup_analysis.py`` does not reach into this
module; the production script and the manifest callers are its only users.
"""

import hashlib
import struct
import zlib
from pathlib import Path

import pandas as pd

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# The exact persisted artifact set a published run must contain. A manifest is
# the publication marker for a complete run, so blessing anything short of this
# set (or naming files outside it) would let an incomplete run look published.
REQUIRED_PERSISTED_ARTIFACTS = frozenset(
    {
        "matchup_summary",
        "notable_matchups",
        "validated_interactions",
        "watchlist",
        "player_relative_matchups",
        "volume_matchup_summary",
        "validated_volume_interactions",
        "volume_reliability",
        "player_relative_volume_matchups",
        "descriptive_pts_per_min_heatmap",
        "descriptive_volume_interaction_heatmaps",
    }
)


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def _parse_png_chunks(png_bytes: bytes):
    """Return the ``(chunk_type, data)`` pairs of a structurally valid PNG.

    Bytes that are not a PNG, a chunk truncated at a header, a chunk whose
    declared length extends past the end of the file, a chunk with a bad CRC,
    a stream whose first chunk is not IHDR, a stream with no final IEND, or a
    stream with trailing bytes after IEND are all rejected so a mutilated
    image can never pass identity verification.
    """
    if not png_bytes.startswith(_PNG_SIGNATURE):
        raise ValueError("Not a PNG file")
    chunks = []
    offset = len(_PNG_SIGNATURE)
    while offset < len(png_bytes):
        if offset + 8 > len(png_bytes):
            raise ValueError("PNG chunk header is truncated")
        length = struct.unpack(">I", png_bytes[offset : offset + 4])[0]
        chunk_type = png_bytes[offset + 4 : offset + 8]
        if offset + 12 + length > len(png_bytes):
            raise ValueError(
                f"PNG chunk {chunk_type!r} extends past the end of the file"
            )
        data = png_bytes[offset + 8 : offset + 8 + length]
        stored_crc = struct.unpack(
            ">I", png_bytes[offset + 8 + length : offset + 12 + length]
        )[0]
        actual_crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        if actual_crc != stored_crc:
            raise ValueError(f"PNG chunk {chunk_type!r} has an invalid CRC")
        if not chunks and chunk_type != b"IHDR":
            raise ValueError("PNG first chunk must be IHDR")
        chunks.append((chunk_type, data))
        offset += 12 + length
        if chunk_type == b"IEND":
            if offset != len(png_bytes):
                raise ValueError("PNG contains data after the IEND chunk")
            return chunks
    raise ValueError("PNG is missing a final IEND chunk")


def stamp_png_identity(png_bytes: bytes, run_id: str, model_version: str) -> bytes:
    """Return ``png_bytes`` with tEXt chunks binding run/model identity.

    The input must already be a structurally valid PNG (signature, IHDR first,
    final IEND, bounded chunks with correct CRCs); the chunks are inserted
    immediately after the first (IHDR) chunk so the persisted PNG is
    self-attributing without disturbing the image data.
    """
    chunks = _parse_png_chunks(png_bytes)
    first_type, _ = chunks[0]
    if first_type != b"IHDR":
        raise ValueError("PNG first chunk must be IHDR")
    offset = len(_PNG_SIGNATURE)
    first_length = struct.unpack(">I", png_bytes[offset : offset + 4])[0]
    first_chunk_end = offset + 12 + first_length
    identity_chunks = _png_chunk(
        b"tEXt", b"run_id\x00" + run_id.encode("utf-8")
    ) + _png_chunk(
        b"tEXt", b"model_version\x00" + model_version.encode("utf-8")
    )
    return png_bytes[:first_chunk_end] + identity_chunks + png_bytes[first_chunk_end:]


def png_text_entries(png_bytes: bytes) -> dict:
    """Parse the tEXt chunks of a structurally valid PNG into a mapping."""
    entries = {}
    for chunk_type, data in _parse_png_chunks(png_bytes):
        if chunk_type == b"tEXt":
            keyword, separator, text = data.partition(b"\x00")
            if separator:
                entries[keyword.decode("ascii")] = text.decode("utf-8")
    return entries


def _verify_artifact_identity(path, run_id, model_version, name):
    """Fail closed unless a persisted artifact is verifiably attributed to a run.

    CSVs must be non-empty and carry ``RUN_ID``/``MODEL_VERSION`` values equal
    to the run's on every row (empty CSVs still embed an identity row), and
    PNGs must be structurally valid and embed the run identity in tEXt chunks.
    A file that cannot be positively attributed is rejected rather than
    blessed.
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        frame = pd.read_csv(path)
        if not {"RUN_ID", "MODEL_VERSION"}.issubset(frame.columns):
            raise ValueError(
                f"Artifact {name} ({Path(path).name}) is not self-attributing: "
                "missing RUN_ID/MODEL_VERSION columns"
            )
        if frame.empty:
            raise ValueError(
                f"Artifact {name} ({Path(path).name}) is not self-attributing: "
                "no rows carry RUN_ID/MODEL_VERSION values"
            )
        mismatched = frame.loc[
            (frame["RUN_ID"].astype(str) != run_id)
            | (frame["MODEL_VERSION"].astype(str) != model_version)
        ]
        if not mismatched.empty:
            raise ValueError(
                f"Artifact {name} ({Path(path).name}) embeds identity that does "
                "not match this run"
            )
    elif suffix == ".png":
        entries = png_text_entries(Path(path).read_bytes())
        if (
            entries.get("run_id") != run_id
            or entries.get("model_version") != model_version
        ):
            raise ValueError(
                f"Artifact {name} ({Path(path).name}) does not embed this run's identity"
            )


def artifact_manifest(run, artifact_paths: dict) -> dict:
    """Content-addressed manifest attributing a complete persisted run.

    ``artifact_paths`` must map exactly the required persisted artifact set to
    the files that were just saved for it; missing, unexpected, or duplicate
    files are rejected rather than blessed, so an incomplete run cannot get a
    manifest. Every CSV must carry the run's own ``RUN_ID``/``MODEL_VERSION``
    on every row and every PNG must embed them, and the recorded SHA-256 covers
    the persisted bytes, so later replacement or an interrupted publication is
    detectable against the digest.
    """
    provided = set(artifact_paths)
    missing = REQUIRED_PERSISTED_ARTIFACTS - provided
    unexpected = provided - REQUIRED_PERSISTED_ARTIFACTS
    if missing or unexpected:
        problems = []
        if missing:
            problems.append("missing: " + ", ".join(sorted(missing)))
        if unexpected:
            problems.append("unexpected: " + ", ".join(sorted(unexpected)))
        raise ValueError(
            "An artifact manifest must cover exactly the required persisted "
            "artifact set; " + "; ".join(problems)
        )
    artifacts = {
        name: {
            "file": Path(path).name,
            "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        }
        for name, path in artifact_paths.items()
    }
    files = [record["file"] for record in artifacts.values()]
    duplicates = sorted({file for file in files if files.count(file) > 1})
    if duplicates:
        raise ValueError(
            "An artifact manifest must map each file to exactly one artifact; "
            "duplicate files: " + ", ".join(duplicates)
        )
    for name, path in artifact_paths.items():
        _verify_artifact_identity(path, run.run_id, run.model_version, name)
    return {
        "run_id": run.run_id,
        "model_version": run.model_version,
        "stable_subtype_keys": {
            str(key): value for key, value in run.stable_subtype_keys.items()
        },
        "provenance": run.provenance.to_dict(),
        "artifacts": artifacts,
    }


def verify_persisted_manifest(manifest: dict, output_dir) -> None:
    """Fail-closed verification that a persisted run is complete and attributable.

    The recorded artifact set must be exactly the required persisted set with no
    duplicate filenames; each artifact must still exist, match its recorded
    SHA-256, and embed the manifest's run/model identity in its own bytes (CSV
    identity columns or PNG tEXt chunks). Missing, tampered, partial, or
    falsely attributed files raise instead of being silently accepted.
    """
    recorded = dict(manifest["artifacts"])
    provided = set(recorded)
    missing = REQUIRED_PERSISTED_ARTIFACTS - provided
    unexpected = provided - REQUIRED_PERSISTED_ARTIFACTS
    if missing or unexpected:
        problems = []
        if missing:
            problems.append("missing: " + ", ".join(sorted(missing)))
        if unexpected:
            problems.append("unexpected: " + ", ".join(sorted(unexpected)))
        raise ValueError(
            "A persisted manifest must cover exactly the required artifact set; "
            + "; ".join(problems)
        )
    files = [record["file"] for record in recorded.values()]
    duplicates = sorted({file for file in files if files.count(file) > 1})
    if duplicates:
        raise ValueError(
            "A persisted manifest must map each file to exactly one artifact; "
            "duplicate files: " + ", ".join(duplicates)
        )
    directory = Path(output_dir)
    for name, record in recorded.items():
        path = directory / record["file"]
        if not path.exists():
            raise FileNotFoundError(f"Artifact {name} is missing: {path}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != record["sha256"]:
            raise ValueError(f"Artifact {name} does not match its recorded digest")
        _verify_artifact_identity(
            path, manifest["run_id"], manifest["model_version"], name
        )
