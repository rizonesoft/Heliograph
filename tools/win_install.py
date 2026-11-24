"""Windows-native half of Heliograph's installer (D01 T01 §16).

Runs under Windows Python (directly or invoked from WSL via python.exe).
Installs:
  - <win_to>\\bin\\helio.exe       (native Windows binary, kept off PATH)
  - <win_to>\\bin\\helio.exe.old   (previous generation backup for --back)
  - <win_to>\\helio.cmd            (command shim on PATH with store precedence & WSL check)
  - <win_to>\\installed.win.json   (provenance, hashes, Path ownership, retired files)
  - HKCU\\<reg_key>\\Path          (adds <win_to> once, preserving kind & % vars)
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

msvcrt: Any = None
winreg: Any = None
if os.name == "nt":
    import msvcrt as _msvcrt
    import winreg as _winreg

    msvcrt = _msvcrt
    winreg = _winreg

WIN_CMD_NAME = "helio.cmd"
WIN_BIN_SUBDIR = "bin"
WIN_EXE_NAME = "helio.exe"
WIN_OLD_EXE_NAME = "helio.exe.old"
WIN_MANIFEST_NAME = "installed.win.json"
WIN_JOURNAL_NAME = "install.win.journal.json"
WIN_CMD_MARKER = "REM Managed by Heliograph installer"
DEFAULT_REG_KEY = "Environment"
HELIO_MARKERS = (b"HELIO_STORE", b"helio passes messages")
TX_ID_RE = re.compile(r"^[0-9a-f]{12}$")
WIN_RETIRED_RE = re.compile(r"^\.helio\.exe\.[0-9a-f]{12}\.retired$")
WIN_RESERVED_ROOT = (WIN_CMD_NAME, WIN_EXE_NAME, WIN_MANIFEST_NAME, WIN_JOURNAL_NAME)
WIN_RESERVED_BIN = (WIN_EXE_NAME, WIN_OLD_EXE_NAME)
WIN_ALLOWED_SAVED_KEYS = {WIN_CMD_NAME, f"{WIN_BIN_SUBDIR}/{WIN_EXE_NAME}", f"{WIN_BIN_SUBDIR}/{WIN_OLD_EXE_NAME}"}
WIN_ALLOWED_EXPECTED_KEYS = WIN_ALLOWED_SAVED_KEYS | {WIN_MANIFEST_NAME}
BATCH_FORBIDDEN = set('\r\n\x00"%!&|<>^()')


class WinInstallError(Exception):
    """Refusal or operational failure with an explicit exit code."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


