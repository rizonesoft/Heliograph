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
import secrets
import shlex
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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
BINARY_NAMES = ("helio", "helio.exe")
BACKUP_NAMES = ("helio.old", "helio.exe.old")
RESERVED_NAMES = BINARY_NAMES + BACKUP_NAMES + (MANIFEST_NAME, JOURNAL_NAME)


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
    dst.parent.mkdir(parents=True, exist_ok=True)
    data = src.read_bytes()
    with contextlib.suppress(OSError):
        dst.unlink()
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


def _is_regular_non_symlink(path: Path) -> bool:
    return path.exists() and not path.is_symlink() and path.is_file()


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
def acquire_ordered_locks(to_dir: Path, wrapper_path: Path) -> Any:
    """Acquire destination lock first, then wrapper-dir lock (if distinct) to prevent deadlocks."""
    to_lock = to_dir / LOCK_NAME
    wrapper_lock = wrapper_path.parent / WRAPPER_LOCK_NAME
    with acquire_lock(to_lock, blocking=False):
        if wrapper_lock.resolve() == to_lock.resolve():
            yield
        else:
            with acquire_lock(wrapper_lock, blocking=True):
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


def _is_heliograph_elf(path: Path) -> bool:
    try:
        header = path.read_bytes()[:4]
    except OSError:
        return False
    if header != b"\x7fELF":
        return False
    if not os.access(path, os.X_OK):
        return False
    try:
        res = subprocess.run([str(path), "help"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0 and "helio" in res.stdout.lower()


def _is_heliograph_pe(path: Path) -> bool:
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    return raw[:2] == b"MZ" and (b"HELIO_STORE" in raw or b"helio passes messages" in raw)


def verify_destination_ownership(to_dir: Path, manifest: dict[str, Any] | None) -> bool:
    """Verify every reserved path in to_dir belongs to Heliograph.

    Returns True if a verified legacy manual installation (no manifest) is being adopted.
    """
    for name in RESERVED_NAMES:
        _check_not_symlink_or_dir(to_dir / name)

    if manifest is not None:
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


def parse_wrapper_ownership(wrapper_path: Path) -> tuple[str, str | None]:
    """Return (kind, bound_target_dir) for wrapper_path.

    kind is one of: 'absent', 'managed', 'legacy', 'foreign'.
    """
    if not wrapper_path.exists() and not wrapper_path.is_symlink():
        return ("absent", None)
    if wrapper_path.is_symlink() or not wrapper_path.is_file():
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
                target = line[len(TARGET_PREFIX):].strip()
                break
        return ("managed", target)
    if any(line.strip().startswith(LEGACY_MARKER) for line in lines) and "helio" in text:
        # Extract the target directory if exec points to .../helio
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


def render_wrapper(to_dir: Path, store_dir: Path) -> str:
    resolved_to = str(to_dir.resolve())
    helio_bin = str((to_dir / "helio").resolve())
    resolved_store = str(store_dir.resolve())
    return (
        "#!/bin/sh\n"
        f"{MANAGED_MARKER}\n"
        f"{TARGET_PREFIX}{resolved_to}\n"
        'if [ -z "${HELIO_STORE:-}" ]; then\n'
        f"  HELIO_STORE={shlex.quote(resolved_store)}\n"
        "  export HELIO_STORE\n"
        "fi\n"
        f"exec {shlex.quote(helio_bin)} \"$@\"\n"
    )


def _cleanup_temp_files(to_dir: Path) -> None:
    if not to_dir.is_dir():
        return
    for entry in to_dir.iterdir():
        name = entry.name
        if name.startswith((".stage.", ".rec.", ".installed.json.tmp.", ".install.journal.json.tmp.")):
            with contextlib.suppress(OSError):
                entry.unlink()
    fsync_dir(to_dir)


def _validate_journal_paths(journal: dict[str, Any], to_dir: Path, wrapper_path: Path) -> None:
    if journal.get("schema_version") != 1:
        raise InstallError("unrecognized install journal schema", code=2)
    if journal.get("op") not in ("install", "back", "remove"):
        raise InstallError("invalid install journal operation", code=2)
    if Path(str(journal.get("to_dir", ""))).resolve() != to_dir.resolve():
        raise InstallError("install journal target directory mismatch", code=2)
    j_wrapper = journal.get("wrapper_path")
    if j_wrapper is not None and Path(str(j_wrapper)).resolve() != wrapper_path.resolve():
        raise InstallError("install journal wrapper path mismatch", code=2)
    saved = journal.get("saved_files", {})
    if not isinstance(saved, dict):
        raise InstallError("install journal saved_files is invalid", code=2)
    resolved_to = to_dir.resolve()
    for name, meta in saved.items():
        if name not in BINARY_NAMES + BACKUP_NAMES:
            raise InstallError(f"install journal names unowned file: {name}", code=2)
        if not isinstance(meta, dict):
            raise InstallError(f"install journal entry invalid for {name}", code=2)
        rec_name = meta.get("rec_name")
        if rec_name is not None:
            rec_path = (to_dir / str(rec_name)).resolve()
            if rec_path.parent != resolved_to or not str(rec_name).startswith(".rec."):
                raise InstallError(f"install journal recovery path escapes destination: {rec_name}", code=2)


def recover_if_needed(to_dir: Path, wrapper_path: Path) -> bool:
    """Recover from a leftover install.journal.json. Returns True if recovery ran."""
    journal_path = to_dir / JOURNAL_NAME
    if not journal_path.exists() and not journal_path.is_symlink():
        return False
    _check_not_symlink_or_dir(journal_path)
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallError(f"corrupt or foreign journal at {journal_path}: {exc}", code=2) from None
    _validate_journal_paths(journal, to_dir, wrapper_path)

    op = journal["op"]
    if op == "remove":
        # Forward-complete removal of owned files.
        for name in BINARY_NAMES + BACKUP_NAMES + (MANIFEST_NAME,):
            p = to_dir / name
            if p.exists() and not p.is_symlink():
                p.unlink()
        if wrapper_belongs_to(wrapper_path, to_dir):
            with contextlib.suppress(OSError):
                wrapper_path.unlink()
            fsync_dir(wrapper_path.parent)
        _cleanup_temp_files(to_dir)
        journal_path.unlink()
        fsync_dir(to_dir)
        return True

    # For 'install' and 'back': check if commit point (installed.json == desired_manifest) was reached.
    desired_manifest = journal.get("desired_manifest")
    current_manifest = None
    if (to_dir / MANIFEST_NAME).exists():
        with contextlib.suppress(InstallError):
            current_manifest = read_manifest(to_dir)

    if desired_manifest is not None and current_manifest == desired_manifest:
        # Commit point reached: finish cleanup and remove journal.
        _cleanup_temp_files(to_dir)
        journal_path.unlink()
        fsync_dir(to_dir)
        return True

    # Commit point not reached: roll back every file in the set from .rec.* snapshots.
    saved: dict[str, dict[str, Any]] = journal.get("saved_files", {})
    for name in BINARY_NAMES + BACKUP_NAMES:
        target = to_dir / name
        meta = saved.get(name)
        if meta and meta.get("existed"):
            rec_path = to_dir / meta["rec_name"]
            expected_hash = meta["sha256"]
            if target.exists() and not target.is_symlink() and sha256_file(target) == expected_hash:
                pass
            elif rec_path.exists() and sha256_file(rec_path) == expected_hash:
                staged_restore = to_dir / f".stage.restore.{name}.{os.getpid()}"
                copy_to_staged(rec_path, staged_restore, mode=0o755)
                replace_binary_atomic(staged_restore, target)
            else:
                raise InstallError(f"recovery failed: cannot restore {target} with expected hash", code=3)
        else:
            if target.exists() and not target.is_symlink():
                target.unlink()
    fsync_dir(to_dir)

    # Restore wrapper state.
    prev_wrapper_existed = bool(journal.get("previous_wrapper_existed"))
    prev_wrapper_content = journal.get("previous_wrapper_content")
    if prev_wrapper_existed and isinstance(prev_wrapper_content, str):
        kind, _ = parse_wrapper_ownership(wrapper_path)
        if kind in ("absent", "managed", "legacy"):
            write_bytes_atomic(wrapper_path, prev_wrapper_content.encode("utf-8"), mode=0o755)
    else:
        if wrapper_belongs_to(wrapper_path, to_dir):
            with contextlib.suppress(OSError):
                wrapper_path.unlink()
            fsync_dir(wrapper_path.parent)

    # Restore manifest state.
    if journal.get("had_manifest") and isinstance(journal.get("previous_manifest"), dict):
        write_json_atomic(to_dir / MANIFEST_NAME, journal["previous_manifest"])
    else:
        mp = to_dir / MANIFEST_NAME
        if mp.exists() and not mp.is_symlink():
            mp.unlink()
            fsync_dir(to_dir)

    _cleanup_temp_files(to_dir)
    journal_path.unlink()
    fsync_dir(to_dir)
    return True


def _snapshot_files(to_dir: Path) -> dict[str, dict[str, Any]]:
    saved: dict[str, dict[str, Any]] = {}
    for name in BINARY_NAMES + BACKUP_NAMES:
        p = to_dir / name
        if p.exists() and not p.is_symlink():
            rec_name = f".rec.{name}"
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


def ensure_sdk_and_build(repo: Path, sdk_url: str) -> tuple[Path, Path]:
    sdk_dir = repo.parent / "ImpossibleSDK"
    if not sdk_dir.exists():
        clone = subprocess.run(["git", "clone", sdk_url, str(sdk_dir)], check=False)
        if clone.returncode != 0:
            raise InstallError(f"failed to clone Impossible SDK from {sdk_url} into {sdk_dir}", code=3)
    app_build = sdk_dir / "tools" / "app_build.py"
    if not app_build.is_file():
        raise InstallError(f"Impossible SDK at {sdk_dir} is missing tools/app_build.py", code=3)
    build = subprocess.run([sys.executable, str(app_build), str(repo)], check=False)
    if build.returncode != 0:
        raise InstallError(f"build/test failed (exit {build.returncode}); nothing was installed", code=build.returncode)

    helio_bin = repo / "build" / "linux" / "linux" / "helio"
    helio_exe = repo / "build" / "linux" / "helio.exe"
    for p in (helio_bin, helio_exe):
        if not p.is_file():
            raise InstallError(f"expected built program is missing: {p}", code=3)
    return helio_bin, helio_exe


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
    to_dir = to_dir.resolve()
    store_dir = store_dir.resolve()
    wrapper_path = wrapper_path.resolve()

    commit, dirty = git_provenance(repo)
    if prebuilt is None:
        helio_src, helio_exe_src = ensure_sdk_and_build(repo, sdk_url)
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
            adopting_legacy = verify_destination_ownership(to_dir, existing_manifest)

            wrap_kind, _ = parse_wrapper_ownership(wrapper_path)
            if wrap_kind == "foreign":
                raise InstallError(f"refusing unowned wrapper at {wrapper_path}", code=2)

            prev_wrapper_existed = wrap_kind in ("managed", "legacy")
            prev_wrapper_content = wrapper_path.read_text(encoding="utf-8") if prev_wrapper_existed else None

            saved = _snapshot_files(to_dir)
            stage_helio = to_dir / ".stage.helio"
            stage_exe = to_dir / ".stage.helio.exe"
            copy_to_staged(helio_src, stage_helio, mode=0o755)
            copy_to_staged(helio_exe_src, stage_exe, mode=0o755)

            previous_entry: dict[str, Any] | None = None
            if existing_manifest is not None:
                prev_cur = existing_manifest["current"]
                previous_entry = {
                    "commit": prev_cur["commit"],
                    "dirty": bool(prev_cur.get("dirty", False)),
                    "installed_at": prev_cur.get("installed_at"),
                    "helio_sha256": saved["helio"]["sha256"] or prev_cur["helio_sha256"],
                    "helio_exe_sha256": saved["helio.exe"]["sha256"] or prev_cur["helio_exe_sha256"],
                }
            elif adopting_legacy:
                previous_entry = {
                    "commit": "legacy",
                    "dirty": False,
                    "installed_at": None,
                    "helio_sha256": saved["helio"]["sha256"],
                    "helio_exe_sha256": saved["helio.exe"]["sha256"],
                }

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
                "to_dir": str(to_dir),
                "wrapper_path": str(wrapper_path),
                "store_dir": str(store_dir),
                "had_manifest": existing_manifest is not None,
                "previous_manifest": existing_manifest,
                "previous_wrapper_existed": prev_wrapper_existed,
                "previous_wrapper_content": prev_wrapper_content,
                "saved_files": saved,
                "desired_manifest": desired_manifest,
                "desired_wrapper_content": desired_wrapper,
            }
            write_json_atomic(to_dir / JOURNAL_NAME, journal)
            if on_boundary:
                on_boundary("after_journal")

            try:
                # 1. Rotate backups from saved live files (if any).
                if saved["helio"]["existed"]:
                    bak_h = to_dir / ".stage.helio.old"
                    copy_to_staged(to_dir / saved["helio"]["rec_name"], bak_h, mode=0o755)
                    replace_binary_atomic(bak_h, to_dir / "helio.old")
                if saved["helio.exe"]["existed"]:
                    bak_e = to_dir / ".stage.helio.exe.old"
                    copy_to_staged(to_dir / saved["helio.exe"]["rec_name"], bak_e, mode=0o755)
                    replace_binary_atomic(bak_e, to_dir / "helio.exe.old")
                if on_boundary:
                    on_boundary("after_backups")

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

                # 4. Initialize store if it does not exist yet (never touch an existing store).
                if not store_dir.exists():
                    store_dir.parent.mkdir(parents=True, exist_ok=True)
                    init_res = subprocess.run(
                        [str(to_dir / "helio"), "init", "--store", str(store_dir)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if init_res.returncode != 0:
                        raise InstallError(
                            f"failed to initialize store at {store_dir}: {init_res.stderr.strip() or init_res.stdout.strip()}",
                            code=3,
                        )

                # 5. Commit manifest (atomic commit point).
                write_json_atomic(to_dir / MANIFEST_NAME, desired_manifest)
                if on_boundary:
                    on_boundary("after_manifest")

                # 6. Remove journal and temporary staging/recovery files.
                (to_dir / JOURNAL_NAME).unlink()
                _cleanup_temp_files(to_dir)
                return desired_manifest
            except BaseException as exc:
                # Roll back cleanly on any failure or interruption unless the test boundary specifically raised
                # to leave the journal on disk for retry testing.
                if getattr(exc, "_leave_journal_for_test", False):
                    raise
                with contextlib.suppress(InstallError, OSError):
                    recover_if_needed(to_dir, wrapper_path)
                raise
    finally:
        _restore_signal_handlers(prev_handlers)


def status(*, to_dir: Path, wrapper_path: Path) -> tuple[int, list[str]]:
    """Inspect installation health by independently hashing live binaries."""
    to_dir = to_dir.resolve()
    wrapper_path = wrapper_path.resolve()
    lines: list[str] = []

    if not to_dir.exists():
        return (1, [f"not installed: {to_dir} does not exist"])

    journal_path = to_dir / JOURNAL_NAME
    if journal_path.exists() or journal_path.is_symlink():
        return (3, [f"interrupted operation pending recovery: {journal_path}"])

    manifest_path = to_dir / MANIFEST_NAME
    if not manifest_path.exists() and not manifest_path.is_symlink():
        present = [n for n in BINARY_NAMES if (to_dir / n).exists() or (to_dir / n).is_symlink()]
        if not present:
            return (1, [f"not installed in {to_dir}"])
        return (3, [f"partial or unmanaged installation in {to_dir}: {', '.join(present)} present without {MANIFEST_NAME}"])

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
            actual = sha256_file(p)
            if actual != cur[key]:
                problems.append(f"modified binary: {p} (recorded {cur[key][:12]}, actual {actual[:12]})")

    if not wrapper_path.exists() and not wrapper_path.is_symlink():
        problems.append(f"missing wrapper: {wrapper_path}")
    elif not wrapper_belongs_to(wrapper_path, to_dir):
        kind, bound = parse_wrapper_ownership(wrapper_path)
        problems.append(f"wrapper {wrapper_path} does not point to {to_dir} (state={kind}, target={bound})")

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
    """Restore the previous installed pair after validating both backup hashes."""
    to_dir = to_dir.resolve()
    wrapper_path = wrapper_path.resolve()
    if not to_dir.is_dir():
        raise InstallError(f"cannot roll back: {to_dir} does not exist", code=2)

    prev_handlers = _install_signal_handlers()
    try:
        with acquire_ordered_locks(to_dir, wrapper_path):
            recover_if_needed(to_dir, wrapper_path)

            manifest = read_manifest(to_dir)
            if manifest is None:
                raise InstallError(f"cannot roll back: {to_dir / MANIFEST_NAME} is missing", code=2)
            previous = manifest.get("previous")
            if previous is None:
                raise InstallError("cannot roll back: no previous installation is recorded", code=2)

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

            saved = _snapshot_files(to_dir)
            stage_helio = to_dir / ".stage.helio"
            stage_exe = to_dir / ".stage.helio.exe"
            copy_to_staged(to_dir / "helio.old", stage_helio, mode=0o755)
            copy_to_staged(to_dir / "helio.exe.old", stage_exe, mode=0o755)

            cur = manifest["current"]
            new_previous = {
                "commit": cur["commit"],
                "dirty": bool(cur.get("dirty", False)),
                "installed_at": cur.get("installed_at"),
                "helio_sha256": saved["helio"]["sha256"] or cur["helio_sha256"],
                "helio_exe_sha256": saved["helio.exe"]["sha256"] or cur["helio_exe_sha256"],
            }
            new_current = {
                "commit": previous["commit"],
                "dirty": bool(previous.get("dirty", False)),
                "installed_at": previous.get("installed_at") or utc_now(),
                "helio_sha256": previous["helio_sha256"],
                "helio_exe_sha256": previous["helio_exe_sha256"],
                "store": cur.get("store", ""),
                "wrapper": str(wrapper_path),
            }
            desired_manifest = {
                "schema_version": 1,
                "current": new_current,
                "previous": new_previous,
            }

            wrap_kind, _ = parse_wrapper_ownership(wrapper_path)
            prev_wrapper_existed = wrap_kind in ("managed", "legacy")
            prev_wrapper_content = wrapper_path.read_text(encoding="utf-8") if prev_wrapper_existed else None

            journal = {
                "schema_version": 1,
                "op": "back",
                "to_dir": str(to_dir),
                "wrapper_path": str(wrapper_path),
                "store_dir": cur.get("store", ""),
                "had_manifest": True,
                "previous_manifest": manifest,
                "previous_wrapper_existed": prev_wrapper_existed,
                "previous_wrapper_content": prev_wrapper_content,
                "saved_files": saved,
                "desired_manifest": desired_manifest,
                "desired_wrapper_content": prev_wrapper_content,
            }
            write_json_atomic(to_dir / JOURNAL_NAME, journal)
            if on_boundary:
                on_boundary("after_journal")

            try:
                if saved["helio"]["existed"]:
                    bak_h = to_dir / ".stage.helio.old"
                    copy_to_staged(to_dir / saved["helio"]["rec_name"], bak_h, mode=0o755)
                    replace_binary_atomic(bak_h, to_dir / "helio.old")
                if saved["helio.exe"]["existed"]:
                    bak_e = to_dir / ".stage.helio.exe.old"
                    copy_to_staged(to_dir / saved["helio.exe"]["rec_name"], bak_e, mode=0o755)
                    replace_binary_atomic(bak_e, to_dir / "helio.exe.old")
                if on_boundary:
                    on_boundary("after_backups")

                replace_binary_atomic(stage_helio, to_dir / "helio")
                if on_boundary:
                    on_boundary("after_helio")

                replace_binary_atomic(stage_exe, to_dir / "helio.exe")
                if on_boundary:
                    on_boundary("after_helio_exe")

                write_json_atomic(to_dir / MANIFEST_NAME, desired_manifest)
                if on_boundary:
                    on_boundary("after_manifest")

                (to_dir / JOURNAL_NAME).unlink()
                _cleanup_temp_files(to_dir)
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
    """Remove only installer-owned files in to_dir and the bound wrapper. Leaves store untouched."""
    to_dir = to_dir.resolve()
    wrapper_path = wrapper_path.resolve()

    if not to_dir.exists():
        # Still check if a wrapper points to this now-absent destination.
        if wrapper_path.parent.exists():
            with acquire_lock(wrapper_path.parent / WRAPPER_LOCK_NAME, blocking=True):
                if wrapper_belongs_to(wrapper_path, to_dir):
                    wrapper_path.unlink()
                    fsync_dir(wrapper_path.parent)
                    return True
        return False

    prev_handlers = _install_signal_handlers()
    try:
        with acquire_ordered_locks(to_dir, wrapper_path):
            recovered = recover_if_needed(to_dir, wrapper_path)
            manifest = read_manifest(to_dir)
            # If nothing owned exists, removal is idempotent.
            any_owned = any((to_dir / n).exists() for n in BINARY_NAMES + BACKUP_NAMES + (MANIFEST_NAME,))
            wrapper_owned = wrapper_belongs_to(wrapper_path, to_dir)
            if not any_owned and not wrapper_owned:
                return recovered

            if manifest is None:
                verify_destination_ownership(to_dir, None)

            journal = {
                "schema_version": 1,
                "op": "remove",
                "to_dir": str(to_dir),
                "wrapper_path": str(wrapper_path),
                "store_dir": "",
                "had_manifest": manifest is not None,
                "previous_manifest": manifest,
                "previous_wrapper_existed": wrapper_owned,
                "previous_wrapper_content": None,
                "saved_files": {},
                "desired_manifest": None,
                "desired_wrapper_content": None,
            }
            write_json_atomic(to_dir / JOURNAL_NAME, journal)
            if on_boundary:
                on_boundary("after_journal")

            try:
                if wrapper_owned:
                    wrapper_path.unlink()
                    fsync_dir(wrapper_path.parent)
                if on_boundary:
                    on_boundary("after_wrapper")

                for name in BINARY_NAMES + BACKUP_NAMES:
                    p = to_dir / name
                    if p.exists() and not p.is_symlink():
                        p.unlink()
                fsync_dir(to_dir)
                if on_boundary:
                    on_boundary("after_binaries")

                mp = to_dir / MANIFEST_NAME
                if mp.exists() and not mp.is_symlink():
                    mp.unlink()
                    fsync_dir(to_dir)

                (to_dir / JOURNAL_NAME).unlink()
                _cleanup_temp_files(to_dir)
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
        print(f"installed {label} to {args.to.resolve()} (wrapper: {args.wrapper.resolve()}, store: {args.store.resolve()})")
        wrapper_dir = str(args.wrapper.resolve().parent)
        path_entries = [str(Path(p).resolve()) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
        if wrapper_dir not in path_entries:
            print(f"note: add {wrapper_dir} to PATH to run `helio` by name")
        return 0
    except InstallError as exc:
        print(f"install.py: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
