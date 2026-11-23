#!/usr/bin/env python3
"""Build, test and install Heliograph in WSL.

Usage:
    python3 tools/install.py [--to DIR] [--store DIR] [--wrapper PATH]
    python3 tools/install.py --status [--to DIR] [--wrapper PATH]
    python3 tools/install.py --back [--to DIR] [--wrapper PATH]
    python3 tools/install.py --remove [--to DIR] [--wrapper PATH]
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

fcntl: Any = None
if os.name == "posix":
    import fcntl as _fcntl
    fcntl = _fcntl

DEFAULT_SDK_URL = "https://github.com/rizonesoft/ImpossibleSDK.git"
MANAGED_MARKER = "# Managed by Heliograph installer"
LEGACY_MARKER = "# Heliograph with this machine's store."
TARGET_PREFIX = "# target="
MANIFEST_NAME = "installed.json"
JOURNAL_NAME = "install.journal.json"
LOCK_NAME = ".install.lock"
WRAPPER_LOCK_NAME = ".helio-install.lock"
BUILD_LOCK_NAME = ".install-build.lock"
BINARY_NAMES = ("helio", "helio.exe")
BACKUP_NAMES = ("helio.old", "helio.exe.old")
RESERVED_NAMES = BINARY_NAMES + BACKUP_NAMES + (MANIFEST_NAME, JOURNAL_NAME)
HELIO_MARKERS = (b"HELIO_STORE", b"helio passes messages")
TX_ID_RE = re.compile(r"^[0-9a-f]{12}$")


class InstallError(Exception):
    """Refusal or operational failure with an explicit exit code."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


class BusyExecutableError(InstallError):
    """Raised when the host refuses atomic replacement of a busy executable."""

    def __init__(self, target: Path) -> None:
        super().__init__(
            f"{target} is in use and cannot be replaced atomically; deferring update so the existing program stays callable",
            code=3,
        )
        self.target = target


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_path_chars(path: Path, label: str) -> None:
    raw = str(path)
    if "\n" in raw or "\r" in raw or "\x00" in raw:
        raise InstallError(f"refusing {label} path containing control characters", code=2)


def _normalize_wrapper_path(path: Path) -> Path:
    """Resolve the parent directory without following a symlink at the final component."""
    _validate_path_chars(path, "wrapper")
    if not path.name:
        raise InstallError(f"invalid wrapper path: {path}", code=2)
    return path.parent.resolve() / path.name


def _redact_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.username is None and parsed.password is None:
        return url
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"***@{host}" + (f":{parsed.port}" if parsed.port is not None else "")
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _validate_sdk_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.username is not None or parsed.password is not None:
        raise InstallError(
            f"refusing SDK URL with embedded credentials ({_redact_url(url)}); use a git credential helper or SSH",
            code=2,
        )


def fsync_dir(directory: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.EBADF, errno.EROFS):
                raise
    finally:
        os.close(fd)