class WinBusyExecutableError(WinInstallError):
    """Raised when Windows refuses both replacement and rename-away of a busy executable."""

    def __init__(self, target: Path) -> None:
        super().__init__(
            f"{target} is locked by another process and cannot be replaced or renamed away",
            code=3,
        )
        self.target = target


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _is_heliograph_pe(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        data = path.read_bytes()
    except OSError:
        return False
    return data.startswith(b"MZ") and any(marker in data for marker in HELIO_MARKERS)


def _is_reparse_or_symlink(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        st = os.lstat(path)
    except OSError:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & 0x0400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _check_regular_or_absent(path: Path) -> None:
    if _is_reparse_or_symlink(path):
        raise WinInstallError(f"refusing symlink or reparse point at {path}", code=2)
    if path.exists() and not path.is_file():
        raise WinInstallError(f"refusing non-file at reserved path {path}", code=2)


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f".{path.name}.{secrets.token_hex(6)}.tmp"
    tmp_path = path.parent / tmp_name
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _write_json_atomic(path: Path, obj: dict[str, Any]) -> None:
    _write_bytes_atomic(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _copy_to_staged(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with src.open("rb") as rfh, os.fdopen(fd, "wb") as wfh:
            shutil.copyfileobj(rfh, wfh, length=65536)
            wfh.flush()
            os.fsync(wfh.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            dst.unlink()
        raise


def _extract_wsl_distro(store_path: str) -> str | None:
    norm = store_path.replace("/", "\\")
    lower = norm.lower()
    for prefix in ("\\\\wsl.localhost\\", "\\\\wsl$\\"):
        if lower.startswith(prefix):
            rest = norm[len(prefix) :]
            distro = rest.split("\\", 1)[0].strip()
            if distro:
                return distro
    return None


def render_win_cmd(default_store: str) -> str:
    """Render helio.cmd (CRLF) forwarding %* directly (no double expansion) with WSL diagnostic."""
    if not default_store or any(ch in BATCH_FORBIDDEN for ch in default_store):
        raise WinInstallError(f"refusing unsafe store path for helio.cmd: {default_store!r}", code=2)
    distro = _extract_wsl_distro(default_store)
    if distro:
        err_msg = f"helio: cannot reach WSL store '{default_store}' - check that WSL and distro '{distro}' are running"
    else:
        err_msg = f"helio: cannot reach store '{default_store}' - check that the store location is available"
    lines = [
        "@echo off",
        WIN_CMD_MARKER,
        "setlocal EnableExtensions",
        'set "_HELIO_USED_DEFAULT="',
        "if not defined HELIO_STORE (",
        f'  set "HELIO_STORE={default_store}"',
        '  set "_HELIO_USED_DEFAULT=1"',
        ")",
        '"%~dp0bin\\helio.exe" %*',
        'set "_HELIO_RC=%ERRORLEVEL%"',
        'if not "%_HELIO_RC%"=="0" if defined _HELIO_USED_DEFAULT if not exist "' + default_store + '\\." (',
        f"  >&2 echo {err_msg}",
        "  exit /b 3",
        ")",
        "exit /b %_HELIO_RC%",
    ]
    return "\r\n".join(lines) + "\r\n"


def default_win_to_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "Heliograph"
    return Path.home() / "AppData" / "Local" / "Heliograph"


def _locks_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "Heliograph-locks"


def _dest_lock_path(win_to: Path) -> Path:
    digest = hashlib.sha256(str(win_to.resolve()).lower().encode("utf-8")).hexdigest()[:16]
    return _locks_root() / f".dest-{digest}.lock"


def _reg_lock_path(reg_key: str) -> Path:
    digest = hashlib.sha256(reg_key.strip().lower().encode("utf-8")).hexdigest()[:16]
    return _locks_root() / f".reg-{digest}.lock"


@contextlib.contextmanager
def acquire_win_lock(lock_path: Path, *, blocking: bool, timeout: float = 30.0):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse_or_symlink(lock_path):
        raise WinInstallError(f"refusing symlink lock path: {lock_path}", code=2)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    locked = False
    try:
        if os.name == "nt" and msvcrt is not None:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError as exc:
                    if not blocking:
                        raise WinInstallError(f"another Windows installer is already running (lock: {lock_path})", code=3) from exc
                    if time.monotonic() >= deadline:
                        raise WinInstallError(f"timed out waiting for Windows installer lock {lock_path}", code=3) from exc
                    time.sleep(0.05)
        yield
    finally:
        if locked and os.name == "nt" and msvcrt is not None:
            with contextlib.suppress(OSError):
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


def _norm_path_entry(entry: str, *, expand_env: bool = True) -> str:
    cleaned = entry.strip().strip('"')
    if expand_env and "%" in cleaned:
        cleaned = os.path.expandvars(cleaned)
    return cleaned.rstrip("\\/").lower()


def broadcast_environment_change() -> bool:
    """Notify running Windows programs that HKCU\\Environment changed."""
    if os.name != "nt":
        return False
    hwnd_broadcast = 0xFFFF
    wm_settingchange = 0x001A
    smto_abortifhung = 0x0002
    result = ctypes.c_size_t(0)
    rc = ctypes.windll.user32.SendMessageTimeoutW(
        hwnd_broadcast,
        wm_settingchange,
        0,
        "Environment",
        smto_abortifhung,
        5000,
        ctypes.byref(result),
    )
    return bool(rc)


def read_reg_path(reg_key: str) -> tuple[bool, str, int]:
    """Return (existed, raw_value, value_type) for Path under HKCU\\<reg_key>."""
    if os.name != "nt" or winreg is None:
        raise WinInstallError("Windows registry operations require Windows Python", code=3)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_key, 0, winreg.KEY_READ) as key:
            val, val_type = winreg.QueryValueEx(key, "Path")
            if not isinstance(val, str):
                raise WinInstallError(f"unexpected non-string Path value in HKCU\\{reg_key}", code=2)
            return True, val, int(val_type)
    except FileNotFoundError:
        return False, "", int(winreg.REG_EXPAND_SZ)


def ensure_reg_path_entry(reg_key: str, folder: Path) -> tuple[bool, bool, int]:
    """Ensure folder is on HKCU\\<reg_key>\\Path once, preserving raw delimiters, % vars and kind.

    Returns (added_now, existed_before, original_kind).
    """
    if os.name != "nt" or winreg is None:
        raise WinInstallError("Windows registry operations require Windows Python", code=3)
    folder_str = str(folder.resolve())
    target_norm = _norm_path_entry(folder_str, expand_env=False)
    existed, raw_val, val_type = read_reg_path(reg_key)
    expand_existing = val_type == int(winreg.REG_EXPAND_SZ)
    segments = raw_val.split(";") if raw_val else []
    if any(seg.strip() and _norm_path_entry(seg, expand_env=expand_existing) == target_norm for seg in segments):
        return False, existed, val_type
    if not raw_val:
        new_val = folder_str
    elif raw_val.endswith(";"):
        new_val = raw_val + folder_str
    else:
        new_val = raw_val + ";" + folder_str
    write_kind = val_type if existed else int(winreg.REG_EXPAND_SZ)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, reg_key, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Path", 0, write_kind, new_val)
    if not broadcast_environment_change():
        raise WinInstallError("WM_SETTINGCHANGE environment broadcast failed", code=3)
    return True, existed, val_type


def remove_reg_path_entry(reg_key: str, folder: Path, *, path_had_value: bool) -> bool:
    """Surgically remove ONE matching folder entry from HKCU\\<reg_key>\\Path, preserving raw delimiters and kind."""
    if os.name != "nt" or winreg is None:
        raise WinInstallError("Windows registry operations require Windows Python", code=3)
    folder_str = str(folder.resolve())
    target_norm = _norm_path_entry(folder_str, expand_env=False)
    existed, raw_val, val_type = read_reg_path(reg_key)
    if not existed:
        return False
    expand_existing = val_type == int(winreg.REG_EXPAND_SZ)
    segments = raw_val.split(";")
    removed_idx: int | None = None
    for idx, seg in enumerate(segments):
        if seg.strip() and _norm_path_entry(seg, expand_env=expand_existing) == target_norm:
            removed_idx = idx
            break
    if removed_idx is None:
        return False
    remaining = segments[:removed_idx] + segments[removed_idx + 1 :]
    new_val = ";".join(remaining)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_key, 0, winreg.KEY_SET_VALUE) as key:
        if not new_val and not path_had_value:
            winreg.DeleteValue(key, "Path")
        else:
            winreg.SetValueEx(key, "Path", 0, val_type, new_val)
    if not broadcast_environment_change():
        raise WinInstallError("WM_SETTINGCHANGE environment broadcast failed", code=3)
    return True


def read_win_manifest(win_to: Path) -> dict[str, Any] | None:
    mp = win_to / WIN_MANIFEST_NAME
    if _is_reparse_or_symlink(mp):
        raise WinInstallError(f"refusing symlink manifest: {mp}", code=2)
    if not mp.exists():
        return None
    if not mp.is_file():
        raise WinInstallError(f"refusing non-file manifest: {mp}", code=2)
    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WinInstallError(f"corrupt Windows manifest {mp}: {exc}", code=2) from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("current"), dict):
        raise WinInstallError(f"unrecognized Windows manifest {mp}", code=2)
    return data


def _verify_win_ownership(win_to: Path, manifest: dict[str, Any] | None, *, require_live: bool = False) -> None:
    for name in WIN_RESERVED_ROOT:
        _check_regular_or_absent(win_to / name)
    root_exe = win_to / WIN_EXE_NAME
    if root_exe.exists() or _is_reparse_or_symlink(root_exe):
        raise WinInstallError(f"refusing top-level {root_exe} which would shadow {WIN_CMD_NAME} on PATH", code=2)
    bin_dir = win_to / WIN_BIN_SUBDIR
    if _is_reparse_or_symlink(bin_dir):
        raise WinInstallError(f"refusing symlink bin directory: {bin_dir}", code=2)
    if bin_dir.exists() and not bin_dir.is_dir():
        raise WinInstallError(f"refusing non-directory bin path: {bin_dir}", code=2)
    for name in WIN_RESERVED_BIN:
        _check_regular_or_absent(bin_dir / name)

    if manifest is None:
        for p in (win_to / WIN_CMD_NAME, bin_dir / WIN_EXE_NAME, bin_dir / WIN_OLD_EXE_NAME):
            if p.exists():
                raise WinInstallError(f"refusing unowned file at {p} (no {WIN_MANIFEST_NAME})", code=2)
        return

    cur = manifest["current"]
    exe_p = bin_dir / WIN_EXE_NAME
    if not exe_p.exists():
        if require_live:
            raise WinInstallError(f"missing live executable {exe_p}", code=2)
    else:
        if sha256_file(exe_p) != cur.get("helio_exe_sha256"):
            raise WinInstallError(f"refusing modified or unowned executable at {exe_p}", code=2)

    cmd_p = win_to / WIN_CMD_NAME
    if not cmd_p.exists():
        if require_live:
            raise WinInstallError(f"missing live command wrapper {cmd_p}", code=2)
    else:
        if sha256_file(cmd_p) != cur.get("helio_cmd_sha256"):
            raise WinInstallError(f"refusing modified or unowned wrapper at {cmd_p}", code=2)

    prev = manifest.get("previous")
    old_p = bin_dir / WIN_OLD_EXE_NAME
    if old_p.exists():
        if not isinstance(prev, dict):
            raise WinInstallError(f"refusing unexpected backup {old_p} with no previous generation recorded", code=2)
        if sha256_file(old_p) != prev.get("helio_exe_sha256"):
            raise WinInstallError(f"refusing modified or unowned backup at {old_p}", code=2)


def _sweep_retired_exes(bin_dir: Path, retired_names: list[str]) -> list[str]:
    remaining: list[str] = []
    if not bin_dir.is_dir():
        return remaining
    for name in retired_names:
        if not isinstance(name, str) or not WIN_RETIRED_RE.fullmatch(name):
            continue
        p = bin_dir / name
        if not p.exists() or _is_reparse_or_symlink(p):
            continue
        try:
            p.unlink()
        except OSError as exc:
            if getattr(exc, "winerror", None) in (5, 32) or isinstance(exc, PermissionError):
                remaining.append(name)
            else:
                raise
    return remaining


def _replace_win_exe(staged: Path, target: Path, tx_id: str) -> str | None:
    """Replace target with staged. If target is busy on Windows, rename it aside to a tracked .retired file."""
    try:
        os.replace(staged, target)
        return None
    except OSError as exc:
        if getattr(exc, "winerror", None) not in (5, 32) and not isinstance(exc, PermissionError):
            raise
        retired = target.parent / f".{target.name}.{tx_id}.retired"
        try:
            os.replace(target, retired)
        except OSError as ren_exc:
            if getattr(ren_exc, "winerror", None) in (5, 32) or isinstance(ren_exc, PermissionError):
                raise WinBusyExecutableError(target) from ren_exc
            raise
        try:
            os.replace(staged, target)
        except OSError as pub_exc:
            with contextlib.suppress(OSError):
                os.replace(retired, target)
            if getattr(pub_exc, "winerror", None) in (5, 32) or isinstance(pub_exc, PermissionError):
                raise WinBusyExecutableError(target) from pub_exc
            raise
        return retired.name


def _is_valid_win_temp(rel: str, tx_id: str) -> bool:
    if not isinstance(rel, str) or ".." in rel:
        return False
    allowed = {
        f".helio.cmd.{tx_id}.rec",
        f"bin/.helio.exe.{tx_id}.rec",
        f"bin/.helio.exe.{tx_id}.stage",
        f"bin/.helio.exe.{tx_id}.restore",
        f"bin/.helio.exe.old.{tx_id}.rec",
        f"bin/.helio.exe.old.{tx_id}.stage",
        f"bin/.helio.exe.old.{tx_id}.restore",
    }
    return rel in allowed


def _cleanup_win_temps(win_to: Path, temp_files: list[str], tx_id: str) -> None:
    for rel in temp_files:
        if not _is_valid_win_temp(rel, tx_id):
            continue
        p = win_to / rel
        if _is_reparse_or_symlink(p):
            continue
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def _validate_win_journal(journal: Any, win_to: Path, reg_key: str) -> dict[str, Any]:
    if not isinstance(journal, dict):
        raise WinInstallError("corrupt Windows install journal: not an object", code=2)
    if journal.get("schema_version") != 2:
        raise WinInstallError("unrecognized Windows install journal schema", code=2)
    if journal.get("op") not in ("install", "back", "remove"):
        raise WinInstallError("invalid Windows install journal op", code=2)
    if journal.get("phase") not in ("staging", "publishing"):
        raise WinInstallError("invalid Windows install journal phase", code=2)
    tx_id = journal.get("tx_id")
    if not isinstance(tx_id, str) or not TX_ID_RE.fullmatch(tx_id):
        raise WinInstallError("invalid Windows install journal tx_id", code=2)
    if Path(str(journal.get("win_to", ""))).resolve() != win_to.resolve():
        raise WinInstallError("Windows install journal target mismatch", code=2)
    if str(journal.get("reg_key", "")).lower() != reg_key.lower():
        raise WinInstallError("Windows install journal registry key mismatch", code=2)
    temp_files = journal.get("temp_files", [])
    if not isinstance(temp_files, list) or any(not _is_valid_win_temp(t, tx_id) for t in temp_files):
        raise WinInstallError("Windows install journal temp_files contains invalid entries", code=2)
    if journal.get("phase") == "staging":
        return journal
    expected = journal.get("expected_hashes", {})
    if not isinstance(expected, dict) or any(k not in WIN_ALLOWED_EXPECTED_KEYS for k in expected):
        raise WinInstallError("Windows install journal expected_hashes contains disallowed paths", code=2)
    saved = journal.get("saved_files", {})
    if not isinstance(saved, dict) or any(k not in WIN_ALLOWED_SAVED_KEYS for k in saved):
        raise WinInstallError("Windows install journal saved_files contains disallowed paths", code=2)
    for meta in saved.values():
        if not isinstance(meta, dict):
            raise WinInstallError("Windows install journal saved_files entry is invalid", code=2)
        rec_name = meta.get("rec_name")
        if rec_name is not None and not _is_valid_win_temp(rec_name, tx_id):
            raise WinInstallError(f"Windows install journal rec_name is invalid: {rec_name}", code=2)
    retired = journal.get("retired", [])
    if not isinstance(retired, list) or any(not isinstance(r, str) or not WIN_RETIRED_RE.fullmatch(r) for r in retired):
        raise WinInstallError("Windows install journal retired list contains invalid entries", code=2)
    return journal


def _desired_win_hashes(desired: dict[str, Any] | None) -> dict[str, str | None]:
    if not isinstance(desired, dict):
        return {}
    cur = desired.get("current") or {}
    prev = desired.get("previous") or {}
    return {
        WIN_CMD_NAME: cur.get("helio_cmd_sha256"),
        f"{WIN_BIN_SUBDIR}/{WIN_EXE_NAME}": cur.get("helio_exe_sha256"),
        f"{WIN_BIN_SUBDIR}/{WIN_OLD_EXE_NAME}": prev.get("helio_exe_sha256"),
    }


def recover_win_if_needed(win_to: Path, reg_key: str) -> bool:
    """Recover an interrupted Windows install, rollback, or removal journal."""
    jp = win_to / WIN_JOURNAL_NAME
    if _is_reparse_or_symlink(jp):
        raise WinInstallError(f"refusing symlink journal: {jp}", code=2)
    if not jp.exists():
        return False
    try:
        raw = json.loads(jp.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WinInstallError(f"corrupt Windows install journal {jp}: {exc}", code=2) from exc
    journal = _validate_win_journal(raw, win_to, reg_key)
    tx_id: str = journal["tx_id"]
    op: str = journal["op"]
    phase: str = journal["phase"]
    temp_files: list[str] = journal.get("temp_files", [])

    if phase == "staging":
        _cleanup_win_temps(win_to, temp_files, tx_id)
        jp.unlink()
        return True

    if op == "remove":
        expected: dict[str, str] = journal.get("expected_hashes", {})
        for rel, exp_hash in expected.items():
            p = win_to / rel
            if p.exists():
                _check_regular_or_absent(p)
                if sha256_file(p) != exp_hash:
                    raise WinInstallError(f"remove recovery refusing modified file {p}", code=2)
        if journal.get("path_added"):
            remove_reg_path_entry(reg_key, win_to, path_had_value=bool(journal.get("path_had_value", True)))
        for rel in expected:
            p = win_to / rel
            if p.exists():
                p.unlink()
        still_busy = _sweep_retired_exes(win_to / WIN_BIN_SUBDIR, journal.get("retired", []))
        if still_busy:
            journal["retired"] = still_busy
            _write_json_atomic(jp, journal)
            raise WinInstallError(
                f"remove recovery paused: retired executable(s) still in use: {', '.join(still_busy)}",
                code=3,
            )
        jp.unlink()
        _prune_empty_win_dirs(win_to)
        return True

    desired = journal.get("desired_manifest")
    prev_manifest = journal.get("previous_manifest")
    current_manifest = read_win_manifest(win_to)

    if desired is not None and current_manifest == desired:
        if desired.get("path_added"):
            ensure_reg_path_entry(reg_key, win_to)
        _cleanup_win_temps(win_to, temp_files, tx_id)
        jp.unlink()
        return True

    if journal.get("had_manifest") and current_manifest is None:
        raise WinInstallError(f"recovery refusing journal claiming had_manifest=True when {WIN_MANIFEST_NAME} is absent", code=2)
    if current_manifest is not None and current_manifest != prev_manifest and current_manifest != desired:
        raise WinInstallError(f"recovery refusing externally modified {WIN_MANIFEST_NAME}", code=2)

    saved: dict[str, dict[str, Any]] = journal.get("saved_files", {})
    desired_hashes = _desired_win_hashes(desired)

    # Verify live files before rolling back so externally created/edited files are never overwritten or deleted.
    for rel, meta in saved.items():
        target = win_to / rel
        if not target.exists() and not _is_reparse_or_symlink(target):
            continue
        _check_regular_or_absent(target)
        actual = sha256_file(target)
        allowed = {h for h in (meta.get("sha256"), desired_hashes.get(rel)) if h}
        if actual not in allowed:
            raise WinInstallError(f"recovery refusing externally created or modified file at {target}", code=2)

    extra_retired: list[str] = list(journal.get("retired", []))
    for rel, meta in saved.items():
        target = win_to / rel
        if meta.get("existed"):
            rec_path = win_to / meta["rec_name"]
            exp_sha = meta["sha256"]
            if target.exists() and sha256_file(target) == exp_sha:
                continue
            if not rec_path.is_file() or sha256_file(rec_path) != exp_sha:
                raise WinInstallError(f"recovery failed: cannot restore {target} from snapshot", code=3)
            restore_tmp = target.parent / f".{target.name}.{tx_id}.restore"
            if restore_tmp.exists():
                restore_tmp.unlink()
            _copy_to_staged(rec_path, restore_tmp)
            ret = _replace_win_exe(restore_tmp, target, tx_id)
            if ret and ret not in extra_retired:
                extra_retired.append(ret)
        else:
            if target.exists():
                target.unlink()

    if journal.get("path_added_this_tx"):
        remove_reg_path_entry(reg_key, win_to, path_had_value=bool(journal.get("path_had_value", True)))

    if journal.get("had_manifest") and isinstance(prev_manifest, dict):
        restored_manifest = dict(prev_manifest)
        merged_retired = list(restored_manifest.get("retired", []))
        for r in extra_retired:
            if r not in merged_retired:
                merged_retired.append(r)
        restored_manifest["retired"] = _sweep_retired_exes(win_to / WIN_BIN_SUBDIR, merged_retired)
        _write_json_atomic(win_to / WIN_MANIFEST_NAME, restored_manifest)
    else:
        mp = win_to / WIN_MANIFEST_NAME
        if mp.exists():
            mp.unlink()

    _cleanup_win_temps(win_to, temp_files, tx_id)
    jp.unlink()
    return True


def _prune_empty_win_dirs(win_to: Path) -> None:
    bin_dir = win_to / WIN_BIN_SUBDIR
    if bin_dir.is_dir():
        with contextlib.suppress(OSError):
            if not any(bin_dir.iterdir()):
                bin_dir.rmdir()
    if win_to.is_dir():
        with contextlib.suppress(OSError):
            if not any(win_to.iterdir()):
                win_to.rmdir()


def _handle_win_failure(exc: BaseException, win_to: Path, reg_key: str) -> None:
    if getattr(exc, "_leave_journal_for_test", False):
        raise exc
    try:
        recover_win_if_needed(win_to, reg_key)
    except (WinInstallError, OSError) as rec_exc:
        raise WinInstallError(
            f"Windows operation failed ({exc}) and recovery could not finish ({rec_exc}); "
            f"pending recovery recorded in {win_to / WIN_JOURNAL_NAME}",
            code=3,
        ) from exc
    if isinstance(exc, OSError):
        raise WinInstallError(f"Windows filesystem error ({exc.strerror or exc}): {exc.filename or win_to}", code=3) from exc
    raise exc


def win_install(
    *,
    source_exe: Path,
    source_sha256: str | None,
    store_path: str,
    commit: str,
    dirty: bool = False,
    win_to: Path | None = None,
    reg_key: str = DEFAULT_REG_KEY,
    on_boundary: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Install helio.exe into <win_to>\\bin, helio.cmd into <win_to>, and add <win_to> to HKCU\\<reg_key>\\Path."""
    if not source_exe.is_file():
        raise WinInstallError(f"source executable is missing: {source_exe}", code=3)

    cmd_text = render_win_cmd(store_path)
    cmd_bytes = cmd_text.encode("utf-8")
    cmd_sha = hashlib.sha256(cmd_bytes).hexdigest()

    dest = (win_to or default_win_to_dir()).resolve()
    bin_dir = dest / WIN_BIN_SUBDIR
    dest.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)

    with acquire_win_lock(_dest_lock_path(dest), blocking=False):
        with acquire_win_lock(_reg_lock_path(reg_key), blocking=True):
            recover_win_if_needed(dest, reg_key)
            existing = read_win_manifest(dest)
            if existing is not None and str(existing.get("reg_key", reg_key)).lower() != reg_key.lower():
                raise WinInstallError(
                    f"destination {dest} is managed under registry key HKCU\\{existing['reg_key']}, not HKCU\\{reg_key}",
                    code=2,
                )
            _verify_win_ownership(dest, existing, require_live=existing is not None)

            prev_retired = list(existing.get("retired", [])) if existing else []
            remaining_retired = _sweep_retired_exes(bin_dir, prev_retired)

            tx_id = secrets.token_hex(6)
            temp_files = [
                f".helio.cmd.{tx_id}.rec",
                f"bin/.helio.exe.{tx_id}.rec",
                f"bin/.helio.exe.{tx_id}.stage",
                f"bin/.helio.exe.{tx_id}.restore",
                f"bin/.helio.exe.old.{tx_id}.rec",
                f"bin/.helio.exe.old.{tx_id}.stage",
                f"bin/.helio.exe.old.{tx_id}.restore",
            ]
            staging_journal = {
                "schema_version": 2,
                "op": "install",
                "phase": "staging",
                "tx_id": tx_id,
                "win_to": str(dest),
                "reg_key": reg_key,
                "temp_files": temp_files,
            }
            _write_json_atomic(dest / WIN_JOURNAL_NAME, staging_journal)
            if on_boundary:
                on_boundary("after_staging_journal")

            try:
                # Copy source_exe into our locked staging file FIRST, then verify PE identity & SHA-256 on the staged bytes!
                stage_exe = bin_dir / f".helio.exe.{tx_id}.stage"
                _copy_to_staged(source_exe, stage_exe)
                if not _is_heliograph_pe(stage_exe):
                    raise WinInstallError(f"staged executable is not a Heliograph PE binary: {source_exe}", code=3)
                actual_sha = sha256_file(stage_exe)
                if source_sha256 is not None and actual_sha != source_sha256:
                    raise WinInstallError(
                        f"staged executable hash mismatch for {source_exe}: expected {source_sha256[:12]}, actual {actual_sha[:12]}",
                        code=3,
                    )

                saved: dict[str, dict[str, Any]] = {}
                for rel, rec_rel in (
                    (WIN_CMD_NAME, f".helio.cmd.{tx_id}.rec"),
                    (f"{WIN_BIN_SUBDIR}/{WIN_EXE_NAME}", f"bin/.helio.exe.{tx_id}.rec"),
                    (f"{WIN_BIN_SUBDIR}/{WIN_OLD_EXE_NAME}", f"bin/.helio.exe.old.{tx_id}.rec"),
                ):
                    p = dest / rel
                    if p.exists():
                        _copy_to_staged(p, dest / rec_rel)
                        saved[rel] = {"existed": True, "rec_name": rec_rel, "sha256": sha256_file(dest / rec_rel)}
                    else:
                        saved[rel] = {"existed": False, "rec_name": None, "sha256": None}

                previous_entry: dict[str, Any] | None = None
                rotate_old = False
                if existing is not None:
                    prev_cur = existing["current"]
                    same_install = (
                        prev_cur.get("helio_exe_sha256") == actual_sha
                        and prev_cur.get("store") == store_path
                        and prev_cur.get("commit") == commit
                        and bool(prev_cur.get("dirty", False)) == dirty
                    )
                    if same_install:
                        # Identical reinstall: preserve the previous distinct rollback generation!
                        previous_entry = existing.get("previous")
                        rotate_old = False
                    else:
                        previous_entry = {
                            "commit": prev_cur["commit"],
                            "dirty": bool(prev_cur.get("dirty", False)),
                            "installed_at": prev_cur.get("installed_at"),
                            "helio_exe_sha256": prev_cur["helio_exe_sha256"],
                            "helio_cmd_sha256": prev_cur["helio_cmd_sha256"],
                            "store": prev_cur["store"],
                        }
                        rotate_old = True

                path_existed_before, raw_path_before, path_kind_before = read_reg_path(reg_key)
                target_norm = _norm_path_entry(str(dest), expand_env=False)
                expand_existing = os.name == "nt" and winreg is not None and path_kind_before == int(winreg.REG_EXPAND_SZ)
                already_on_path = any(
                    seg.strip() and _norm_path_entry(seg, expand_env=expand_existing) == target_norm
                    for seg in (raw_path_before.split(";") if raw_path_before else [])
                )
                prior_path_added = bool(existing.get("path_added", False)) if existing else False
                prior_path_had_value = bool(existing.get("path_had_value", path_existed_before)) if existing else path_existed_before
                path_added_this_tx = not already_on_path
                final_path_added = prior_path_added or path_added_this_tx

                desired_manifest: dict[str, Any] = {
                    "schema_version": 1,
                    "current": {
                        "commit": commit,
                        "dirty": dirty,
                        "installed_at": _utc_now(),
                        "helio_exe_sha256": actual_sha,
                        "helio_cmd_sha256": cmd_sha,
                        "store": store_path,
                    },
                    "previous": previous_entry,
                    "path_added": final_path_added,
                    "path_had_value": prior_path_had_value,
                    "path_original_kind": path_kind_before,
                    "reg_key": reg_key,
                    "retired": list(remaining_retired),
                }

                journal = {
                    "schema_version": 2,
                    "op": "install",
                    "phase": "publishing",
                    "tx_id": tx_id,
                    "win_to": str(dest),
                    "reg_key": reg_key,
                    "had_manifest": existing is not None,
                    "previous_manifest": existing,
                    "path_added_this_tx": path_added_this_tx,
                    "path_had_value": prior_path_had_value,
                    "saved_files": saved,
                    "temp_files": temp_files,
                    "retired": list(remaining_retired),
                    "desired_manifest": desired_manifest,
                }
                _write_json_atomic(dest / WIN_JOURNAL_NAME, journal)
                if on_boundary:
                    on_boundary("after_journal")

                if rotate_old:
                    stage_old = bin_dir / f".helio.exe.old.{tx_id}.stage"
                    _copy_to_staged(dest / saved[f"{WIN_BIN_SUBDIR}/{WIN_EXE_NAME}"]["rec_name"], stage_old)
                    _replace_win_exe(stage_old, bin_dir / WIN_OLD_EXE_NAME, tx_id)
                    if on_boundary:
                        on_boundary("after_backup")

                retired_name = _replace_win_exe(stage_exe, bin_dir / WIN_EXE_NAME, tx_id)
                if retired_name:
                    desired_manifest["retired"].append(retired_name)
                    journal["retired"].append(retired_name)
                    journal["desired_manifest"] = desired_manifest
                    _write_json_atomic(dest / WIN_JOURNAL_NAME, journal)
                if on_boundary:
                    on_boundary("after_exe")

                _write_bytes_atomic(dest / WIN_CMD_NAME, cmd_bytes)
                if on_boundary:
                    on_boundary("after_cmd")

                if path_added_this_tx:
                    ensure_reg_path_entry(reg_key, dest)
                if on_boundary:
                    on_boundary("after_path")

                _write_json_atomic(dest / WIN_MANIFEST_NAME, desired_manifest)
                if on_boundary:
                    on_boundary("after_manifest")

                _cleanup_win_temps(dest, temp_files, tx_id)
                (dest / WIN_JOURNAL_NAME).unlink()
                return desired_manifest
            except BaseException as exc:
                _handle_win_failure(exc, dest, reg_key)
                raise


def win_status(*, win_to: Path | None = None, reg_key: str = DEFAULT_REG_KEY) -> tuple[int, list[str]]:
    """Verify Windows installation: live/backup hashes, helio.cmd bytes, Path entry, and initialized store."""
    dest = (win_to or default_win_to_dir()).resolve()
    bin_dir = dest / WIN_BIN_SUBDIR
    lines: list[str] = []

    if not dest.exists():
        return (1, [f"not installed: {dest} does not exist"])
    if (dest / WIN_JOURNAL_NAME).exists():
        return (3, [f"interrupted Windows operation pending recovery: {dest / WIN_JOURNAL_NAME}"])
    if not (dest / WIN_MANIFEST_NAME).exists():
        present = [
            str(p)
            for p in (dest / WIN_CMD_NAME, dest / WIN_EXE_NAME, bin_dir / WIN_EXE_NAME, bin_dir / WIN_OLD_EXE_NAME)
            if p.exists() or _is_reparse_or_symlink(p)
        ]
        if not present:
            return (1, [f"not installed in {dest}"])
        return (3, [f"partial or unmanaged Windows installation in {dest}: {', '.join(present)}"])

    try:
        manifest = read_win_manifest(dest)
    except WinInstallError as exc:
        return (3, [str(exc)])
    assert manifest is not None

    cur = manifest["current"]
    label = f"{cur['commit']} (dirty)" if cur.get("dirty") else cur["commit"]
    lines.append(f"commit: {label}")
    if cur.get("installed_at"):
        lines.append(f"installed: {cur['installed_at']}")
    lines.append(f"store: {cur['store']}")

    problems: list[str] = []
    if (dest / WIN_EXE_NAME).exists() or _is_reparse_or_symlink(dest / WIN_EXE_NAME):
        problems.append(f"unexpected top-level executable shadowing {WIN_CMD_NAME}: {dest / WIN_EXE_NAME}")

    exe_p = bin_dir / WIN_EXE_NAME
    if _is_reparse_or_symlink(exe_p) or not exe_p.is_file():
        problems.append(f"missing or invalid executable: {exe_p}")
    elif sha256_file(exe_p) != cur.get("helio_exe_sha256"):
        problems.append(f"modified executable: {exe_p}")

    cmd_p = dest / WIN_CMD_NAME
    if _is_reparse_or_symlink(cmd_p) or not cmd_p.is_file():
        problems.append(f"missing or invalid wrapper: {cmd_p}")
    else:
        expected_cmd = render_win_cmd(cur["store"]).encode("utf-8")
        if cmd_p.read_bytes() != expected_cmd:
            problems.append(f"wrapper content mismatch: {cmd_p}")

    prev = manifest.get("previous")
    old_p = bin_dir / WIN_OLD_EXE_NAME
    if isinstance(prev, dict):
        if _is_reparse_or_symlink(old_p) or not old_p.is_file():
            problems.append(f"missing or invalid backup: {old_p}")
        elif sha256_file(old_p) != prev.get("helio_exe_sha256"):
            problems.append(f"modified backup: {old_p}")
    elif old_p.exists() or _is_reparse_or_symlink(old_p):
        problems.append(f"unexpected backup with no previous generation: {old_p}")

    recorded_reg_key = str(manifest.get("reg_key") or reg_key)
    if recorded_reg_key.lower() != reg_key.lower():
        problems.append(f"registry key mismatch: installed under HKCU\\{recorded_reg_key}, checked HKCU\\{reg_key}")
    elif os.name == "nt":
        _, raw_path, val_type = read_reg_path(reg_key)
        expand_existing = winreg is not None and val_type == int(winreg.REG_EXPAND_SZ)
        target_norm = _norm_path_entry(str(dest), expand_env=False)
        if not any(seg.strip() and _norm_path_entry(seg, expand_env=expand_existing) == target_norm for seg in (raw_path.split(";") if raw_path else [])):
            problems.append(f"missing from HKCU\\{reg_key}\\Path: {dest}")

    store_p = Path(cur["store"])
    if not store_p.is_dir() or not (store_p / "messages").is_dir():
        problems.append(f"uninitialized or unreachable store: {cur['store']}")

    if problems:
        lines.extend(problems)
        return (3, lines)
    lines.append("status: ok")
    return (0, lines)


def win_back(
    *,
    win_to: Path | None = None,
    reg_key: str = DEFAULT_REG_KEY,
    on_boundary: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Restore the previous Windows generation (helio.exe.old and previous store)."""
    dest = (win_to or default_win_to_dir()).resolve()
    bin_dir = dest / WIN_BIN_SUBDIR
    if not dest.is_dir():
        raise WinInstallError(f"cannot roll back: {dest} does not exist", code=2)

    with acquire_win_lock(_dest_lock_path(dest), blocking=False):
        with acquire_win_lock(_reg_lock_path(reg_key), blocking=True):
            recover_win_if_needed(dest, reg_key)
            manifest = read_win_manifest(dest)
            if manifest is None:
                raise WinInstallError(f"cannot roll back: {dest / WIN_MANIFEST_NAME} is missing", code=2)
            recorded_reg = str(manifest.get("reg_key") or reg_key)
            if recorded_reg.lower() != reg_key.lower():
                raise WinInstallError(
                    f"cannot roll back: destination {dest} is managed under HKCU\\{recorded_reg}, not HKCU\\{reg_key}",
                    code=2,
                )
            prev = manifest.get("previous")
            if not isinstance(prev, dict):
                raise WinInstallError("cannot roll back: no previous Windows installation is recorded", code=2)

            _verify_win_ownership(dest, manifest, require_live=True)
            old_p = bin_dir / WIN_OLD_EXE_NAME
            if not old_p.is_file():
                raise WinInstallError(f"cannot roll back: backup {old_p} is missing", code=2)

            cmd_bytes = render_win_cmd(prev["store"]).encode("utf-8")
            cmd_sha = hashlib.sha256(cmd_bytes).hexdigest()

            tx_id = secrets.token_hex(6)
            temp_files = [
                f".helio.cmd.{tx_id}.rec",
                f"bin/.helio.exe.{tx_id}.rec",
                f"bin/.helio.exe.{tx_id}.stage",
                f"bin/.helio.exe.{tx_id}.restore",
                f"bin/.helio.exe.old.{tx_id}.rec",
                f"bin/.helio.exe.old.{tx_id}.stage",
                f"bin/.helio.exe.old.{tx_id}.restore",
            ]
            _write_json_atomic(
                dest / WIN_JOURNAL_NAME,
                {
                    "schema_version": 2,
                    "op": "back",
                    "phase": "staging",
                    "tx_id": tx_id,
                    "win_to": str(dest),
                    "reg_key": reg_key,
                    "temp_files": temp_files,
                },
            )
            try:
                saved: dict[str, dict[str, Any]] = {}
                for rel, rec_rel in (
                    (WIN_CMD_NAME, f".helio.cmd.{tx_id}.rec"),
                    (f"{WIN_BIN_SUBDIR}/{WIN_EXE_NAME}", f"bin/.helio.exe.{tx_id}.rec"),
                    (f"{WIN_BIN_SUBDIR}/{WIN_OLD_EXE_NAME}", f"bin/.helio.exe.old.{tx_id}.rec"),
                ):
                    p = dest / rel
                    _copy_to_staged(p, dest / rec_rel)
                    saved[rel] = {"existed": True, "rec_name": rec_rel, "sha256": sha256_file(dest / rec_rel)}

                cur = manifest["current"]
                swept_retired = _sweep_retired_exes(bin_dir, list(manifest.get("retired", [])))
                desired_manifest = dict(manifest)
                desired_manifest["current"] = {
                    "commit": prev["commit"],
                    "dirty": bool(prev.get("dirty", False)),
                    "installed_at": _utc_now(),
                    "helio_exe_sha256": prev["helio_exe_sha256"],
                    "helio_cmd_sha256": cmd_sha,
                    "store": prev["store"],
                }
                desired_manifest["previous"] = {
                    "commit": cur["commit"],
                    "dirty": bool(cur.get("dirty", False)),
                    "installed_at": cur.get("installed_at"),
                    "helio_exe_sha256": cur["helio_exe_sha256"],
                    "helio_cmd_sha256": cur["helio_cmd_sha256"],
                    "store": cur["store"],
                }
                desired_manifest["retired"] = list(swept_retired)

                journal = {
                    "schema_version": 2,
                    "op": "back",
                    "phase": "publishing",
                    "tx_id": tx_id,
                    "win_to": str(dest),
                    "reg_key": reg_key,
                    "had_manifest": True,
                    "previous_manifest": manifest,
                    "path_added_this_tx": False,
                    "path_had_value": bool(manifest.get("path_had_value", True)),
                    "saved_files": saved,
                    "temp_files": temp_files,
                    "retired": list(swept_retired),
                    "desired_manifest": desired_manifest,
                }
                _write_json_atomic(dest / WIN_JOURNAL_NAME, journal)
                if on_boundary:
                    on_boundary("after_journal")

                stage_exe = bin_dir / f".helio.exe.{tx_id}.stage"
                stage_old = bin_dir / f".helio.exe.old.{tx_id}.stage"
                _copy_to_staged(dest / saved[f"{WIN_BIN_SUBDIR}/{WIN_OLD_EXE_NAME}"]["rec_name"], stage_exe)
                _copy_to_staged(dest / saved[f"{WIN_BIN_SUBDIR}/{WIN_EXE_NAME}"]["rec_name"], stage_old)

                _replace_win_exe(stage_old, bin_dir / WIN_OLD_EXE_NAME, tx_id)
                retired_name = _replace_win_exe(stage_exe, bin_dir / WIN_EXE_NAME, tx_id)
                if retired_name:
                    desired_manifest["retired"].append(retired_name)
                    journal["retired"].append(retired_name)
                    journal["desired_manifest"] = desired_manifest
                    _write_json_atomic(dest / WIN_JOURNAL_NAME, journal)
                if on_boundary:
                    on_boundary("after_exe")

                _write_bytes_atomic(dest / WIN_CMD_NAME, cmd_bytes)
                if on_boundary:
                    on_boundary("after_cmd")

                _write_json_atomic(dest / WIN_MANIFEST_NAME, desired_manifest)
                if on_boundary:
                    on_boundary("after_manifest")

                _cleanup_win_temps(dest, temp_files, tx_id)
                (dest / WIN_JOURNAL_NAME).unlink()
                return desired_manifest
            except BaseException as exc:
                _handle_win_failure(exc, dest, reg_key)
                raise


def win_remove(
    *,
    win_to: Path | None = None,
    reg_key: str = DEFAULT_REG_KEY,
    on_boundary: Callable[[str], None] | None = None,
) -> bool:
    """Remove installer-owned files and undo only the owned Path entry; leave unrelated files intact."""
    dest = (win_to or default_win_to_dir()).resolve()
    bin_dir = dest / WIN_BIN_SUBDIR
    if not dest.exists():
        return False

    removed_any = False
    with acquire_win_lock(_dest_lock_path(dest), blocking=False):
        with acquire_win_lock(_reg_lock_path(reg_key), blocking=True):
            recovered = recover_win_if_needed(dest, reg_key)
            manifest = read_win_manifest(dest)
            owned_rels = [
                WIN_CMD_NAME,
                f"{WIN_BIN_SUBDIR}/{WIN_EXE_NAME}",
                f"{WIN_BIN_SUBDIR}/{WIN_OLD_EXE_NAME}",
                WIN_MANIFEST_NAME,
            ]
            if manifest is None:
                if any((dest / r).exists() for r in owned_rels):
                    _verify_win_ownership(dest, None)
                return recovered

            recorded_reg = str(manifest.get("reg_key") or reg_key)
            if recorded_reg.lower() != reg_key.lower():
                raise WinInstallError(
                    f"refusing remove: destination {dest} is managed under HKCU\\{recorded_reg}, not HKCU\\{reg_key}",
                    code=2,
                )

            _verify_win_ownership(dest, manifest)
            expected: dict[str, str] = {}
            for rel in owned_rels:
                p = dest / rel
                if p.exists():
                    expected[rel] = sha256_file(p)

            retired_list = list(manifest.get("retired", []))
            path_added = bool(manifest.get("path_added", False))
            path_had_value = bool(manifest.get("path_had_value", True))

            journal = {
                "schema_version": 2,
                "op": "remove",
                "phase": "publishing",
                "tx_id": secrets.token_hex(6),
                "win_to": str(dest),
                "reg_key": reg_key,
                "path_added": path_added,
                "path_had_value": path_had_value,
                "expected_hashes": expected,
                "retired": retired_list,
                "temp_files": [],
            }
            _write_json_atomic(dest / WIN_JOURNAL_NAME, journal)
            if on_boundary:
                on_boundary("after_journal")

            try:
                if path_added:
                    remove_reg_path_entry(reg_key, dest, path_had_value=path_had_value)
                if on_boundary:
                    on_boundary("after_path")

                for rel in owned_rels:
                    p = dest / rel
                    if p.exists():
                        p.unlink()
                        removed_any = True
                    if on_boundary:
                        on_boundary(f"after_unlink_{rel.replace('/', '_').replace('.', '_')}")

                still_busy = _sweep_retired_exes(bin_dir, retired_list)
                if still_busy:
                    journal["retired"] = still_busy
                    _write_json_atomic(dest / WIN_JOURNAL_NAME, journal)
                    raise WinInstallError(
                        f"removed active files, but retired executable(s) are still in use: {', '.join(still_busy)}; "
                        "rerun --windows --remove after they exit",
                        code=3,
                    )
                (dest / WIN_JOURNAL_NAME).unlink()
            except BaseException as exc:
                _handle_win_failure(exc, dest, reg_key)
                raise

    _prune_empty_win_dirs(dest)
    return removed_any or recovered


def wsl_to_windows_path(path: Path) -> str:
    res = subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True, check=False, timeout=30)
    if res.returncode != 0 or not res.stdout.strip():
        raise WinInstallError(f"wslpath -w failed for {path}: {res.stderr.strip()}", code=3)
    return res.stdout.strip()
