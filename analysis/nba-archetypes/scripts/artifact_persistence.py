"""Persisted-artifact attribution for one Analysis Run.

This module owns everything the Analysis Run persistence path needs that is
not statistical modeling or identity construction: PNG identity stamping and
strict PNG structure verification, self-attributing CSV/PNG checks, the
content-addressed persisted-artifact manifest plus its fail-closed verifier,
and atomic staged publication of a verified artifact set.
``AnalysisRunBuilder`` in ``matchup_analysis.py`` does not reach into this
module; the production script and the manifest callers are its only users.
"""

import csv
import fcntl
import hashlib
import json
import os
import re
import shutil
import struct
import zlib
from pathlib import Path

import pandas as pd

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
# pandas silently renames a repeated header field to ``NAME.1``/``NAME.2``; a
# CSV carrying that mangled form has an ambiguous identity header.
_MANGLED_IDENTITY_COLUMN = re.compile(r"^(run_id|model_version)\.[0-9]+$", re.IGNORECASE)

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

# Every logical artifact is bound to exactly one canonical filename and file
# type. A manifest may not substitute an arbitrary name or path for a logical
# artifact: verification resolves each record to its canonical basename, so
# absolute paths, traversal paths, and CSV-for-PNG (or PNG-for-CSV) type
# substitution are structurally rejected before any content is trusted.
PERSISTED_ARTIFACT_FILENAMES = {
    "matchup_summary": "matchup_summary.csv",
    "notable_matchups": "notable_pts_per_min_matchups.csv",
    "validated_interactions": "validated_pts_per_min_interactions.csv",
    "watchlist": "descriptive_pts_per_min_watchlist.csv",
    "player_relative_matchups": "player_relative_matchups.csv",
    "volume_matchup_summary": "volume_matchup_summary.csv",
    "validated_volume_interactions": "validated_volume_interactions.csv",
    "volume_reliability": "volume_split_half_reliability.csv",
    "player_relative_volume_matchups": "player_relative_volume_matchups.csv",
    "descriptive_pts_per_min_heatmap": "descriptive_pts_per_min_interaction_heatmap.png",
    "descriptive_volume_interaction_heatmaps": "descriptive_volume_interaction_heatmaps.png",
}


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
    """Parse the tEXt chunks of a structurally valid PNG into a mapping.

    A keyword may appear at most once: a double-stamped image that embeds a
    foreign identity chunk before the expected one would otherwise silently
    overwrite the earlier value and pass verification, so a duplicate tEXt
    keyword fails closed instead of resolving to whichever chunk came last.
    """
    entries = {}
    for chunk_type, data in _parse_png_chunks(png_bytes):
        if chunk_type == b"tEXt":
            keyword, separator, text = data.partition(b"\x00")
            if separator:
                key = keyword.decode("ascii")
                if key in entries:
                    raise ValueError(
                        f"PNG embeds duplicate tEXt keyword {key!r}; refusing to "
                        "attribute a run to conflicting identity chunks"
                    )
                entries[key] = text.decode("utf-8")
    return entries