def write_bytes_atomic(path: Path, data: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode if mode is not None else 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None and os.name == "posix":
            os.chmod(tmp, mode)
        os.replace(tmp, path)
        fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    write_bytes_atomic(path, raw, mode=0o644)


def copy_to_staged(src: Path, dst: Path, mode: int = 0o755) -> None:
    """Create a fresh staged file with O_CREAT|O_EXCL so pre-existing unrelated files are never unlinked."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    data = src.read_bytes()
    fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name == "posix":
            os.chmod(dst, mode)
    except BaseException:
        with contextlib.suppress(OSError):
            dst.unlink()
        raise


def replace_binary_atomic(staged: Path, target: Path) -> None:
    try:
        os.replace(staged, target)
    except PermissionError:
        raise BusyExecutableError(target) from None
    except OSError as exc:
        winerr = getattr(exc, "winerror", None)
        if exc.errno in (errno.EACCES, errno.EBUSY, errno.ETXTBSY) or winerr in (5, 32):
            raise BusyExecutableError(target) from None
        raise
    fsync_dir(target.parent)


def _check_not_symlink_or_dir(path: Path) -> None:
    if path.is_symlink():
        raise InstallError(f"refusing symlink at reserved path: {path}", code=2)
    if path.exists() and not path.is_file():
        raise InstallError(f"refusing non-file at reserved path: {path}", code=2)


@contextlib.contextmanager
def acquire_lock(lock_path: Path, *, blocking: bool) -> Any:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise InstallError(f"refusing symlink lock file: {lock_path}", code=2)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if fcntl is not None:
            flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                fcntl.flock(fd, flags)
            except BlockingIOError:
                raise InstallError(f"another Heliograph installer run holds {lock_path}", code=2) from None
        yield
    finally:
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


@contextlib.contextmanager
def acquire_ordered_locks(to_dir: Path, *wrapper_paths: Path) -> Any:
    """Acquire destination lock first, then distinct wrapper-dir locks in sorted order to prevent deadlocks."""
    to_lock = (to_dir / LOCK_NAME).resolve()
    extra_locks = sorted(
        {
            (wp.parent / WRAPPER_LOCK_NAME).resolve()
            for wp in wrapper_paths
            if (wp.parent / WRAPPER_LOCK_NAME).resolve() != to_lock
        },
        key=str,
    )
    with contextlib.ExitStack() as stack:
        stack.enter_context(acquire_lock(to_lock, blocking=False))
        for wl in extra_locks:
            stack.enter_context(acquire_lock(wl, blocking=True))
        yield


def _validate_manifest_dict(data: Any, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise InstallError(f"unrecognized or corrupt manifest: {path}", code=2)
    cur = data.get("current")
    if not isinstance(cur, dict):
        raise InstallError(f"manifest missing current record: {path}", code=2)
    for key in ("commit", "helio_sha256", "helio_exe_sha256"):
        if not isinstance(cur.get(key), str) or not cur[key]:
            raise InstallError(f"manifest missing current.{key}: {path}", code=2)
    prev = data.get("previous")
    if prev is not None:
        if not isinstance(prev, dict):
            raise InstallError(f"manifest has invalid previous record: {path}", code=2)
        for key in ("commit", "helio_sha256", "helio_exe_sha256"):
            if not isinstance(prev.get(key), str) or not prev[key]:
                raise InstallError(f"manifest missing previous.{key}: {path}", code=2)
    return data


def read_manifest(to_dir: Path) -> dict[str, Any] | None:
    manifest_path = to_dir / MANIFEST_NAME
    if not manifest_path.exists() and not manifest_path.is_symlink():
        return None
    _check_not_symlink_or_dir(manifest_path)
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallError(f"unreadable or foreign {manifest_path}: {exc}", code=2) from None
    return _validate_manifest_dict(data, manifest_path)


def _file_has_magic_and_markers(path: Path, magic: bytes, markers: tuple[bytes, ...]) -> bool:
    """Inspect magic bytes in O(1) memory, then stream chunks for markers without executing the file."""
    overlap = max(len(m) for m in markers)
    found = [False] * len(markers)
    try:
        with path.open("rb") as fh:
            head = fh.read(len(magic))
            if head != magic:
                return False
            buf = head
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                window = buf + chunk
                for idx, marker in enumerate(markers):
                    if not found[idx] and marker in window:
                        found[idx] = True
                if all(found):
                    return True
                buf = window[-overlap:]
    except OSError:
        return False
    return all(found)


def _is_heliograph_elf(path: Path) -> bool:
    return _file_has_magic_and_markers(path, b"\x7fELF", HELIO_MARKERS)


def _is_heliograph_pe(path: Path) -> bool:
    return _file_has_magic_and_markers(path, b"MZ", HELIO_MARKERS)


def verify_destination_ownership(to_dir: Path, manifest: dict[str, Any] | None, *, require_live: bool = False) -> bool:
    """Verify every reserved path in to_dir belongs to Heliograph.

    Returns True if a verified legacy manual installation (no manifest) is being adopted.
    """
    for name in RESERVED_NAMES:
        _check_not_symlink_or_dir(to_dir / name)

    if manifest is not None:
        cur = manifest["current"]
        for name, key in (("helio", "helio_sha256"), ("helio.exe", "helio_exe_sha256")):
            p = to_dir / name
            if not p.exists():
                if require_live:
                    raise InstallError(f"missing live binary {p}; remove or restore it before replacing", code=2)
                continue
            if sha256_file(p) != cur[key]:
                raise InstallError(f"refusing modified or unowned file at {p}: hash does not match {MANIFEST_NAME}", code=2)
        prev = manifest.get("previous")
        for name, key in (("helio.old", "helio_sha256"), ("helio.exe.old", "helio_exe_sha256")):
            p = to_dir / name
            if not p.exists():
                continue
            if prev is None:
                raise InstallError(f"refusing unowned file at {p}: manifest records no previous installation", code=2)
            if sha256_file(p) != prev[key]:
                raise InstallError(f"refusing modified or unowned backup at {p}: hash does not match {MANIFEST_NAME}", code=2)
        return False

    present_bins = [name for name in BINARY_NAMES + BACKUP_NAMES if (to_dir / name).exists()]
    if not present_bins:
        return False

    for name in present_bins:
        p = to_dir / name
        if name in ("helio", "helio.old"):
            if not _is_heliograph_elf(p):
                raise InstallError(f"refusing unrelated file at {p}: not a Heliograph executable", code=2)
        else:
            if not _is_heliograph_pe(p):
                raise InstallError(f"refusing unrelated file at {p}: not a Heliograph Windows executable", code=2)

    return (to_dir / "helio").exists() and (to_dir / "helio.exe").exists()


def _parse_target_line(raw_value: str) -> str:
    raw_value = raw_value.strip()
    if raw_value.startswith('"'):
        try:
            decoded = json.loads(raw_value)
            if isinstance(decoded, str):
                return decoded
        except ValueError:
            pass
    return raw_value


def parse_wrapper_ownership(wrapper_path: Path) -> tuple[str, str | None]:
    """Return (kind, bound_target_dir) for wrapper_path without following a final symlink.

    kind is one of: 'absent', 'managed', 'legacy', 'foreign'.
    """
    if wrapper_path.is_symlink():
        return ("foreign", None)
    if not wrapper_path.exists():
        return ("absent", None)
    if not wrapper_path.is_file():
        return ("foreign", None)
    try:
        text = wrapper_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ("foreign", None)
    lines = text.splitlines()
    if not lines or lines[0].strip() != "#!/bin/sh":
        return ("foreign", None)
    if any(line.strip() == MANAGED_MARKER for line in lines):
        target = None
        for line in lines:
            if line.startswith(TARGET_PREFIX):
                target = _parse_target_line(line[len(TARGET_PREFIX):])
                break
        return ("managed", target)
    if any(line.strip().startswith(LEGACY_MARKER) for line in lines) and "helio" in text:
        target = None
        for line in lines:
            if "exec " in line and "/helio" in line:
                part = line.split("exec ", 1)[1].strip()
                try:
                    tokens = shlex.split(part)
                except ValueError:
                    tokens = []
                if tokens:
                    expanded = os.path.expandvars(tokens[0])
                    target = str(Path(expanded).parent.resolve())
        return ("legacy", target)
    return ("foreign", None)


def wrapper_belongs_to(wrapper_path: Path, to_dir: Path) -> bool:
    kind, bound = parse_wrapper_ownership(wrapper_path)
    if kind not in ("managed", "legacy"):
        return False
    if bound is None:
        return False
    return Path(bound).resolve() == to_dir.resolve()


def _wrapper_is_available_for(wrapper_path: Path, to_dir: Path) -> bool:
    """True only if wrapper_path is absent or already owned by to_dir (never another destination)."""
    kind, _ = parse_wrapper_ownership(wrapper_path)
    return kind == "absent" or wrapper_belongs_to(wrapper_path, to_dir)


def render_wrapper(to_dir: Path, store_dir: Path) -> str:
    resolved_to = str(to_dir.resolve())
    helio_bin = str((to_dir / "helio").resolve())
    resolved_store = str(store_dir.resolve())
    return (
        "#!/bin/sh\n"
        f"{MANAGED_MARKER}\n"
        f"{TARGET_PREFIX}{json.dumps(resolved_to)}\n"
        'if [ -z "${HELIO_STORE:-}" ]; then\n'
        f"  HELIO_STORE={shlex.quote(resolved_store)}\n"
        "  export HELIO_STORE\n"
        "fi\n"
        f"exec {shlex.quote(helio_bin)} \"$@\"\n"
    )


def _is_valid_tx_temp_name(name: str, tx_id: str) -> bool:
    if not isinstance(name, str) or "/" in name or "\\" in name:
        return False
    for base in BINARY_NAMES + BACKUP_NAMES:
        if name in (f".{base}.{tx_id}.stage", f".{base}.{tx_id}.rec"):
            return True
    return False


def _cleanup_temp_files(to_dir: Path, temp_names: list[str], tx_id: str) -> None:
    if not to_dir.is_dir() or not temp_names:
        return
    resolved_to = to_dir.resolve()
    for name in temp_names:
        if not _is_valid_tx_temp_name(name, tx_id):
            continue
        p = to_dir / name
        if p.parent.resolve() != resolved_to or p.is_symlink():
            continue
        with contextlib.suppress(OSError):
            p.unlink()
    fsync_dir(to_dir)


def _validate_journal_paths(journal: Any, to_dir: Path, wrapper_path: Path) -> dict[str, Any]:
    if not isinstance(journal, dict):
        raise InstallError("corrupt or foreign install journal: not a JSON object", code=2)
    if journal.get("schema_version") != 1:
        raise InstallError("unrecognized install journal schema", code=2)
    op = journal.get("op")
    if op not in ("install", "back", "remove"):
        raise InstallError("invalid install journal operation", code=2)
    phase = journal.get("phase", "publishing")
    if phase not in ("staging", "publishing"):
        raise InstallError("invalid install journal phase", code=2)
    tx_id = journal.get("tx_id")
    if not isinstance(tx_id, str) or not TX_ID_RE.fullmatch(tx_id):
        raise InstallError("invalid install journal tx_id", code=2)
    if Path(str(journal.get("to_dir", ""))).resolve() != to_dir.resolve():
        raise InstallError("install journal target directory mismatch", code=2)
    j_wrapper = journal.get("wrapper_path")
    if j_wrapper is not None and _normalize_wrapper_path(Path(str(j_wrapper))) != wrapper_path:
        raise InstallError("install journal wrapper path mismatch", code=2)
    temp_files = journal.get("temp_files")
    if not isinstance(temp_files, list) or any(not _is_valid_tx_temp_name(t, tx_id) for t in temp_files):
        raise InstallError("install journal temp_files list contains invalid entries", code=2)
    if phase == "staging":
        return journal
    saved = journal.get("saved_files", {})
    if not isinstance(saved, dict):
        raise InstallError("install journal saved_files is invalid", code=2)
    for name, meta in saved.items():
        if name not in BINARY_NAMES + BACKUP_NAMES:
            raise InstallError(f"install journal names unowned file: {name}", code=2)
        if not isinstance(meta, dict):
            raise InstallError(f"install journal entry invalid for {name}", code=2)
        rec_name = meta.get("rec_name")
        if rec_name is not None and rec_name != f".{name}.{tx_id}.rec":
            raise InstallError(f"install journal recovery path invalid: {rec_name}", code=2)
    alt_wrapper = journal.get("alt_wrapper_path")
    if alt_wrapper is not None:
        if op not in ("back", "remove"):
            raise InstallError("install journal may not specify alt_wrapper_path", code=2)
        alt_norm = _normalize_wrapper_path(Path(str(alt_wrapper)))
        allowed_alts: set[Path] = set()
        for mkey in ("previous_manifest", "desired_manifest"):
            m = journal.get(mkey)
            if isinstance(m, dict) and isinstance(m.get("current"), dict) and isinstance(m["current"].get("wrapper"), str):
                allowed_alts.add(_normalize_wrapper_path(Path(m["current"]["wrapper"])))
        if alt_norm not in allowed_alts:
            raise InstallError("journal alt_wrapper_path is not bound to a recorded manifest wrapper", code=2)
    if op == "remove":
        if temp_files:
            raise InstallError("remove journal may not list temp_files for deletion", code=2)
        expected = journal.get("expected_hashes")
        if not isinstance(expected, dict):
            raise InstallError("remove journal missing expected_hashes ownership record", code=2)
        for name, digest in expected.items():
            if name not in BINARY_NAMES + BACKUP_NAMES + (MANIFEST_NAME,):
                raise InstallError(f"remove journal names unowned file: {name}", code=2)
            if not isinstance(digest, str) or len(digest) != 64:
                raise InstallError(f"remove journal has invalid hash for {name}", code=2)
    return journal


def _verify_published_set(to_dir: Path, manifest: dict[str, Any]) -> bool:
    """Confirm live binaries, recorded backups, bound wrapper, and store all match the committed manifest."""
    cur = manifest["current"]
    for name, key in (("helio", "helio_sha256"), ("helio.exe", "helio_exe_sha256")):
        p = to_dir / name
        if p.is_symlink() or not p.is_file() or sha256_file(p) != cur[key]:
            return False
    prev = manifest.get("previous")
    if prev is not None:
        for name, key in (("helio.old", "helio_sha256"), ("helio.exe.old", "helio_exe_sha256")):
            p = to_dir / name
            if p.is_symlink() or not p.is_file() or sha256_file(p) != prev[key]:
                return False
    w_raw = cur.get("wrapper")
    if isinstance(w_raw, str) and w_raw:
        wp = _normalize_wrapper_path(Path(w_raw))
        if not wrapper_belongs_to(wp, to_dir):
            return False
    s_raw = cur.get("store")
    if isinstance(s_raw, str) and s_raw:
        sp = Path(s_raw)
        if sp.is_symlink() or not sp.is_dir() or not (sp / "messages").is_dir():
            return False
    return True


def _restore_wrapper_from_manifest(wrapper_path: Path, to_dir: Path, store_str: str | None) -> None:
    """Restore a wrapper deterministically from validated (to_dir, store), never arbitrary journal text."""
    if not store_str:
        return
    if _wrapper_is_available_for(wrapper_path, to_dir):
        content = render_wrapper(to_dir, Path(store_str))
        write_bytes_atomic(wrapper_path, content.encode("utf-8"), mode=0o755)


def recover_if_needed(to_dir: Path, wrapper_path: Path) -> bool:
    """Recover from a leftover install.journal.json. Returns True if recovery ran."""
    journal_path = to_dir / JOURNAL_NAME
    if not journal_path.exists() and not journal_path.is_symlink():
        return False
    _check_not_symlink_or_dir(journal_path)
    try:
        raw_journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallError(f"corrupt or foreign journal at {journal_path}: {exc}", code=2) from None
    journal = _validate_journal_paths(raw_journal, to_dir, wrapper_path)

    op = journal["op"]
    tx_id = journal["tx_id"]
    phase = journal.get("phase", "publishing")
    temp_files: list[str] = list(journal.get("temp_files", []))

    if phase == "staging":
        # Stopped before any live target was mutated: simply clean up any created tx temp files and journal.
        _cleanup_temp_files(to_dir, temp_files, tx_id)
        journal_path.unlink()
        fsync_dir(to_dir)
        return True

    if op == "remove":
        expected_hashes: dict[str, str] = journal["expected_hashes"]
        for name in BINARY_NAMES + BACKUP_NAMES + (MANIFEST_NAME,):
            p = to_dir / name
            if not p.exists() and not p.is_symlink():
                continue
            _check_not_symlink_or_dir(p)
            if name not in expected_hashes:
                raise InstallError(f"remove recovery refusing unexpected file {p} not recorded in journal", code=2)
            if sha256_file(p) != expected_hashes[name]:
                raise InstallError(f"remove recovery refusing modified file {p} whose hash changed since removal started", code=2)
        for name in BINARY_NAMES + BACKUP_NAMES + (MANIFEST_NAME,):
            p = to_dir / name
            if p.exists():
                p.unlink()
        wrappers_to_clean = [wrapper_path]
        if isinstance(journal.get("alt_wrapper_path"), str):
            alt_wp = _normalize_wrapper_path(Path(journal["alt_wrapper_path"]))
            if alt_wp != wrapper_path:
                wrappers_to_clean.append(alt_wp)
        for wp in wrappers_to_clean:
            if wrapper_belongs_to(wp, to_dir):
                wp.unlink()
                fsync_dir(wp.parent)
        journal_path.unlink()
        fsync_dir(to_dir)
        return True

    # For 'install' and 'back': check if commit point (installed.json == desired_manifest) was reached.
    desired_manifest = journal.get("desired_manifest")
    prev_manifest = journal.get("previous_manifest")
    current_manifest = None
    manifest_file = to_dir / MANIFEST_NAME
    if manifest_file.exists() or manifest_file.is_symlink():
        current_manifest = read_manifest(to_dir)

    if desired_manifest is not None and current_manifest == desired_manifest:
        if not _verify_published_set(to_dir, desired_manifest):
            raise InstallError("recovery failed: committed manifest does not match published binaries, backups, wrapper or store", code=3)
        _cleanup_temp_files(to_dir, temp_files, tx_id)
        journal_path.unlink()
        fsync_dir(to_dir)
        return True

    # Verify manifest ownership before rolling it back: if it exists, it must equal previous_manifest or desired_manifest.
    if current_manifest is not None and current_manifest != prev_manifest and current_manifest != desired_manifest:
        raise InstallError(f"recovery refusing to overwrite externally modified {manifest_file}", code=2)

    # Commit point not reached: roll back every file in the set from .rec snapshots after verifying ownership.
    saved: dict[str, dict[str, Any]] = journal.get("saved_files", {})
    desired_cur = (desired_manifest or {}).get("current", {})
    desired_prev = (desired_manifest or {}).get("previous") or {}
    desired_hashes = {
        "helio": desired_cur.get("helio_sha256"),
        "helio.exe": desired_cur.get("helio_exe_sha256"),
        "helio.old": desired_prev.get("helio_sha256"),
        "helio.exe.old": desired_prev.get("helio_exe_sha256"),
    }

    for name in BINARY_NAMES + BACKUP_NAMES:
        target = to_dir / name
        if not target.exists() and not target.is_symlink():
            continue
        _check_not_symlink_or_dir(target)
        actual = sha256_file(target)
        allowed = {h for h in (saved.get(name, {}).get("sha256"), desired_hashes.get(name)) if h}
        if actual not in allowed:
            raise InstallError(f"recovery refusing to overwrite externally modified file at {target}", code=2)

    for name in BINARY_NAMES + BACKUP_NAMES:
        target = to_dir / name
        meta = saved.get(name)
        if meta and meta.get("existed"):
            rec_path = to_dir / meta["rec_name"]
            expected_hash = meta["sha256"]
            if target.exists() and sha256_file(target) == expected_hash:
                pass
            elif rec_path.exists() and sha256_file(rec_path) == expected_hash:
                staged_restore = to_dir / f".{name}.{tx_id}.restore"
                copy_to_staged(rec_path, staged_restore, mode=0o755)
                replace_binary_atomic(staged_restore, target)
            else:
                raise InstallError(f"recovery failed: cannot restore {target} with expected hash", code=3)
        else:
            if target.exists():
                target.unlink()
    fsync_dir(to_dir)

    # Restore wrapper deterministically from previous_manifest (or remove newly created wrapper), never clobbering another destination.
    prev_store = prev_manifest["current"].get("store") if isinstance(prev_manifest, dict) else None
    if journal.get("previous_wrapper_existed") and prev_store:
        _restore_wrapper_from_manifest(wrapper_path, to_dir, prev_store)
    elif not journal.get("previous_wrapper_existed"):
        if wrapper_belongs_to(wrapper_path, to_dir):
            wrapper_path.unlink()
            fsync_dir(wrapper_path.parent)

    if op == "back" and isinstance(journal.get("alt_wrapper_path"), str):
        alt_wp = _normalize_wrapper_path(Path(journal["alt_wrapper_path"]))
        if alt_wp != wrapper_path and wrapper_belongs_to(alt_wp, to_dir):
            if not journal.get("alt_wrapper_existed"):
                alt_wp.unlink()
                fsync_dir(alt_wp.parent)
            elif prev_store:
                _restore_wrapper_from_manifest(alt_wp, to_dir, prev_store)

    # Restore manifest state.
    if journal.get("had_manifest") and isinstance(prev_manifest, dict):
        write_json_atomic(manifest_file, prev_manifest)
    else:
        if manifest_file.exists() and not manifest_file.is_symlink():
            manifest_file.unlink()
            fsync_dir(to_dir)

    _cleanup_temp_files(to_dir, temp_files, tx_id)
    journal_path.unlink()
    fsync_dir(to_dir)
    return True


def _snapshot_files(to_dir: Path, tx_id: str) -> dict[str, dict[str, Any]]:
    saved: dict[str, dict[str, Any]] = {}
    for name in BINARY_NAMES + BACKUP_NAMES:
        p = to_dir / name
        if p.exists() and not p.is_symlink():
            rec_name = f".{name}.{tx_id}.rec"
            copy_to_staged(p, to_dir / rec_name, mode=0o755)
            saved[name] = {
                "existed": True,
                "rec_name": rec_name,
                "sha256": sha256_file(to_dir / rec_name),
            }
        else:
            saved[name] = {"existed": False, "rec_name": None, "sha256": None}
    fsync_dir(to_dir)
    return saved


def _planned_temp_names(tx_id: str) -> list[str]:
    names: list[str] = []
    for base in BINARY_NAMES + BACKUP_NAMES:
        names.append(f".{base}.{tx_id}.rec")
        names.append(f".{base}.{tx_id}.stage")
    return names


def git_provenance(repo: Path) -> tuple[str, bool]:
    rev = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True)
    if rev.returncode != 0:
        raise InstallError(f"cannot read git HEAD in {repo}: {rev.stderr.strip()}", code=2)
    commit = rev.stdout.strip()
    st = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
    if st.returncode != 0:
        raise InstallError(f"cannot read git status in {repo}: {st.stderr.strip()}", code=2)
    dirty = bool(st.stdout.strip())
    return commit, dirty


def ensure_sdk_and_build(repo: Path, sdk_url: str, stage_dir: Path) -> tuple[Path, Path]:
    """Ensure Impossible SDK exists beside repo, build+test under checkout build lock, and snapshot outputs."""
    _validate_sdk_url(sdk_url)
    sdk_dir = repo.parent / "ImpossibleSDK"
    build_lock = repo / "build" / BUILD_LOCK_NAME
    with acquire_lock(build_lock, blocking=True):
        if not sdk_dir.exists():
            clone = subprocess.run(["git", "clone", sdk_url, str(sdk_dir)], check=False)
            if clone.returncode != 0:
                raise InstallError(f"failed to clone Impossible SDK from {_redact_url(sdk_url)} into {sdk_dir}", code=3)
        app_build = sdk_dir / "tools" / "app_build.py"
        if not app_build.is_file():
            raise InstallError(f"Impossible SDK at {sdk_dir} is missing tools/app_build.py", code=3)
        cmd = [sys.executable, str(app_build), str(repo)]
        bosun = shutil.which("bosun")
        if bosun and not os.environ.get("HELIO_NO_BOSUN"):
            cmd = [bosun, "run", "--", *cmd]
        build = subprocess.run(cmd, check=False)
        if build.returncode != 0:
            raise InstallError(f"build/test failed (exit {build.returncode}); nothing was installed", code=build.returncode)

        helio_bin = repo / "build" / "linux" / "linux" / "helio"
        helio_exe = repo / "build" / "linux" / "helio.exe"
        for p in (helio_bin, helio_exe):
            if not p.is_file():
                raise InstallError(f"expected built program is missing: {p}", code=3)
        snap_helio = stage_dir / "helio"
        snap_exe = stage_dir / "helio.exe"
        copy_to_staged(helio_bin, snap_helio, mode=0o755)
        copy_to_staged(helio_exe, snap_exe, mode=0o755)
        return snap_helio, snap_exe


def _ensure_store_ready(helio_bin: Path, store_dir: Path) -> None:
    if store_dir.is_symlink():
        raise InstallError(f"refusing symlink store path: {store_dir}", code=2)
    if store_dir.exists():
        if not store_dir.is_dir():
            raise InstallError(f"refusing non-directory store path: {store_dir}", code=2)
        if (store_dir / "messages").is_dir():
            return
        if any(store_dir.iterdir()):
            raise InstallError(f"store directory {store_dir} is non-empty and not initialized (missing messages/)", code=2)
    store_dir.parent.mkdir(parents=True, exist_ok=True)
    init_res = subprocess.run(
        [str(helio_bin), "init", "--store", str(store_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    if init_res.returncode != 0 or not (store_dir / "messages").is_dir():
        raise InstallError(
            f"failed to initialize store at {store_dir}: {init_res.stderr.strip() or init_res.stdout.strip()}",
            code=3,
        )


def _install_signal_handlers() -> dict[int, Any]:
    prev: dict[int, Any] = {}

    def _raise_interrupt(signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt(f"signal {signum}")

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):
            prev[sig] = signal.getsignal(sig)
            signal.signal(sig, _raise_interrupt)
    return prev


def _restore_signal_handlers(prev: dict[int, Any]) -> None:
    for sig, handler in prev.items():
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, handler)


def install(
    *,
    repo: Path,
    to_dir: Path,
    store_dir: Path,
    wrapper_path: Path,
    sdk_url: str = DEFAULT_SDK_URL,
    prebuilt: tuple[Path, Path] | None = None,
    on_boundary: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Build (unless prebuilt is supplied for internal/phase callers) and install Heliograph."""
    _validate_path_chars(to_dir, "destination")
    _validate_path_chars(store_dir, "store")
    to_dir = to_dir.resolve()
    store_dir = store_dir.resolve()
    wrapper_path = _normalize_wrapper_path(wrapper_path)

    for name in RESERVED_NAMES:
        if wrapper_path == to_dir / name:
            raise InstallError(f"wrapper path {wrapper_path} collides with destination file {to_dir / name}", code=2)

    commit, dirty = git_provenance(repo)

    with tempfile.TemporaryDirectory(prefix="helio-install-snap-") as snap_tmp:
        snap_dir = Path(snap_tmp)
        if prebuilt is None:
            helio_src, helio_exe_src = ensure_sdk_and_build(repo, sdk_url, snap_dir)
        else:
            helio_src, helio_exe_src = prebuilt
            for p in (helio_src, helio_exe_src):
                if not p.is_file():
                    raise InstallError(f"expected program is missing: {p}", code=3)

        new_helio_sha = sha256_file(helio_src)
        new_exe_sha = sha256_file(helio_exe_src)

        to_dir.mkdir(parents=True, exist_ok=True)
        wrapper_path.parent.mkdir(parents=True, exist_ok=True)

        prev_handlers = _install_signal_handlers()
        try:
            with acquire_ordered_locks(to_dir, wrapper_path):
                recover_if_needed(to_dir, wrapper_path)

                existing_manifest = read_manifest(to_dir)
                # Require both live binaries intact whenever replacing an existing managed installation
                # so every successful replacement guarantees a complete, verifiable previous pair.
                adopting_legacy = verify_destination_ownership(
                    to_dir,
                    existing_manifest,
                    require_live=existing_manifest is not None,
                )

                wrap_kind, _ = parse_wrapper_ownership(wrapper_path)
                if wrap_kind == "foreign":
                    raise InstallError(f"refusing unowned wrapper at {wrapper_path}", code=2)

                prev_wrapper_existed = wrap_kind in ("managed", "legacy")

                tx_id = secrets.token_hex(6)
                temp_files = _planned_temp_names(tx_id)

                # Write staging journal BEFORE creating any .rec/.stage files so even SIGKILL during staging leaves zero leaks.
                staging_journal = {
                    "schema_version": 1,
                    "op": "install",
                    "phase": "staging",
                    "tx_id": tx_id,
                    "to_dir": str(to_dir),
                    "wrapper_path": str(wrapper_path),
                    "temp_files": temp_files,
                }
                write_json_atomic(to_dir / JOURNAL_NAME, staging_journal)

                try:
                    saved = _snapshot_files(to_dir, tx_id)
                    if on_boundary:
                        on_boundary("after_snapshot")

                    stage_helio = to_dir / f".helio.{tx_id}.stage"
                    stage_exe = to_dir / f".helio.exe.{tx_id}.stage"
                    copy_to_staged(helio_src, stage_helio, mode=0o755)
                    copy_to_staged(helio_exe_src, stage_exe, mode=0o755)

                    rotate_live_to_old = False
                    previous_entry: dict[str, Any] | None = None
                    if existing_manifest is not None:
                        prev_cur = existing_manifest["current"]
                        previous_entry = {
                            "commit": prev_cur["commit"],
                            "dirty": bool(prev_cur.get("dirty", False)),
                            "installed_at": prev_cur.get("installed_at"),
                            "helio_sha256": prev_cur["helio_sha256"],
                            "helio_exe_sha256": prev_cur["helio_exe_sha256"],
                            "store": prev_cur.get("store", str(store_dir)),
                            "wrapper": prev_cur.get("wrapper", str(wrapper_path)),
                        }
                        rotate_live_to_old = True
                    elif adopting_legacy:
                        previous_entry = {
                            "commit": "legacy",
                            "dirty": False,
                            "installed_at": None,
                            "helio_sha256": saved["helio"]["sha256"],
                            "helio_exe_sha256": saved["helio.exe"]["sha256"],
                            "store": str(store_dir),
                            "wrapper": str(wrapper_path),
                        }
                        rotate_live_to_old = True

                    desired_manifest = {
                        "schema_version": 1,
                        "current": {
                            "commit": commit,
                            "dirty": dirty,
                            "installed_at": utc_now(),
                            "helio_sha256": new_helio_sha,
                            "helio_exe_sha256": new_exe_sha,
                            "store": str(store_dir),
                            "wrapper": str(wrapper_path),
                        },
                        "previous": previous_entry,
                    }
                    desired_wrapper = render_wrapper(to_dir, store_dir)

                    journal = {
                        "schema_version": 1,
                        "op": "install",
                        "phase": "publishing",
                        "tx_id": tx_id,
                        "to_dir": str(to_dir),
                        "wrapper_path": str(wrapper_path),
                        "store_dir": str(store_dir),
                        "had_manifest": existing_manifest is not None,
                        "previous_manifest": existing_manifest,
                        "previous_wrapper_existed": prev_wrapper_existed,
                        "saved_files": saved,
                        "temp_files": temp_files,
                        "desired_manifest": desired_manifest,
                    }
                    write_json_atomic(to_dir / JOURNAL_NAME, journal)
                    if on_boundary:
                        on_boundary("after_journal")

                    # 1. Rotate backups from saved live files.
                    if rotate_live_to_old:
                        bak_h = to_dir / f".helio.old.{tx_id}.stage"
                        bak_e = to_dir / f".helio.exe.old.{tx_id}.stage"
                        copy_to_staged(to_dir / saved["helio"]["rec_name"], bak_h, mode=0o755)
                        replace_binary_atomic(bak_h, to_dir / "helio.old")
                        if on_boundary:
                            on_boundary("after_backup_helio")
                        copy_to_staged(to_dir / saved["helio.exe"]["rec_name"], bak_e, mode=0o755)
                        replace_binary_atomic(bak_e, to_dir / "helio.exe.old")
                        if on_boundary:
                            on_boundary("after_backup_exe")

                    # 2. Publish live executables atomically.
                    replace_binary_atomic(stage_helio, to_dir / "helio")
                    if on_boundary:
                        on_boundary("after_helio")

                    replace_binary_atomic(stage_exe, to_dir / "helio.exe")
                    if on_boundary:
                        on_boundary("after_helio_exe")

                    # 3. Publish destination-bound wrapper.
                    write_bytes_atomic(wrapper_path, desired_wrapper.encode("utf-8"), mode=0o755)
                    if on_boundary:
                        on_boundary("after_wrapper")

                    # 4. Ensure store is initialized (never touching an already-initialized store).
                    _ensure_store_ready(to_dir / "helio", store_dir)

                    # 5. Commit manifest (atomic commit point).
                    write_json_atomic(to_dir / MANIFEST_NAME, desired_manifest)
                    if on_boundary:
                        on_boundary("after_manifest")

                    # 6. Remove journal and transaction temp files.
                    (to_dir / JOURNAL_NAME).unlink()
                    _cleanup_temp_files(to_dir, temp_files, tx_id)
                    return desired_manifest
                except BaseException as exc:
                    if getattr(exc, "_leave_journal_for_test", False):
                        raise
                    with contextlib.suppress(InstallError, OSError):
                        recover_if_needed(to_dir, wrapper_path)
                    raise
        finally:
            _restore_signal_handlers(prev_handlers)


def status(*, to_dir: Path, wrapper_path: Path) -> tuple[int, list[str]]:
    """Inspect installation health by independently hashing live binaries and checking executability."""
    to_dir = to_dir.resolve()
    wrapper_path = _normalize_wrapper_path(wrapper_path)
    lines: list[str] = []

    if not to_dir.exists():
        if wrapper_belongs_to(wrapper_path, to_dir):
            return (3, [f"partial installation: wrapper {wrapper_path} points to missing destination {to_dir}"])
        return (1, [f"not installed: {to_dir} does not exist"])

    journal_path = to_dir / JOURNAL_NAME
    if journal_path.exists() or journal_path.is_symlink():
        return (3, [f"interrupted operation pending recovery: {journal_path}"])

    manifest_path = to_dir / MANIFEST_NAME
    if not manifest_path.exists() and not manifest_path.is_symlink():
        present = [n for n in BINARY_NAMES + BACKUP_NAMES if (to_dir / n).exists() or (to_dir / n).is_symlink()]
        has_bound_wrapper = wrapper_belongs_to(wrapper_path, to_dir)
        if not present and not has_bound_wrapper:
            return (1, [f"not installed in {to_dir}"])
        parts = list(present) + ([f"wrapper {wrapper_path}"] if has_bound_wrapper else [])
        return (3, [f"partial or unmanaged installation in {to_dir}: {', '.join(parts)} present without {MANIFEST_NAME}"])

    try:
        manifest = read_manifest(to_dir)
    except InstallError as exc:
        return (3, [str(exc)])
    assert manifest is not None

    cur = manifest["current"]
    commit_label = f"{cur['commit']} (dirty)" if cur.get("dirty") else cur["commit"]
    lines.append(f"commit: {commit_label}")
    if cur.get("installed_at"):
        lines.append(f"installed: {cur['installed_at']}")
    if cur.get("store"):
        lines.append(f"store: {cur['store']}")

    problems: list[str] = []
    for name, key in (("helio", "helio_sha256"), ("helio.exe", "helio_exe_sha256")):
        p = to_dir / name
        if p.is_symlink() or not p.is_file():
            problems.append(f"missing or invalid binary: {p}")
        else:
            try:
                actual = sha256_file(p)
            except OSError as exc:
                problems.append(f"unreadable binary: {p} ({exc})")
            else:
                if actual != cur[key]:
                    problems.append(f"modified binary: {p} (recorded {cur[key][:12]}, actual {actual[:12]})")
            if name == "helio" and os.name == "posix" and not os.access(p, os.X_OK):
                problems.append(f"not executable: {p}")

    if wrapper_path.is_symlink() or not wrapper_path.exists():
        problems.append(f"missing or invalid wrapper: {wrapper_path}")
    elif not wrapper_belongs_to(wrapper_path, to_dir):
        kind, bound = parse_wrapper_ownership(wrapper_path)
        problems.append(f"wrapper {wrapper_path} does not point to {to_dir} (state={kind}, target={bound})")
    elif os.name == "posix" and not os.access(wrapper_path, os.X_OK):
        problems.append(f"not executable: {wrapper_path}")

    if cur.get("store"):
        sp = Path(cur["store"])
        if sp.is_symlink() or not sp.is_dir() or not (sp / "messages").is_dir():
            problems.append(f"uninitialized or invalid store: {sp}")

    if problems:
        lines.extend(problems)
        return (3, lines)

    lines.append("status: ok")
    return (0, lines)


def go_back(
    *,
    to_dir: Path,
    wrapper_path: Path,
    on_boundary: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Restore the previous installed pair after validating ownership of both live and backup pairs."""
    to_dir = to_dir.resolve()
    wrapper_path = _normalize_wrapper_path(wrapper_path)
    if not to_dir.is_dir():
        raise InstallError(f"cannot roll back: {to_dir} does not exist", code=2)

    # Peek manifest to discover any secondary wrapper path so all involved wrapper directories are locked in order.
    lock_wrappers = [wrapper_path]
    with contextlib.suppress(InstallError):
        peek = read_manifest(to_dir)
        if peek and isinstance(peek.get("previous"), dict) and isinstance(peek["previous"].get("wrapper"), str):
            lock_wrappers.append(_normalize_wrapper_path(Path(peek["previous"]["wrapper"])))

    prev_handlers = _install_signal_handlers()
    try:
        with acquire_ordered_locks(to_dir, *lock_wrappers):
            recover_if_needed(to_dir, wrapper_path)

            manifest = read_manifest(to_dir)
            if manifest is None:
                raise InstallError(f"cannot roll back: {to_dir / MANIFEST_NAME} is missing", code=2)
            previous = manifest.get("previous")
            if previous is None:
                raise InstallError("cannot roll back: no previous installation is recorded", code=2)

            verify_destination_ownership(to_dir, manifest, require_live=True)

            for name, key in (("helio.old", "helio_sha256"), ("helio.exe.old", "helio_exe_sha256")):
                p = to_dir / name
                if p.is_symlink() or not p.is_file():
                    raise InstallError(f"cannot roll back: backup {p} is missing or not a regular file", code=2)
                actual = sha256_file(p)
                if actual != previous[key]:
                    raise InstallError(
                        f"cannot roll back: backup {p} hash mismatch (expected {previous[key][:12]}, actual {actual[:12]})",
                        code=2,
                    )

            cur = manifest["current"]
            target_wrapper = _normalize_wrapper_path(Path(previous["wrapper"])) if previous.get("wrapper") else wrapper_path
            for name in RESERVED_NAMES:
                if target_wrapper == to_dir / name:
                    raise InstallError(f"wrapper path {target_wrapper} collides with destination file {to_dir / name}", code=2)

            # Refuse if target_wrapper is owned by a different destination (or is a foreign file/symlink).
            if not _wrapper_is_available_for(target_wrapper, to_dir):
                raise InstallError(f"refusing to overwrite wrapper at {target_wrapper} owned by another installation or user", code=2)

            tx_id = secrets.token_hex(6)
            temp_files = _planned_temp_names(tx_id)
            staging_journal = {
                "schema_version": 1,
                "op": "back",
                "phase": "staging",
                "tx_id": tx_id,
                "to_dir": str(to_dir),
                "wrapper_path": str(wrapper_path),
                "temp_files": temp_files,
            }
            write_json_atomic(to_dir / JOURNAL_NAME, staging_journal)

            try:
                saved = _snapshot_files(to_dir, tx_id)
                if on_boundary:
                    on_boundary("after_snapshot")

                stage_helio = to_dir / f".helio.{tx_id}.stage"
                stage_exe = to_dir / f".helio.exe.{tx_id}.stage"
                bak_h = to_dir / f".helio.old.{tx_id}.stage"
                bak_e = to_dir / f".helio.exe.old.{tx_id}.stage"
                copy_to_staged(to_dir / "helio.old", stage_helio, mode=0o755)
                copy_to_staged(to_dir / "helio.exe.old", stage_exe, mode=0o755)

                new_previous = {
                    "commit": cur["commit"],
                    "dirty": bool(cur.get("dirty", False)),
                    "installed_at": cur.get("installed_at"),
                    "helio_sha256": cur["helio_sha256"],
                    "helio_exe_sha256": cur["helio_exe_sha256"],
                    "store": cur.get("store", ""),
                    "wrapper": cur.get("wrapper", str(wrapper_path)),
                }
                restored_store = previous.get("store") or cur.get("store", "")
                new_current = {
                    "commit": previous["commit"],
                    "dirty": bool(previous.get("dirty", False)),
                    "installed_at": previous.get("installed_at") or utc_now(),
                    "helio_sha256": previous["helio_sha256"],
                    "helio_exe_sha256": previous["helio_exe_sha256"],
                    "store": restored_store,
                    "wrapper": str(target_wrapper),
                }
                desired_manifest = {
                    "schema_version": 1,
                    "current": new_current,
                    "previous": new_previous,
                }

                prev_wrapper_existed = wrapper_belongs_to(wrapper_path, to_dir)
                alt_wrapper_existed = wrapper_belongs_to(target_wrapper, to_dir) if target_wrapper != wrapper_path else False
                desired_wrapper_content = render_wrapper(to_dir, Path(restored_store)) if restored_store else None

                journal = {
                    "schema_version": 1,
                    "op": "back",
                    "phase": "publishing",
                    "tx_id": tx_id,
                    "to_dir": str(to_dir),
                    "wrapper_path": str(wrapper_path),
                    "alt_wrapper_path": str(target_wrapper) if target_wrapper != wrapper_path else None,
                    "alt_wrapper_existed": alt_wrapper_existed,
                    "store_dir": restored_store,
                    "had_manifest": True,
                    "previous_manifest": manifest,
                    "previous_wrapper_existed": prev_wrapper_existed,
                    "saved_files": saved,
                    "temp_files": temp_files,
                    "desired_manifest": desired_manifest,
                }
                write_json_atomic(to_dir / JOURNAL_NAME, journal)
                if on_boundary:
                    on_boundary("after_journal")

                copy_to_staged(to_dir / saved["helio"]["rec_name"], bak_h, mode=0o755)
                replace_binary_atomic(bak_h, to_dir / "helio.old")
                if on_boundary:
                    on_boundary("after_backup_helio")

                copy_to_staged(to_dir / saved["helio.exe"]["rec_name"], bak_e, mode=0o755)
                replace_binary_atomic(bak_e, to_dir / "helio.exe.old")
                if on_boundary:
                    on_boundary("after_backup_exe")

                replace_binary_atomic(stage_helio, to_dir / "helio")
                if on_boundary:
                    on_boundary("after_helio")

                replace_binary_atomic(stage_exe, to_dir / "helio.exe")
                if on_boundary:
                    on_boundary("after_helio_exe")

                if desired_wrapper_content is not None:
                    if target_wrapper == wrapper_path:
                        if wrapper_belongs_to(wrapper_path, to_dir):
                            write_bytes_atomic(wrapper_path, desired_wrapper_content.encode("utf-8"), mode=0o755)
                    else:
                        target_wrapper.parent.mkdir(parents=True, exist_ok=True)
                        write_bytes_atomic(target_wrapper, desired_wrapper_content.encode("utf-8"), mode=0o755)
                        if on_boundary:
                            on_boundary("after_target_wrapper")
                        if wrapper_belongs_to(wrapper_path, to_dir):
                            wrapper_path.unlink()
                            fsync_dir(wrapper_path.parent)
                if on_boundary:
                    on_boundary("after_wrapper")

                write_json_atomic(to_dir / MANIFEST_NAME, desired_manifest)
                if on_boundary:
                    on_boundary("after_manifest")

                (to_dir / JOURNAL_NAME).unlink()
                _cleanup_temp_files(to_dir, temp_files, tx_id)
                return desired_manifest
            except BaseException as exc:
                if getattr(exc, "_leave_journal_for_test", False):
                    raise
                with contextlib.suppress(InstallError, OSError):
                    recover_if_needed(to_dir, wrapper_path)
                raise
    finally:
        _restore_signal_handlers(prev_handlers)


def remove(
    *,
    to_dir: Path,
    wrapper_path: Path,
    on_boundary: Callable[[str], None] | None = None,
) -> bool:
    """Remove only installer-owned files in to_dir and bound wrapper(s). Leaves store untouched."""
    to_dir = to_dir.resolve()
    wrapper_path = _normalize_wrapper_path(wrapper_path)

    if not to_dir.exists():
        if wrapper_path.parent.exists():
            with acquire_lock(wrapper_path.parent / WRAPPER_LOCK_NAME, blocking=True):
                if wrapper_belongs_to(wrapper_path, to_dir):
                    wrapper_path.unlink()
                    fsync_dir(wrapper_path.parent)
                    return True
        return False

    # Also lock the manifest's recorded wrapper directory if it differs from wrapper_path.
    lock_wrappers = [wrapper_path]
    with contextlib.suppress(InstallError):
        peek = read_manifest(to_dir)
        if peek and isinstance(peek.get("current"), dict) and isinstance(peek["current"].get("wrapper"), str):
            lock_wrappers.append(_normalize_wrapper_path(Path(peek["current"]["wrapper"])))

    prev_handlers = _install_signal_handlers()
    try:
        with acquire_ordered_locks(to_dir, *lock_wrappers):
            recovered = recover_if_needed(to_dir, wrapper_path)
            manifest = read_manifest(to_dir)
            recorded_wrapper: Path | None = None
            if manifest and isinstance(manifest["current"].get("wrapper"), str):
                rw = _normalize_wrapper_path(Path(manifest["current"]["wrapper"]))
                if rw != wrapper_path:
                    recorded_wrapper = rw

            any_owned = any((to_dir / n).exists() for n in BINARY_NAMES + BACKUP_NAMES + (MANIFEST_NAME,))
            wrapper_owned = wrapper_belongs_to(wrapper_path, to_dir)
            recorded_wrapper_owned = recorded_wrapper is not None and wrapper_belongs_to(recorded_wrapper, to_dir)
            if not any_owned and not wrapper_owned and not recorded_wrapper_owned:
                return recovered

            verify_destination_ownership(to_dir, manifest)

            expected_hashes: dict[str, str] = {}
            for name in BINARY_NAMES + BACKUP_NAMES + (MANIFEST_NAME,):
                p = to_dir / name
                if p.exists():
                    expected_hashes[name] = sha256_file(p)

            journal = {
                "schema_version": 1,
                "op": "remove",
                "phase": "publishing",
                "tx_id": secrets.token_hex(6),
                "to_dir": str(to_dir),
                "wrapper_path": str(wrapper_path),
                "alt_wrapper_path": str(recorded_wrapper) if recorded_wrapper_owned else None,
                "store_dir": "",
                "had_manifest": manifest is not None,
                "previous_manifest": manifest,
                "previous_wrapper_existed": wrapper_owned,
                "saved_files": {},
                "temp_files": [],
                "expected_hashes": expected_hashes,
                "desired_manifest": None,
            }
            write_json_atomic(to_dir / JOURNAL_NAME, journal)
            if on_boundary:
                on_boundary("after_journal")

            try:
                if wrapper_owned:
                    wrapper_path.unlink()
                    fsync_dir(wrapper_path.parent)
                if recorded_wrapper_owned and recorded_wrapper is not None:
                    recorded_wrapper.unlink()
                    fsync_dir(recorded_wrapper.parent)
                if on_boundary:
                    on_boundary("after_wrapper")

                for name in BINARY_NAMES + BACKUP_NAMES:
                    p = to_dir / name
                    if p.exists() and not p.is_symlink():
                        p.unlink()
                        fsync_dir(to_dir)
                    if on_boundary:
                        on_boundary(f"after_unlink_{name.replace('.', '_')}")

                mp = to_dir / MANIFEST_NAME
                if mp.exists() and not mp.is_symlink():
                    mp.unlink()
                    fsync_dir(to_dir)
                if on_boundary:
                    on_boundary("after_manifest")

                (to_dir / JOURNAL_NAME).unlink()
                fsync_dir(to_dir)
                return True
            except BaseException as exc:
                if getattr(exc, "_leave_journal_for_test", False):
                    raise
                with contextlib.suppress(InstallError, OSError):
                    recover_if_needed(to_dir, wrapper_path)
                raise
    finally:
        _restore_signal_handlers(prev_handlers)


def default_paths(repo: Path) -> tuple[Path, Path, Path]:
    home = Path(os.environ.get("HOME") or str(Path.home()))
    return (
        repo.parent / "bin",
        repo.parent / "Heliograph-signals",
        home / ".local" / "bin" / "helio",
    )


def main(argv: list[str] | None = None) -> int:
    repo = Path(os.environ.get("HELIO_REPO") or Path(__file__).resolve().parents[1]).resolve()
    def_to, def_store, def_wrapper = default_paths(repo)

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--to", type=Path, default=def_to, help="program folder (default: <checkout>/../bin)")
    parser.add_argument("--store", type=Path, default=def_store, help="store folder (default: <checkout>/../Heliograph-signals)")
    parser.add_argument("--wrapper", type=Path, default=def_wrapper, help="wrapper path (default: ~/.local/bin/helio)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="verify and print the installed commit and health")
    mode.add_argument("--back", action="store_true", help="restore the previously installed pair")
    mode.add_argument("--remove", action="store_true", help="remove installer-owned files, leaving the store untouched")
    args = parser.parse_args(argv)

    try:
        if args.status:
            code, lines = status(to_dir=args.to, wrapper_path=args.wrapper)
            for line in lines:
                print(line)
            return code
        if args.back:
            manifest = go_back(to_dir=args.to, wrapper_path=args.wrapper)
            cur = manifest["current"]
            label = f"{cur['commit']} (dirty)" if cur.get("dirty") else cur["commit"]
            print(f"restored {label} in {args.to.resolve()}")
            return 0
        if args.remove:
            removed = remove(to_dir=args.to, wrapper_path=args.wrapper)
            print(f"removed Heliograph from {args.to.resolve()}" if removed else f"nothing to remove in {args.to.resolve()}")
            return 0

        sdk_url = os.environ.get("HELIO_SDK_URL", DEFAULT_SDK_URL)
        manifest = install(
            repo=repo,
            to_dir=args.to,
            store_dir=args.store,
            wrapper_path=args.wrapper,
            sdk_url=sdk_url,
        )
        cur = manifest["current"]
        label = f"{cur['commit']} (dirty)" if cur.get("dirty") else cur["commit"]
        print(f"installed {label} to {args.to.resolve()} (wrapper: {_normalize_wrapper_path(args.wrapper)}, store: {args.store.resolve()})")
        wrapper_dir = str(_normalize_wrapper_path(args.wrapper).parent)
        path_entries = [str(Path(p).resolve()) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
        if wrapper_dir not in path_entries:
            print(f"note: add {wrapper_dir} to PATH to run `helio` by name")
        return 0
    except InstallError as exc:
        print(f"install.py: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