def _reject_ambiguous_csv_identity_header(path) -> None:
    """Fail closed when a CSV's raw header has duplicate or mangled identity fields.

    pandas silently renames a repeated header field to ``NAME.1``/``NAME.2``, so
    a CSV carrying two ``RUN_ID`` or ``MODEL_VERSION`` columns would otherwise
    read only the first while a conflicting second identity (``RUN_ID.1``)
    slipped past the value check. The raw header is parsed before any values are
    trusted and rejected when an identity field appears more than once (in any
    case), or when a pandas-mangled ``.N`` variant of an identity field is
    present at all.
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        try:
            header = next(csv.reader(handle))
        except StopIteration:
            raise ValueError(
                f"{Path(path).name} is not self-attributing: the CSV has no header"
            )
    normalized = [token.upper() for token in header]
    for identity in ("RUN_ID", "MODEL_VERSION"):
        if normalized.count(identity) > 1:
            raise ValueError(
                f"{Path(path).name} repeats the {identity} identity column; "
                "refusing to attribute a run to an ambiguous identity header"
            )
    for token in header:
        if _MANGLED_IDENTITY_COLUMN.fullmatch(token):
            raise ValueError(
                f"{Path(path).name} carries a mangled identity column {token!r}; "
                "refusing to attribute a run to an ambiguous identity header"
            )


def _verify_artifact_identity(path, run_id, model_version, name):
    """Fail closed unless a persisted artifact is verifiably attributed to a run.

    CSVs must be non-empty and carry ``RUN_ID``/``MODEL_VERSION`` values equal
    to the run's on every row (empty CSVs still embed an identity row), and
    PNGs must be structurally valid and embed the run identity in tEXt chunks.
    The file type is bound to the logical artifact: a ``.csv`` name for a PNG
    logical artifact (or vice versa) is rejected, and any other file type cannot
    carry run identity, so a misnamed, substituted, or identity-less file is
    never silently blessed.
    """
    expected_suffix = (
        ".png" if PERSISTED_ARTIFACT_FILENAMES[name].endswith(".png") else ".csv"
    )
    if Path(path).suffix.lower() != expected_suffix:
        raise ValueError(
            f"Artifact {name} must be persisted as a {expected_suffix} file; "
            f"got {Path(path).name}"
        )
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        _reject_ambiguous_csv_identity_header(path)
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
    else:
        raise ValueError(
            f"Artifact {name} ({Path(path).name}) is not a supported artifact "
            "type: only .csv and .png files can carry run identity"
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
    for name, path in artifact_paths.items():
        expected = PERSISTED_ARTIFACT_FILENAMES[name]
        if Path(path).name != expected:
            raise ValueError(
                f"Artifact {name} must be saved as {expected}, not {Path(path).name}"
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
    duplicate filenames, and every logical artifact must be bound to its one
    canonical basename (which also structurally rejects absolute, traversal, and
    type-substituted paths). Each artifact must still exist under ``output_dir``,
    resolve back inside it, match its recorded SHA-256, and embed the manifest's
    run/model identity in its own bytes (CSV identity columns or PNG tEXt
    chunks). Missing, tampered, partial, substituted, or falsely attributed
    files raise instead of being silently accepted.
    """
    for field in ("run_id", "model_version"):
        value = manifest.get(field)
        if not isinstance(value, str) or not _HEX_DIGEST.fullmatch(value):
            raise ValueError(
                f"A persisted manifest {field} must be a 64-character lowercase "
                "hex digest"
            )
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
    directory = Path(output_dir).resolve()
    for name, record in recorded.items():
        expected = PERSISTED_ARTIFACT_FILENAMES.get(name)
        file = record.get("file")
        if expected is None or file != expected:
            raise ValueError(
                f"Artifact {name} must be persisted as {expected}, not {file!r}"
            )
        path = directory / file
        if not path.exists():
            raise FileNotFoundError(f"Artifact {name} is missing: {path}")
        resolved = path.resolve()
        if resolved.parent != directory:
            raise ValueError(
                f"Artifact {name} resolves outside the artifact directory: {resolved}"
            )
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != record["sha256"]:
            raise ValueError(f"Artifact {name} does not match its recorded digest")
        _verify_artifact_identity(
            path, manifest["run_id"], manifest["model_version"], name
        )


def _next_publication_sequence(versioned_root, run_id) -> int:
    """The next unique suffix for a versioned publication of ``run_id``."""
    prefix = run_id + "~"
    sequences = [
        int(entry.name[len(prefix):])
        for entry in versioned_root.iterdir()
        if entry.name.startswith(prefix) and entry.name[len(prefix):].isdigit()
    ]
    sequence = max(sequences, default=0) + 1
    while (versioned_root / f"{run_id}~{sequence}").exists():
        sequence += 1
    return sequence


# Deterministic test seams: production never sets these, tests install them to
# pause publication at a specific point or to observe durability ordering.
_PUBLISH_HOOKS = {}


def _run_publish_hook(name) -> None:
    hook = _PUBLISH_HOOKS.get(name)
    if hook is not None:
        hook()


class _PublicationLock:
    """Cross-process exclusive lock serializing publication and garbage collection.

    Without synchronization, one publisher's garbage collection can delete
    another publisher's pending (installed but not yet live) version directory,
    and the second publisher then flips the live pointer onto the deleted
    directory — both calls return successfully while the published path is
    broken. Every publication holds this lock from version install through
    pointer flip and garbage collection, so no publisher ever removes a set
    another publisher is about to make live. ``flock`` is advisory but every
    publisher in this code base honors it, which is sufficient because
    publication is only ever performed by this module.
    """

    def __init__(self, versioned_root):
        # Sibling of the versioned namespace, not inside it, so the namespace
        # keeps containing exactly the immutable version directories.
        self._path = Path(str(versioned_root) + ".publication.lock")

    def __enter__(self):
        self._file = open(self._path, "w")
        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)
        _run_publish_hook("after_lock")
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()
        return False


def _fsync_path(path) -> None:
    """Flush ``path``'s file contents to durable storage."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path) -> None:
    """Flush ``path``'s directory entries (names and renames) to durable storage."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durability_barriers(versioned, versioned_root, target) -> bool:
    """Fsync the new set and pointer before any prior set may be removed.

    Ordering: every artifact file's contents are flushed first, then the
    installed version directory itself (persisting its artifact entries), then
    the versioned root (persisting the renamed immutable set), then the
    published pointer's parent (persisting the flipped symlink). Only after all
    barriers succeed may the superseded immutable sets be garbage-collected; if
    any barrier cannot be confirmed (for example on a filesystem that does not
    support directory fsync), the prior set is retained rather than risked.
    """
    try:
        for path in versioned.iterdir():
            if path.is_file():
                _fsync_path(path)
        _fsync_directory(versioned)
        _fsync_directory(versioned_root)
        _fsync_directory(target.parent)
    except OSError:
        return False
    return True


def _gc_published_runs(versioned_root, target) -> None:
    """Best-effort removal of superseded immutable sets, keeping the live one.

    Runs only after the pointer has been flipped, while holding the publication
    lock, and only after the new set and pointer have been durably committed, so
    a crash during garbage collection can strand unreachable sets but can never
    remove the set the published pointer resolves to or lose the new set before
    it is durable.
    """
    try:
        current = Path(target).resolve()
    except (OSError, RuntimeError):
        return
    for entry in versioned_root.iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.resolve() == current:
                continue
        except (OSError, RuntimeError):
            continue
        shutil.rmtree(entry, ignore_errors=True)


def publish_artifact_set(staging_dir, output_dir) -> None:
    """Crash-safely replace a published artifact set with a verified staged set.

    ``staging_dir`` must be a unique per-run/per-process directory containing
    every required persisted artifact plus the ``run_identity_manifest.json``
    written by ``artifact_manifest``. The staged set is verified only after it
    has been moved into the immutable, run-addressed versioned namespace while
    the publication lock is held, so the bytes that are verified are exactly the
    bytes that will be installed — no other publisher can reach the moved set to
    replace it, and a staging path that was mutated by another process fails the
    post-move verification and leaves ``output_dir`` untouched.

    Publication uses immutable versioned directories plus an atomically replaced
    pointer, serialized across publishers by a cross-process lock. The staged
    set is first moved into an immutable versioned directory (a sibling
    ``<name>-runs`` namespace); the externally observed ``output_dir`` is a
    symlink whose target is swapped in one atomic ``os.replace``. A crash before
    the swap leaves the previous set fully live; a crash after the swap
    publishes the new set. The old set survives until the new set and pointer
    are durably committed (every artifact file, the installed version directory,
    the versioned root, and the pointer's parent directory are fsynced) and the
    lock is still held, so SIGKILL or power loss can never leave the publication
    path absent, expose a partially replaced set, or delete the live set while
    another publisher is mid-swap.
    """
    staging = Path(staging_dir)
    target = Path(output_dir)
    if not staging.is_dir():
        raise FileNotFoundError(f"Staging directory is missing: {staging}")
    manifest_path = staging / "run_identity_manifest.json"
    if not manifest_path.exists():
        raise ValueError(f"Staging is missing the run identity manifest: {manifest_path}")
    if target.exists() and not target.is_symlink():
        raise ValueError(
            f"{target} is a real directory, not a versioned pointer; move it "
            "aside and republish so publication can stay atomic"
        )
    versioned_root = target.with_name(target.name + "-runs")
    versioned_root.mkdir(parents=True, exist_ok=True)
    _run_publish_hook("before_lock")
    with _PublicationLock(versioned_root):
        manifest = json.loads(manifest_path.read_text())
        run_id = manifest["run_id"]
        sequence = _next_publication_sequence(versioned_root, run_id)
        versioned = versioned_root / f"{run_id}~{sequence}"
        _run_publish_hook("before_version_install")
        # Atomic move of the staged set into the private immutable versioned
        # namespace, then verification of the moved set under the lock: another
        # process mutating the staging path after this point cannot inject
        # unverified bytes, because the moved set is unique to this publication
        # and unreachable through any shared path. Until the pointer swap below,
        # the published path still resolves to the previous set.
        os.replace(staging, versioned)
        _run_publish_hook("after_version_install")
        try:
            verify_persisted_manifest(manifest, versioned)
            _run_publish_hook("after_verify")
            pointer_tmp = versioned_root / f".current-{run_id}~{sequence}-{os.getpid()}"
            os.symlink(os.path.relpath(versioned, target.parent), pointer_tmp)
            _run_publish_hook("before_pointer_flip")
            try:
                # Atomic pointer flip: the externally observed path switches from
                # the old set to the new set in a single rename.
                os.replace(pointer_tmp, target)
            except BaseException:
                pointer_tmp.unlink(missing_ok=True)
                raise
            _run_publish_hook("after_pointer_flip")
        except BaseException:
            shutil.rmtree(versioned, ignore_errors=True)
            raise
        _run_publish_hook("before_gc")
        # Retain the prior immutable set until the new set and pointer are
        # durably committed; only then may the superseded sets be removed.
        durable = _durability_barriers(versioned, versioned_root, target)
        _run_publish_hook("after_durability")
        if durable:
            _gc_published_runs(versioned_root, target)
