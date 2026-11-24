#!/usr/bin/env python3
"""Phase-close replacement for Heliograph (`tools/install.py --phase <n>`).

Replaces both the WSL installation and the Windows installation with the exact
binaries proved for a closed, fully verified phase on `origin/main`. Guards
cross-host replacement with a durable phase journal, skips already-current
healthy hosts on retry to preserve distinct rollback generations, verifies both
live entry points in clean environments plus an isolated cross-OS roundtrip,
rolls back only changed hosts on any fault, and sends one idempotent
`helio tell impossible/all` notice keyed by commit.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

PHASE_LOCK_NAME = ".phase.lock"
PHASE_JOURNAL_NAME = "phase.journal.json"
PHASE_TELL_AS = "impossible/apps"
PHASE_TELL_TO = "impossible/all"

RACE_TEST_NAME = "test_windows_and_linux_take_each_job_once"


def find_closed_phases_missing_install(repo: Path, install_mod: Any | None = None) -> list[int]:
    """Return sorted phase numbers that are all-verified and closed in committed HEAD docs/phase-runs/ but lack a pushed Installed commit receipt on origin/main after their latest Closeout."""
    def _fail(msg: str) -> None:
        if install_mod is not None:
            raise install_mod.InstallError(msg, code=3)
        raise RuntimeError(msg)

    res = subprocess.run(
        [sys.executable, "-B", str(repo / "scripts" / "todo-graph.py"), "query", "plan"],
        cwd=str(repo),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        _fail(f"cannot query implementation plan in {repo} (exit {res.returncode}): {(res.stderr or res.stdout).strip()}")
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        _fail(f"invalid JSON from todo-graph.py query plan in {repo}: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("phases"), dict):
        _fail(f"malformed plan payload from todo-graph.py query plan in {repo}")

    # Defer querying `origin` until at least one phase is locally all-verified and closed (not parked) so an
    # offline session starting with open work is never blocked, while any closed phase still fails exit 3 if
    # `origin/main` publication cannot be verified.
    rem_sha_cache: str | None = None

    def _resolve_origin_main_sha() -> str:
        nonlocal rem_sha_cache
        if rem_sha_cache is not None:
            return rem_sha_cache
        rem_url = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", "remote.origin.url"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if rem_url.returncode != 0 or not rem_url.stdout.strip():
            _fail(f"cannot verify origin/main publication in {repo}: remote 'origin' is not configured")
        ls_rem = subprocess.run(
            ["git", "-C", str(repo), "ls-remote", "origin", "refs/heads/main"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if ls_rem.returncode != 0:
            _fail(f"cannot query origin refs/heads/main in {repo}: {(ls_rem.stderr or ls_rem.stdout).strip()}")
        rem_lines = [ln.strip() for ln in ls_rem.stdout.splitlines() if ln.strip()]
        if not rem_lines:
            _fail(f"cannot verify origin/main publication in {repo}: origin has no refs/heads/main")
        sha = rem_lines[0].split()[0]

        # Ensure sha is locally readable for `git ls-tree` / `git show` (handles remote advances without touching HEAD).
        has_obj = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if has_obj.returncode != 0:
            fetch_res = subprocess.run(
                ["git", "-C", str(repo), "fetch", "--no-tags", "--quiet", "origin", "refs/heads/main"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            if fetch_res.returncode != 0:
                _fail(f"cannot fetch origin refs/heads/main in {repo}: {(fetch_res.stderr or fetch_res.stdout).strip()}")
        rem_sha_cache = sha
        return sha

    def _scan_run_files(commit_ref: str, pnum: int) -> tuple[bool, bool, str | None, list[str], int, str, list[str]]:
        ls_r = subprocess.run(
            ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", commit_ref, "docs/phase-runs"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if ls_r.returncode != 0:
            _fail(f"cannot read docs/phase-runs from {commit_ref} in {repo}: {(ls_r.stderr or ls_r.stdout).strip()}")
        suffix = f"-phase-{pnum}.md"
        phase_files = sorted(p.strip() for p in ls_r.stdout.splitlines() if p.strip().endswith(suffix))
        has_closeout = False
        has_open_park = False
        installed_sha: str | None = None
        closeout_count = 0
        latest_co_file = ""
        latest_co_lines: list[str] = []
        collecting_co = False
        for rp in phase_files:
            show_r = subprocess.run(
                ["git", "-C", str(repo), "show", f"{commit_ref}:{rp}"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            if show_r.returncode != 0:
                _fail(f"cannot read {rp} from {commit_ref}: {(show_r.stderr or show_r.stdout).strip()}")
            for line in show_r.stdout.splitlines():
                if line.startswith("## Closeout"):
                    has_closeout = True
                    has_open_park = False
                    installed_sha = None
                    closeout_count += 1
                    latest_co_file = rp
                    latest_co_lines = [line.rstrip()]
                    collecting_co = True
                elif line.startswith("PARKED "):
                    has_open_park = True
                    collecting_co = False
                elif has_closeout and not has_open_park:
                    m = re.search(r"Installed commit:\s*`?([0-9a-f]{40})`?", line)
                    if m:
                        installed_sha = m.group(1)
                        # Stop collecting closeout body once the receipt is written so required post-receipt
                        # CI notes or follow-up entries appended after the receipt never reopen the phase.
                        collecting_co = False
                    elif collecting_co and rp == latest_co_file:
                        if line.startswith("## "):
                            collecting_co = False
                        else:
                            latest_co_lines.append(line.rstrip())
        while latest_co_lines and latest_co_lines[-1] == "":
            latest_co_lines.pop()
        return has_closeout, has_open_park, installed_sha, phase_files, closeout_count, latest_co_file, latest_co_lines

    pending: list[int] = []
    declared_phases = data["phases"]
    sections = data.get("sections") if isinstance(data.get("sections"), list) else []
    for pkey in declared_phases:
        try:
            pnum = int(pkey)
        except (TypeError, ValueError):
            continue
        p_sections = [s for s in sections if isinstance(s, dict) and s.get("phase") == pnum]
        if not p_sections or not all(s.get("status") == "verified" for s in p_sections):
            continue
        head_closed, head_parked, head_installed_sha, head_files, head_co_cnt, head_co_file, head_co_lines = _scan_run_files("HEAD", pnum)
        if not head_closed or head_parked:
            continue
        rem_sha = _resolve_origin_main_sha()
        rem_closed, rem_parked, rem_installed_sha, _rem_files, rem_co_cnt, rem_co_file, rem_co_lines = _scan_run_files(rem_sha, pnum)
        rem_settled = False
        if rem_closed and not rem_parked and bool(rem_installed_sha):
            inst_anc = subprocess.run(
                ["git", "-C", str(repo), "merge-base", "--is-ancestor", rem_installed_sha, rem_sha],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            if inst_anc.returncode == 0:
                inst_closed, inst_parked, _inst_sha, _inst_files, inst_co_cnt, inst_co_file, inst_co_lines = _scan_run_files(rem_installed_sha, pnum)
                rem_settled = (
                    inst_closed
                    and not inst_parked
                    and inst_co_cnt == rem_co_cnt
                    and inst_co_file == rem_co_file
                    and inst_co_lines == rem_co_lines
                )
        if not rem_settled:
            pending.append(pnum)
            continue
        # `origin/main` has a valid published receipt bound to its latest closeout.
        # If HEAD's last run-file commit is an ancestor of `origin/main` (stale checkouts or unrelated local commits),
        # the phase is settled. If HEAD has unpushed run-file commits, it stays settled ONLY when HEAD's closeout
        # count, file, body, and receipt SHA all match `origin/main` (e.g. unpushed post-receipt CI notes);
        # any unpushed amended closeout or competing receipt remains pending!
        rl = subprocess.run(
            ["git", "-C", str(repo), "rev-list", "-1", "HEAD", "--", *head_files],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if rl.returncode != 0 or not rl.stdout.strip():
            _fail(f"cannot determine last commit for phase {pnum} run files in {repo}")
        head_run_commit = rl.stdout.strip()
        anc = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", head_run_commit, rem_sha],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if anc.returncode not in (0, 1):
            _fail(f"cannot compare ancestry for phase {pnum} run files in {repo}: {(anc.stderr or anc.stdout).strip()}")
        if anc.returncode != 0:
            same_settled_closeout = (
                head_installed_sha == rem_installed_sha
                and head_co_cnt == rem_co_cnt
                and head_co_file == rem_co_file
                and head_co_lines == rem_co_lines
            )
            if not same_settled_closeout:
                pending.append(pnum)
    return sorted(pending)


def _create_initial_phase_journal(jp: Path, payload: dict[str, Any], install_mod: Any) -> None:
    """Atomically create the initial acquiring phase.journal.json via Linux O_TMPFILE + linkat(AT_SYMLINK_FOLLOW).

    No directory entry exists in `jp.parent` until the complete, fsynced JSON inode is atomically linked to `jp`
    in a single kernel syscall. Passing `dst_dir_fd` + relative `jp.name` + `follow_symlinks=True` guarantees
    CPython 3.12+ calls `linkat(..., AT_SYMLINK_FOLLOW)` rather than 2-arg `link()`.
    """
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    dir_fd = os.open(str(jp.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        fd = os.open(str(jp.parent), os.O_TMPFILE | os.O_WRONLY, 0o644)
        try:
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write while creating initial phase journal")
                view = view[written:]
            os.fsync(fd)
            try:
                os.link(f"/proc/self/fd/{fd}", jp.name, dst_dir_fd=dir_fd, follow_symlinks=True)
            except FileExistsError as exc:
                raise install_mod.InstallError(f"refusing existing phase journal at {jp}", code=2) from exc
            os.fsync(dir_fd)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def _sweep_phase_journal_temps(to_dir: Path, tx_id: str, install_mod: Any) -> None:
    """Remove only this transaction's atomic-writer `.<target>.tx-<tx_id>.tmp.<pid>.<8hex>` files."""
    if not tx_id or not re.fullmatch(r"[0-9a-f]{12}", tx_id):
        return
    pat = re.compile(
        rf"^\.(?:phase\.journal\.json|helio\.old|helio\.exe\.old)\.tx-{re.escape(tx_id)}\.tmp\.[0-9]+\.[0-9a-f]{{8}}$"
    )
    removed = False
    for p in to_dir.iterdir():
        if pat.fullmatch(p.name) and p.is_file() and not p.is_symlink():
            p.unlink()
            removed = True
    if removed:
        install_mod.fsync_dir(to_dir)


def _capture_wsl_previous_b64(to_dir: Path, pre_previous: dict[str, Any], install_mod: Any) -> tuple[str, str]:
    """Read-only capture of helio.old and helio.exe.old as verified base64 strings stored inside phase.journal.json."""
    old_h = to_dir / "helio.old"
    old_e = to_dir / "helio.exe.old"
    if not old_h.is_file() or not old_e.is_file() or old_h.is_symlink() or old_e.is_symlink():
        raise install_mod.InstallError(f"missing WSL previous generation files in {to_dir}", code=3)
    raw_h = old_h.read_bytes()
    raw_e = old_e.read_bytes()
    exp_h = str(pre_previous.get("helio_sha256") or "")
    exp_e = str(pre_previous.get("helio_exe_sha256") or "")
    if (exp_h and hashlib.sha256(raw_h).hexdigest() != exp_h) or (exp_e and hashlib.sha256(raw_e).hexdigest() != exp_e):
        raise install_mod.InstallError(f"WSL previous generation hash mismatch in {to_dir}", code=3)
    return base64.b64encode(raw_h).decode("ascii"), base64.b64encode(raw_e).decode("ascii")


def _restore_wsl_previous(
    to_dir: Path,
    journal: dict[str, Any],
    pre_previous: dict[str, Any] | None,
    install_mod: Any,
) -> None:
    """Restore or clear helio.old, helio.exe.old, and installed.json['previous'] from verified in-journal backup bytes."""
    manifest = install_mod.read_manifest(to_dir)
    if not isinstance(manifest, dict):
        raise install_mod.InstallError(f"missing WSL manifest in {to_dir} when restoring previous generation", code=3)
    if pre_previous is None:
        # First upgrade (B was sole generation -> failed C): remove failed C backups and clear `previous`!
        for name in ("helio.old", "helio.exe.old"):
            p = to_dir / name
            if p.exists() and not p.is_symlink():
                p.unlink()
        manifest["previous"] = None
        install_mod.write_json_atomic(to_dir / install_mod.MANIFEST_NAME, manifest)
        install_mod.fsync_dir(to_dir)
        return
    b64_h = journal.get("wsl_pre_prev_helio_b64")
    b64_e = journal.get("wsl_pre_prev_helio_exe_b64")
    if not isinstance(b64_h, str) or not b64_h or not isinstance(b64_e, str) or not b64_e:
        raise install_mod.InstallError(f"missing WSL previous generation backup in phase journal for {to_dir}", code=3)
    raw_h = base64.b64decode(b64_h.encode("ascii"))
    raw_e = base64.b64decode(b64_e.encode("ascii"))
    exp_h = str(pre_previous.get("helio_sha256") or "")
    exp_e = str(pre_previous.get("helio_exe_sha256") or "")
    if (exp_h and hashlib.sha256(raw_h).hexdigest() != exp_h) or (exp_e and hashlib.sha256(raw_e).hexdigest() != exp_e):
        raise install_mod.InstallError(f"WSL phase previous backup hash mismatch for {to_dir}", code=3)
    j_tx = str(journal.get("tx_id") or "") or None
    install_mod.write_bytes_atomic(to_dir / "helio.old", raw_h, mode=0o755, tx_id=j_tx)
    install_mod.write_bytes_atomic(to_dir / "helio.exe.old", raw_e, mode=0o755, tx_id=j_tx)
    manifest["previous"] = pre_previous
    install_mod.write_json_atomic(to_dir / install_mod.MANIFEST_NAME, manifest)


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 1800,
    install_mod: Any | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            input=input_text,
            stdin=subprocess.DEVNULL if input_text is None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        if install_mod is not None:
            raise install_mod.InstallError(
                f"command timed out after {timeout}s: {' '.join(argv[:4])}",
                code=3,
            ) from exc
        raise


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-C", str(repo), *args], timeout=120)


def _check_interop(install_mod: Any) -> tuple[str, str]:
    if os.name != "posix":
        raise install_mod.InstallError(
            "phase replacement must run from WSL so both WSL and Windows proofs and installs execute",
            code=3,
        )
    python_exe = shutil.which("python.exe")
    wslpath_bin = shutil.which("wslpath")
    missing = [name for name, found in (("python.exe", python_exe), ("wslpath", wslpath_bin)) if not found]
    if missing:
        raise install_mod.InstallError(
            f"WSL-to-Windows interop unavailable (missing on PATH: {', '.join(missing)})",
            code=3,
        )
    assert python_exe is not None and wslpath_bin is not None
    return (python_exe, wslpath_bin)


def _validate_phase_verified_and_closed(repo: Path, phase: int, commit: str, install_mod: Any) -> None:
    if phase < 0:
        raise install_mod.InstallError(f"invalid phase number: {phase}", code=2)

    graph_script = repo / "scripts" / "todo-graph.py"
    if not graph_script.is_file():
        raise install_mod.InstallError(f"missing todo-graph script: {graph_script}", code=2)

    res = _run([sys.executable, str(graph_script), "query", "plan"], cwd=repo, timeout=60)
    if res.returncode != 0:
        raise install_mod.InstallError(
            f"todo-graph query plan failed (exit {res.returncode}): {(res.stderr or res.stdout).strip()}",
            code=2,
        )
    try:
        plan = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise install_mod.InstallError(f"todo-graph query plan returned invalid JSON: {exc}", code=2) from exc

    phases = plan.get("phases") or {}
    if str(phase) not in phases:
        raise install_mod.InstallError(f"unknown phase {phase}: not declared in todo/plan.json", code=2)

    sections = [s for s in (plan.get("sections") or []) if s.get("phase") == phase]
    if not sections:
        raise install_mod.InstallError(f"phase {phase} has no sections in the implementation plan", code=2)

    unverified = [s.get("ref", "?") for s in sections if s.get("status") != "verified"]
    if unverified:
        raise install_mod.InstallError(
            f"phase {phase} is not fully verified; open sections: {', '.join(unverified)}",
            code=2,
        )

    # Require a committed closeout record under docs/phase-runs/*-phase-<phase>.md at `commit` without an open PARKED state.
    ls = _git(repo, "ls-tree", "-r", "--name-only", commit, "docs/phase-runs")
    if ls.returncode != 0:
        raise install_mod.InstallError(
            f"cannot inspect committed phase run files in docs/phase-runs at {commit[:12]}",
            code=2,
        )
    suffix = f"-phase-{phase}.md"
    candidates = [ln.strip() for ln in ls.stdout.splitlines() if ln.strip().endswith(suffix)]
    if not candidates:
        raise install_mod.InstallError(
            f"phase {phase} has no committed run file matching docs/phase-runs/*{suffix} at {commit[:12]}",
            code=2,
        )

    last_event: str | None = None
    for rel in sorted(candidates):
        show = _git(repo, "show", f"{commit}:{rel}")
        if show.returncode != 0:
            continue
        for line in show.stdout.splitlines():
            if re.match(r"^##\s+Closeout\b", line):
                last_event = "closeout"
            elif line.startswith("PARKED "):
                last_event = "parked"

    if last_event != "closeout":
        if last_event == "parked":
            raise install_mod.InstallError(
                f"phase {phase} has an open PARKED record in docs/phase-runs/*{suffix} with no subsequent closeout",
                code=2,
            )
        raise install_mod.InstallError(
            f"phase {phase} has no committed `## Closeout` heading in docs/phase-runs/*{suffix} at {commit[:12]}",
            code=2,
        )


def _check_clean_and_origin_main(repo: Path, install_mod: Any, *, expected_commit: str | None = None) -> str:
    st = _git(repo, "status", "--porcelain")
    if st.returncode != 0:
        raise install_mod.InstallError(f"git status failed: {st.stderr.strip()}", code=2)
    if st.stdout.strip():
        raise install_mod.InstallError("refusing phase install: working tree has uncommitted changes", code=2)

    head = _git(repo, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise install_mod.InstallError(f"git rev-parse HEAD failed: {head.stderr.strip()}", code=2)
    commit = head.stdout.strip()
    if expected_commit is not None and commit != expected_commit:
        raise install_mod.InstallError(
            f"refusing phase install: HEAD drifted from {expected_commit[:12]} to {commit[:12]}",
            code=3,
        )

    # Query origin directly with `git ls-remote` (single read-only call, never touches mutable FETCH_HEAD
    # and ignores narrowed remote.origin.fetch mappings).
    ls_rem = _git(repo, "ls-remote", "origin", "refs/heads/main")
    if ls_rem.returncode != 0:
        raise install_mod.InstallError(
            f"refusing phase install: cannot verify authoritative origin/main ({ls_rem.stderr.strip()})",
            code=2,
        )
    remote_sha = ""
    for line in ls_rem.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "refs/heads/main":
            remote_sha = parts[0].strip()
            break
    if not remote_sha:
        raise install_mod.InstallError(
            "refusing phase install: origin has no refs/heads/main",
            code=2,
        )
    if commit != remote_sha:
        raise install_mod.InstallError(
            f"refusing phase install: HEAD ({commit[:12]}) is not at authoritative origin/main ({remote_sha[:12]})",
            code=2,
        )
    return commit


def _verify_race_test_ran(output: str, install_mod: Any) -> None:
    found_ok = False
    for line in output.splitlines():
        if RACE_TEST_NAME in line:
            low = line.lower()
            if "skipped" in low:
                raise install_mod.InstallError(
                    f"refusing phase install: {RACE_TEST_NAME} was skipped in app_build.py output",
                    code=3,
                )
            if low.rstrip().endswith("... ok") or low.rstrip().endswith(" ok"):
                found_ok = True
    if not found_ok:
        raise install_mod.InstallError(
            f"refusing phase install: {RACE_TEST_NAME} did not pass in app_build.py output",
            code=3,
        )


def _find_sdk_dir(repo: Path, sdk_url: str, install_mod: Any) -> Path:
    sdk_dir = repo.parent / "ImpossibleSDK"
    if not (sdk_dir / "tools" / "app_build.py").is_file() or not (sdk_dir / "tools" / "prove.py").is_file():
        raise install_mod.InstallError(
            f"ImpossibleSDK with app_build.py and prove.py not found beside checkout at {sdk_dir}",
            code=3,
        )
    return sdk_dir


def _probe_win_state(
    python_exe: str,
    win_install_py_win: str,
    win_to_raw: str | None,
    reg_key: str,
    install_mod: Any | None = None,
) -> dict[str, Any]:
    """Query Windows installation state (resolved dir, manifest, status code) via Windows Python."""
    code = (
        "import importlib.util, json, sys\n"
        "from pathlib import Path\n"
        "spec = importlib.util.spec_from_file_location('wi', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "win_to = Path(sys.argv[2]) if sys.argv[2] else None\n"
        "reg_key = sys.argv[3]\n"
        "dest = (win_to or mod.default_win_to_dir()).resolve()\n"
        "manifest = mod.read_win_manifest(dest) if dest.is_dir() else None\n"
        "st_code, st_lines = mod.win_status(win_to=win_to, reg_key=reg_key)\n"
        "exe_p = dest / mod.WIN_BIN_SUBDIR / mod.WIN_EXE_NAME\n"
        "exe_sha = mod.sha256_file(exe_p) if exe_p.is_file() else ''\n"
        "marker_tx = mod.read_win_phase_marker_tx(dest) if dest.is_dir() else None\n"
        "print(json.dumps({'dest': str(dest), 'manifest': manifest, 'status_code': st_code, "
        "'status_lines': st_lines, 'exe_sha': exe_sha, 'phase_marker_tx': marker_tx}))\n"
    )
    res = _run([python_exe, "-B", "-c", code, win_install_py_win, win_to_raw or "", reg_key], timeout=60, install_mod=install_mod)
    if res.returncode != 0:
        msg = f"Windows state probe failed (exit {res.returncode}): {(res.stderr or res.stdout).strip()}"
        if install_mod is not None:
            raise install_mod.InstallError(msg, code=3)
        raise RuntimeError(msg)
    try:
        return json.loads(res.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        msg = f"Windows state probe returned invalid JSON: {exc}"
        if install_mod is not None:
            raise install_mod.InstallError(msg, code=3) from exc
        raise RuntimeError(msg) from exc


def _win_phase_marker_call(
    python_exe: str,
    win_install_py_win: str,
    action: str,
    win_to_raw: str | None,
    reg_key: str,
    tx_id: str,
    phase: int = 0,
    commit: str = "",
    payload_json: str = "",
    stdin_json: str | None = None,
    install_mod: Any | None = None,
) -> str:
    code = (
        "import base64, importlib.util, json, sys\n"
        "from pathlib import Path\n"
        "spec = importlib.util.spec_from_file_location('wi', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "action = sys.argv[2]\n"
        "win_to = Path(sys.argv[3]) if sys.argv[3] else None\n"
        "reg_key, tx_id, phase, commit, payload = sys.argv[4], sys.argv[5], int(sys.argv[6]), sys.argv[7], sys.argv[8]\n"
        "try:\n"
        "    if action == 'begin':\n"
        "        mod.win_phase_begin(win_to=win_to, reg_key=reg_key, tx_id=tx_id, phase=phase, commit=commit)\n"
        "    elif action == 'end':\n"
        "        mod.win_phase_end(win_to=win_to, tx_id=tx_id)\n"
        "    elif action == 'recover':\n"
        "        mod.win_recover(win_to=win_to, reg_key=reg_key, phase_tx_id=tx_id or None)\n"
        "    elif action == 'snapshot_prev':\n"
        "        raw = mod.win_phase_snapshot_prev(win_to=win_to, tx_id=tx_id)\n"
        "        if raw is not None:\n"
        "            sys.stdout.write(base64.b64encode(raw).decode('ascii'))\n"
        "    elif action == 'restore_prev':\n"
        "        data = json.loads(sys.stdin.read())\n"
        "        rb64 = data.get('restore_b64')\n"
        "        rbytes = base64.b64decode(rb64.encode('ascii')) if isinstance(rb64, str) and rb64 else None\n"
        "        mod.win_phase_restore_prev(win_to=win_to, tx_id=tx_id, pre_previous=data.get('pre_previous'), restore_bytes=rbytes)\n"
        "except mod.WinInstallError as exc:\n"
        "    print(str(exc), file=sys.stderr)\n"
        "    sys.exit(exc.code)\n"
    )
    res = _run(
        [python_exe, "-B", "-c", code, win_install_py_win, action, win_to_raw or "", reg_key, tx_id, str(phase), commit, payload_json],
        input_text=stdin_json,
        timeout=60,
        install_mod=install_mod,
    )
    if res.returncode != 0:
        msg = f"Windows phase {action} failed (exit {res.returncode}): {(res.stderr or res.stdout).strip()}"
        if install_mod is not None:
            raise install_mod.InstallError(msg, code=3)
        raise RuntimeError(msg)
    return res.stdout


def _run_win_cli(
    python_exe: str,
    install_py_win: str,
    args: list[str],
    *,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    return _run([python_exe, "-B", install_py_win, "--windows", *args], timeout=timeout)


def _run_clean_win_helio(
    python_exe: str,
    reg_key: str,
    helio_args: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Run bare `helio` in a fresh cmd.exe environment with HELIO_STORE unset and PATH from HKCU\\<reg_key>\\Path."""
    helper = (
        "import os, subprocess, sys, winreg\n"
        "reg_key = sys.argv[1]\n"
        "args = sys.argv[2:]\n"
        "user_path = ''\n"
        "try:\n"
        "    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_key, 0, winreg.KEY_READ) as k:\n"
        "        val, val_type = winreg.QueryValueEx(k, 'Path')\n"
        "        if isinstance(val, str):\n"
        "            user_path = os.path.expandvars(val) if val_type == winreg.REG_EXPAND_SZ else val\n"
        "except OSError:\n"
        "    pass\n"
        "sys_path = ''\n"
        "try:\n"
        "    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment', 0, winreg.KEY_READ) as k:\n"
        "        val, val_type = winreg.QueryValueEx(k, 'Path')\n"
        "        if isinstance(val, str):\n"
        "            sys_path = os.path.expandvars(val) if val_type == winreg.REG_EXPAND_SZ else val\n"
        "except OSError:\n"
        "    sys_path = os.environ.get('PATH', '')\n"
        "env = {k: v for k, v in os.environ.items() if k.upper() not in ('HELIO_STORE', 'HELIO_DEFAULT_STORE', 'HELIO_DEFAULT_STORE_HEX', 'HELIO_AS')}\n"
        "env['PATH'] = (sys_path + ';' + user_path) if (sys_path and user_path) else (sys_path or user_path)\n"
        "res = subprocess.run(['cmd.exe', '/d', '/c', 'helio', *args], env=env, cwd=os.environ.get('TEMP', 'C:\\\\'), "
        "stdin=sys.stdin, capture_output=False)\n"
        "sys.exit(res.returncode)\n"
    )
    return _run([python_exe, "-B", "-c", helper, reg_key, *helio_args], input_text=input_text, timeout=timeout)


def _run_clean_wsl_helio(
    wrapper_path: Path,
    helio_args: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 60,
    require_on_path: bool = False,
    install_mod: Any | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run bare `helio` in a fresh POSIX environment with HELIO_STORE unset."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("HELIO_STORE", "HELIO_DEFAULT_STORE", "HELIO_DEFAULT_STORE_HEX", "HELIO_AS")
    }
    base_path = env.get("PATH", "/usr/bin:/bin")
    if require_on_path:
        env["PATH"] = base_path
        winner = shutil.which("helio", path=base_path)
        if winner is None:
            if install_mod is not None:
                raise install_mod.InstallError(
                    f"installed WSL wrapper {wrapper_path} is not reachable on PATH ({wrapper_path.parent} missing from PATH)",
                    code=3,
                )
        elif Path(winner).resolve() != wrapper_path.resolve():
            if install_mod is not None:
                raise install_mod.InstallError(
                    f"another helio earlier on PATH shadows {wrapper_path}: {winner}",
                    code=3,
                )
        return _run(
            ["sh", "-c", 'exec helio "$@"', "sh", *helio_args],
            env=env,
            cwd=Path("/tmp"),
            input_text=input_text,
            timeout=timeout,
            install_mod=install_mod,
        )
    return _run(
        [str(wrapper_path), *helio_args],
        env=env,
        cwd=Path("/tmp"),
        input_text=input_text,
        timeout=timeout,
        install_mod=install_mod,
    )


def _verify_live_installations(
    *,
    to_dir: Path,
    wrapper_path: Path,
    store_dir: Path,
    win_to_raw: str | None,
    reg_key: str,
    commit: str,
    proved_wsl_sha: str,
    proved_win_sha: str,
    python_exe: str,
    win_install_py_win: str,
    install_mod: Any,
    win_mod: Any,
    on_verify: Callable[[str], None] | None = None,
) -> None:
    if on_verify:
        on_verify("before_verify")

    # 1. Verify WSL live status, commit provenance, and exact binary digests.
    wsl_code, wsl_lines = install_mod.status(to_dir=to_dir, wrapper_path=wrapper_path)
    if wsl_code != 0:
        raise install_mod.InstallError(f"post-install WSL status unhealthy (exit {wsl_code}): {'; '.join(wsl_lines)}", code=3)
    wsl_m = install_mod.read_manifest(to_dir) or {}
    wsl_cur = wsl_m.get("current") or {}
    if wsl_cur.get("commit") != commit or bool(wsl_cur.get("dirty", False)):
        raise install_mod.InstallError(
            f"post-install WSL manifest commit mismatch: expected clean {commit[:12]}, got {wsl_cur.get('commit')}",
            code=3,
        )
    live_wsl_sha = install_mod.sha256_file(to_dir / "helio")
    live_wsl_exe_sha = install_mod.sha256_file(to_dir / "helio.exe")
    if live_wsl_sha != proved_wsl_sha or live_wsl_exe_sha != proved_win_sha:
        raise install_mod.InstallError("post-install WSL binaries do not match proved SHA-256 digests", code=3)

    # 2. Verify Windows live status (includes PATH shadowing check), commit provenance, and exact binary digest.
    win_state = _probe_win_state(python_exe, win_install_py_win, win_to_raw, reg_key, install_mod=install_mod)
    if win_state["status_code"] != 0:
        raise install_mod.InstallError(
            f"post-install Windows status unhealthy (exit {win_state['status_code']}): {'; '.join(win_state['status_lines'])}",
            code=3,
        )
    win_cur = (win_state.get("manifest") or {}).get("current") or {}
    if win_cur.get("commit") != commit or bool(win_cur.get("dirty", False)):
        raise install_mod.InstallError(
            f"post-install Windows manifest commit mismatch: expected clean {commit[:12]}, got {win_cur.get('commit')}",
            code=3,
        )
    if win_state["exe_sha"] != proved_win_sha:
        raise install_mod.InstallError("post-install Windows helio.exe does not match proved SHA-256 digest", code=3)

    if on_verify:
        on_verify("before_real_store_check")

    # 3. Clean-environment `helio check` on the REAL store from both WSL and Windows entry points
    # using the session's ACTUAL PATH (never prepending a missing wrapper directory).
    wsl_real = _run_clean_wsl_helio(
        wrapper_path,
        ["check"],
        require_on_path=True,
        install_mod=install_mod,
    )
    if wsl_real.returncode != 0:
        raise install_mod.InstallError(
            f"installed WSL `helio check` failed on real store (exit {wsl_real.returncode}): {(wsl_real.stderr or wsl_real.stdout).strip()}",
            code=3,
        )
    win_real = _run_clean_win_helio(python_exe, reg_key, ["check"])
    if win_real.returncode != 0:
        raise install_mod.InstallError(
            f"installed Windows `helio check` failed on real store (exit {win_real.returncode}): {(win_real.stderr or win_real.stdout).strip()}",
            code=3,
        )

    if on_verify:
        on_verify("before_roundtrip")

    # 4. Isolated cross-OS roundtrip: Windows sends job, WSL takes & finishes it, both show & check it.
    with tempfile.TemporaryDirectory(prefix="helio-phase-smoke-") as smoke_tmp:
        smoke_wsl = Path(smoke_tmp) / "smoke-store"
        smoke_wsl.mkdir()
        smoke_win = win_mod.wsl_to_windows_path(smoke_wsl)

        init_res = _run_clean_wsl_helio(wrapper_path, ["--store", str(smoke_wsl), "init"])
        if init_res.returncode != 0:
            raise install_mod.InstallError(
                f"smoke store init failed (exit {init_res.returncode}): {(init_res.stderr or init_res.stdout).strip()}",
                code=3,
            )

        ask_res = _run_clean_win_helio(
            python_exe,
            reg_key,
            ["--store", smoke_win, "ask", "impossible/all", "phase verification job", "--as", "impossible/win-smoke"],
            input_text="cross-os smoke body\n",
        )
        if ask_res.returncode != 0:
            raise install_mod.InstallError(
                f"Windows smoke `helio ask` failed (exit {ask_res.returncode}): {(ask_res.stderr or ask_res.stdout).strip()}",
                code=3,
            )
        job_id = ask_res.stdout.strip().splitlines()[-1].strip() if ask_res.stdout.strip() else ""
        if not job_id:
            raise install_mod.InstallError("Windows smoke `helio ask` returned empty job id", code=3)

        take_res = _run_clean_wsl_helio(
            wrapper_path,
            ["--store", str(smoke_wsl), "take", job_id, "--as", "impossible/wsl-smoke"],
        )
        if take_res.returncode != 0:
            raise install_mod.InstallError(
                f"WSL smoke `helio take` failed (exit {take_res.returncode}): {(take_res.stderr or take_res.stdout).strip()}",
                code=3,
            )

        done_res = _run_clean_wsl_helio(
            wrapper_path,
            ["--store", str(smoke_wsl), "done", job_id, "verified", "--as", "impossible/wsl-smoke"],
        )
        if done_res.returncode != 0:
            raise install_mod.InstallError(
                f"WSL smoke `helio done` failed (exit {done_res.returncode}): {(done_res.stderr or done_res.stdout).strip()}",
                code=3,
            )

        for label, show_res in (
            ("WSL", _run_clean_wsl_helio(wrapper_path, ["--store", str(smoke_wsl), "show", job_id, "--as", "impossible/wsl-smoke"])),
            ("Windows", _run_clean_win_helio(python_exe, reg_key, ["--store", smoke_win, "show", job_id, "--as", "impossible/win-smoke"])),
        ):
            if show_res.returncode != 0:
                raise install_mod.InstallError(
                    f"{label} smoke `helio show` failed (exit {show_res.returncode}): {(show_res.stderr or show_res.stdout).strip()}",
                    code=3,
                )
            text = show_res.stdout
            if job_id not in text or "impossible/wsl-smoke" not in text or "done" not in text.lower():
                raise install_mod.InstallError(
                    f"{label} smoke `helio show` missing expected job id, taker, or done state: {text.strip()}",
                    code=3,
                )

        for label, chk_res in (
            ("WSL", _run_clean_wsl_helio(wrapper_path, ["--store", str(smoke_wsl), "check"])),
            ("Windows", _run_clean_win_helio(python_exe, reg_key, ["--store", smoke_win, "check"])),
        ):
            if chk_res.returncode != 0:
                raise install_mod.InstallError(
                    f"{label} smoke `helio check` failed (exit {chk_res.returncode}): {(chk_res.stderr or chk_res.stdout).strip()}",
                    code=3,
                )

    if on_verify:
        on_verify("after_verify")


def _send_phase_tell(
    *,
    wrapper_path: Path,
    store_dir: Path,
    phase: int,
    commit: str,
    to_dir: Path,
    win_dest: str,
    install_mod: Any,
    on_boundary: Callable[[str], None] | None = None,
) -> str:
    if on_boundary:
        on_boundary("before_tell")
    key = f"install-phase-{phase}-{commit}"
    subject = f"Heliograph phase {phase} installed ({commit[:12]})"
    body = (
        f"Phase {phase} installed from commit {commit}.\n"
        f"WSL: {to_dir}\n"
        f"Windows: {win_dest}\n"
        f"Store: {store_dir}\n"
    )
    res = _run_clean_wsl_helio(
        wrapper_path,
        [
            "--store",
            str(store_dir),
            "tell",
            PHASE_TELL_TO,
            subject,
            "--as",
            PHASE_TELL_AS,
            "--key",
            key,
        ],
        input_text=body,
    )
    if res.returncode != 0:
        raise install_mod.InstallError(
            f"phase notification `helio tell` failed (exit {res.returncode}): {(res.stderr or res.stdout).strip()}",
            code=3,
        )
    msg_id = res.stdout.strip().splitlines()[-1].strip() if res.stdout.strip() else ""
    if not msg_id:
        raise install_mod.InstallError("phase notification `helio tell` returned empty message id", code=3)
    if on_boundary:
        on_boundary("after_tell")
    return msg_id


def _rollback_phase_journal(
    journal: dict[str, Any],
    *,
    python_exe: str,
    install_py_win: str,
    win_install_py_win: str,
    install_mod: Any,
    on_boundary: Callable[[str], None] | None = None,
) -> None:
    to_dir = Path(journal["wsl_to"])
    wrapper_path = Path(journal["wrapper"])
    win_to_raw = journal.get("win_to_arg")
    reg_key = journal["reg_key"]

    journal["phase_state"] = "rolling_back"
    install_mod.write_json_atomic(to_dir / PHASE_JOURNAL_NAME, journal, tx_id=str(journal.get("tx_id") or "") or None)
    if on_boundary:
        on_boundary("after_rollback_journal")

    tx_id = str(journal.get("tx_id") or "")
    errors: list[str] = []

    if journal.get("win_changed"):
        # Strict schema-2 lock-token ownership: Windows recovery and rollback require `.phase-active.json`
        # to be actively held by `tx_id`. Repeated retries against a missing marker deterministically refuse
        # with exit 3 and preserve `phase.journal.json` unless Windows is already at the expected pre-phase state.
        try:
            cur_win = _probe_win_state(python_exe, win_install_py_win, win_to_raw, reg_key, install_mod=install_mod)
            marker_held = bool(tx_id and cur_win.get("phase_marker_tx") == tx_id)
            if marker_held:
                _win_phase_marker_call(
                    python_exe,
                    win_install_py_win,
                    "recover",
                    win_to_raw,
                    reg_key,
                    tx_id,
                    install_mod=install_mod,
                )
                cur_win = _probe_win_state(python_exe, win_install_py_win, win_to_raw, reg_key, install_mod=install_mod)
                marker_held = bool(tx_id and cur_win.get("phase_marker_tx") == tx_id)
        except Exception as exc:
            errors.append(f"Windows rollback recovery/probe failed: {exc}")
            cur_win = None
            marker_held = False
        if cur_win is not None:
            cur_win_m = cur_win.get("manifest") or {}
            cur_win_c = cur_win_m.get("current") or {}
            win_st = cur_win.get("status_code")
            win_has_pre_prev = "win_pre_previous" in journal
            win_pre_prev = journal.get("win_pre_previous")
            if win_has_pre_prev:
                if win_pre_prev is None:
                    win_prev_matches = cur_win_m.get("previous") is None
                elif isinstance(win_pre_prev, dict):
                    win_prev_matches = (cur_win_m.get("previous") or {}).get("commit") == win_pre_prev.get("commit")
                else:
                    win_prev_matches = False
            else:
                win_prev_matches = True
            win_already_restored = (
                (
                    win_st == 0
                    and cur_win.get("exe_sha") == journal.get("win_pre_exe_sha", "")
                    and cur_win_c.get("commit") == journal.get("win_pre_commit", "")
                    and cur_win_c.get("store") == journal.get("win_pre_store", "")
                    and win_prev_matches
                )
                if journal.get("win_had_install")
                else (win_st == 1 and cur_win.get("manifest") is None and not cur_win.get("exe_sha"))
            )
            if not win_already_restored:
                expected_win_sha = str(journal.get("proved_win_sha") or "")
                expected_win_store = str(journal.get("win_store") or "")
                if not marker_held:
                    errors.append(
                        f"refusing Windows rollback: active phase marker for {tx_id or '?'} is not held on Windows destination"
                    )
                elif (
                    journal.get("win_had_install")
                    and cur_win.get("exe_sha") == journal.get("win_pre_exe_sha", "")
                    and cur_win_c.get("commit") == journal.get("win_pre_commit", "")
                    and cur_win_c.get("store") == journal.get("win_pre_store", "")
                    and win_has_pre_prev
                ):
                    # `current` is already B (including after an interruption between writing helio.exe.old and updating manifest,
                    # where status_code is 3 due to the backup hash mismatch!). Finish restoring/clearing `previous` and re-verify!
                    try:
                        restore_payload = {
                            "pre_previous": win_pre_prev,
                            "restore_b64": journal.get("win_pre_prev_exe_b64") if isinstance(win_pre_prev, dict) else None,
                        }
                        _win_phase_marker_call(
                            python_exe,
                            win_install_py_win,
                            "restore_prev",
                            win_to_raw,
                            reg_key,
                            tx_id,
                            stdin_json=json.dumps(restore_payload),
                            install_mod=install_mod,
                        )
                        post_win = _probe_win_state(python_exe, win_install_py_win, win_to_raw, reg_key, install_mod=install_mod)
                        if post_win.get("status_code") != 0 or post_win["exe_sha"] != journal.get("win_pre_exe_sha", ""):
                            errors.append("Windows previous-generation restoration left unhealthy status or mismatched binary")
                    except Exception as exc:
                        errors.append(f"Windows previous-generation restoration failed: {exc}")
                elif (
                    not expected_win_sha
                    or not expected_win_store
                    or cur_win_c.get("commit") != journal.get("commit")
                    or cur_win.get("exe_sha") != expected_win_sha
                    or cur_win_c.get("store") != expected_win_store
                ):
                    errors.append(
                        f"refusing Windows rollback: live installation ({cur_win_c.get('commit')}, store={cur_win_c.get('store')}) "
                        f"does not match phase publication ({journal.get('commit')}, store={expected_win_store})"
                    )
                else:
                    win_args = ["--back" if journal.get("win_had_install") else "--remove", "--reg-key", reg_key, "--phase-tx", tx_id]
                    if win_to_raw:
                        win_args.extend(["--to", win_to_raw])
                    wres = _run_win_cli(python_exe, install_py_win, win_args)
                    if wres.returncode != 0:
                        errors.append(f"Windows rollback failed (exit {wres.returncode}): {(wres.stderr or wres.stdout).strip()}")
                    else:
                        try:
                            if journal.get("win_had_install") and win_has_pre_prev:
                                restore_payload = {
                                    "pre_previous": win_pre_prev,
                                    "restore_b64": journal.get("win_pre_prev_exe_b64") if isinstance(win_pre_prev, dict) else None,
                                }
                                _win_phase_marker_call(
                                    python_exe,
                                    win_install_py_win,
                                    "restore_prev",
                                    win_to_raw,
                                    reg_key,
                                    tx_id,
                                    stdin_json=json.dumps(restore_payload),
                                    install_mod=install_mod,
                                )
                            post_win = _probe_win_state(python_exe, win_install_py_win, win_to_raw, reg_key, install_mod=install_mod)
                            if journal.get("win_had_install"):
                                if post_win.get("status_code") != 0 or post_win["exe_sha"] != journal.get("win_pre_exe_sha", ""):
                                    errors.append("Windows rollback hash/status does not match pre-transaction binary")
                            else:
                                if post_win.get("status_code") != 1 or post_win["manifest"] is not None:
                                    errors.append("Windows rollback did not remove newly installed manifest")
                        except Exception as exc:
                            errors.append(f"Windows rollback verification failed: {exc}")
        if on_boundary:
            on_boundary("after_win_rollback")

    if journal.get("wsl_changed"):
        try:
            install_mod.recover_destination(to_dir, wrapper_path)
            cur_wsl_m = install_mod.read_manifest(to_dir)
            cur_wsl_c = (cur_wsl_m or {}).get("current") or {}
            cur_wsl_sha = install_mod.sha256_file(to_dir / "helio") if (to_dir / "helio").is_file() else ""
            wsl_has_pre_prev = "wsl_pre_previous" in journal
            wsl_pre_prev = journal.get("wsl_pre_previous")
            if wsl_has_pre_prev:
                if wsl_pre_prev is None:
                    wsl_prev_matches = (cur_wsl_m or {}).get("previous") is None and not (to_dir / "helio.old").exists()
                elif isinstance(wsl_pre_prev, dict):
                    wsl_prev_matches = (((cur_wsl_m or {}).get("previous") or {}).get("commit") == wsl_pre_prev.get("commit"))
                else:
                    wsl_prev_matches = False
            else:
                wsl_prev_matches = True
            if journal.get("wsl_had_legacy"):
                wsl_already_restored = cur_wsl_sha == journal.get("wsl_pre_helio_sha", "")
            elif journal.get("wsl_had_install"):
                wsl_already_restored = (
                    cur_wsl_sha == journal.get("wsl_pre_helio_sha", "")
                    and cur_wsl_c.get("commit") == journal.get("wsl_pre_commit", "")
                    and cur_wsl_c.get("store") == journal.get("wsl_pre_store", "")
                    and cur_wsl_c.get("wrapper") == journal.get("wsl_pre_wrapper", "")
                    and wsl_prev_matches
                )
            else:
                wsl_already_restored = cur_wsl_m is None and not (to_dir / "helio").exists()
            if not wsl_already_restored:
                expected_wsl_sha = str(journal.get("proved_wsl_sha") or "")
                if (
                    journal.get("wsl_had_install")
                    and cur_wsl_sha == journal.get("wsl_pre_helio_sha", "")
                    and cur_wsl_c.get("commit") == journal.get("wsl_pre_commit", "")
                    and wsl_has_pre_prev
                ):
                    _restore_wsl_previous(to_dir, journal, wsl_pre_prev, install_mod)
                elif not expected_wsl_sha or cur_wsl_c.get("commit") != journal.get("commit") or cur_wsl_sha != expected_wsl_sha:
                    errors.append(
                        f"refusing WSL rollback: live installation ({cur_wsl_c.get('commit')}) does not match phase publication ({journal.get('commit')})"
                    )
                elif journal.get("wsl_had_install") or journal.get("wsl_had_legacy"):
                    install_mod.go_back(to_dir=to_dir, wrapper_path=wrapper_path, allow_phase_journal=True)
                    if journal.get("wsl_had_install") and wsl_has_pre_prev:
                        _restore_wsl_previous(to_dir, journal, wsl_pre_prev, install_mod)
                    live_sha = install_mod.sha256_file(to_dir / "helio")
                    if live_sha != journal.get("wsl_pre_helio_sha", ""):
                        errors.append("WSL rollback hash does not match pre-transaction binary")
                else:
                    install_mod.remove(to_dir=to_dir, wrapper_path=wrapper_path, allow_phase_journal=True)
                    if install_mod.read_manifest(to_dir) is not None:
                        errors.append("WSL rollback did not remove newly installed manifest")
        except Exception as exc:
            errors.append(f"WSL rollback failed: {exc}")
        if on_boundary:
            on_boundary("after_wsl_rollback")

    # Keep the Windows phase marker active if rollback had ANY error so no concurrent installer can interleave before retry!
    if errors:
        raise install_mod.InstallError("phase rollback incomplete: " + "; ".join(errors), code=3)

    # Durable commit point for rollback: write `phase_state = "rolled_back"` FIRST while `.phase-active.json`
    # is still held so a crash before unlinking the WSL journal NEVER re-enters rollback!
    journal["phase_state"] = "rolled_back"
    install_mod.write_json_atomic(to_dir / PHASE_JOURNAL_NAME, journal, tx_id=str(journal.get("tx_id") or "") or None)

    if tx_id:
        try:
            _win_phase_marker_call(python_exe, win_install_py_win, "end", win_to_raw, reg_key, tx_id, install_mod=install_mod)
        except Exception as exc:
            raise install_mod.InstallError(f"phase rollback succeeded but failed to release Windows phase marker: {exc}", code=3) from exc
    if on_boundary:
        on_boundary("after_rollback_marker_end")

    _sweep_phase_journal_temps(to_dir, tx_id, install_mod)
    jp = to_dir / PHASE_JOURNAL_NAME
    if jp.exists():
        jp.unlink()
        install_mod.fsync_dir(to_dir)


_REQUIRED_PHASE_JOURNAL_KEYS = (
    "tx_id",
    "phase",
    "commit",
    "phase_state",
    "wsl_to",
    "wrapper",
    "store",
    "win_store",
    "win_dest",
    "reg_key",
    "proved_wsl_sha",
    "proved_win_sha",
    "wsl_changed",
    "win_changed",
    "wsl_had_install",
    "win_had_install",
)


def _read_and_validate_phase_journal(
    to_dir: Path,
    *,
    wrapper_path: Path,
    store_dir: Path,
    win_to_raw: str | None,
    reg_key: str,
    install_mod: Any,
) -> dict[str, Any]:
    """Validate ownership and exact destination/wrapper/store binding of phase.journal.json BEFORE any side effect."""
    jp = to_dir / PHASE_JOURNAL_NAME
    if jp.is_symlink():
        raise install_mod.InstallError(f"refusing symlink phase journal at {jp}", code=2)
    if not jp.is_file():
        raise install_mod.InstallError(f"refusing non-regular phase journal at {jp}", code=2)
    try:
        journal = json.loads(jp.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise install_mod.InstallError(f"refusing unreadable or foreign phase journal at {jp}: {exc}", code=2) from exc
    if not isinstance(journal, dict):
        raise install_mod.InstallError(f"refusing foreign phase journal at {jp}", code=2)
    sv = journal.get("schema_version")
    if sv != 2:
        raise install_mod.InstallError(f"refusing unsupported phase journal schema_version {sv!r} at {jp}", code=2)
    if journal.get("managed_by") != "heliograph-phase-installer":
        raise install_mod.InstallError(f"refusing unowned phase journal at {jp}", code=2)
    if not all(k in journal for k in _REQUIRED_PHASE_JOURNAL_KEYS):
        raise install_mod.InstallError(f"refusing incomplete or foreign phase journal at {jp}", code=2)
    for bkey in ("wsl_changed", "win_changed", "wsl_had_install", "win_had_install"):
        if not isinstance(journal.get(bkey), bool):
            raise install_mod.InstallError(f"refusing phase journal with non-boolean {bkey} at {jp}", code=2)

    tx_id = journal.get("tx_id")
    if not isinstance(tx_id, str) or not re.fullmatch(r"[0-9a-f]{12}", tx_id):
        raise install_mod.InstallError(f"refusing phase journal with invalid tx_id at {jp}", code=2)
    phase_val = journal.get("phase")
    if isinstance(phase_val, bool) or not isinstance(phase_val, int) or phase_val < 0:
        raise install_mod.InstallError(f"refusing phase journal with invalid phase {phase_val!r} at {jp}", code=2)
    if journal.get("phase_state") not in ("acquiring", "planned", "wsl_done", "both_done", "notifying", "rolling_back", "rolled_back"):
        raise install_mod.InstallError(f"refusing phase journal with invalid state at {jp}", code=2)
    for dkey in ("proved_wsl_sha", "proved_win_sha"):
        dval = journal.get(dkey)
        if not isinstance(dval, str) or not re.fullmatch(r"[0-9a-f]{64}", dval):
            raise install_mod.InstallError(f"refusing phase journal with invalid {dkey} at {jp}", code=2)
    rec_to = journal.get("wsl_to")
    if not isinstance(rec_to, str) or not rec_to or Path(rec_to).resolve() != to_dir.resolve():
        raise install_mod.InstallError(
            f"refusing phase journal bound to {rec_to!r}, expected {to_dir.resolve()}",
            code=2,
        )
    rec_wrap = journal.get("wrapper")
    if not isinstance(rec_wrap, str) or not rec_wrap or install_mod._normalize_wrapper_path(Path(rec_wrap)) != wrapper_path:
        raise install_mod.InstallError(
            f"refusing phase journal bound to wrapper {rec_wrap!r}, expected {wrapper_path}",
            code=2,
        )
    rec_store = journal.get("store")
    if not isinstance(rec_store, str) or not rec_store or Path(rec_store).resolve() != store_dir.resolve():
        raise install_mod.InstallError(
            f"refusing phase journal bound to store {rec_store!r}, expected {store_dir.resolve()}",
            code=2,
        )
    if journal.get("win_to_arg") != win_to_raw or str(journal.get("reg_key") or "").lower() != reg_key.lower():
        raise install_mod.InstallError(
            f"refusing phase journal bound to Windows target ({journal.get('win_to_arg')!r}, {journal.get('reg_key')!r}), "
            f"expected ({win_to_raw!r}, {reg_key!r})",
            code=2,
        )
    if not isinstance(journal.get("win_store"), str) or not journal.get("win_store"):
        raise install_mod.InstallError(f"refusing phase journal with invalid win_store at {jp}", code=2)
    if not isinstance(journal.get("win_dest"), str):
        raise install_mod.InstallError(f"refusing phase journal with invalid win_dest at {jp}", code=2)
    if journal.get("phase_state") == "notifying":
        if not journal.get("win_dest"):
            raise install_mod.InstallError(f"refusing notifying phase journal with empty win_dest at {jp}", code=2)
        if not install_mod.wrapper_belongs_to(wrapper_path, to_dir):
            raise install_mod.InstallError(
                f"refusing phase notification recovery: wrapper {wrapper_path} is not owned by {to_dir}",
                code=2,
            )
    return journal


def _recover_pending_phase_journal(
    *,
    to_dir: Path,
    wrapper_path: Path,
    store_dir: Path,
    win_to_raw: str | None,
    reg_key: str,
    python_exe: str,
    install_py_win: str,
    win_install_py_win: str,
    install_mod: Any,
    on_boundary: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    """Recover an existing phase.journal.json. Returns notification result if a `notifying` journal was completed."""
    jp = to_dir / PHASE_JOURNAL_NAME
    if not jp.exists() and not jp.is_symlink():
        return None
    journal = _read_and_validate_phase_journal(
        to_dir,
        wrapper_path=wrapper_path,
        store_dir=store_dir,
        win_to_raw=win_to_raw,
        reg_key=reg_key,
        install_mod=install_mod,
    )

    state = journal.get("phase_state")
    j_tx = str(journal["tx_id"])
    if state == "rolled_back":
        # Rollback already completed durably; release marker and unlink journal (which held all backup bytes).
        _win_phase_marker_call(
            python_exe,
            win_install_py_win,
            "end",
            journal.get("win_to_arg"),
            str(journal["reg_key"]),
            j_tx,
            install_mod=install_mod,
        )
        _sweep_phase_journal_temps(to_dir, j_tx, install_mod)
        jp.unlink()
        install_mod.fsync_dir(to_dir)
        return None
    if state == "notifying":
        # Idempotently release any stranded Windows marker.
        _win_phase_marker_call(
            python_exe,
            win_install_py_win,
            "end",
            journal.get("win_to_arg"),
            str(journal["reg_key"]),
            j_tx,
            install_mod=install_mod,
        )
        msg_id = _send_phase_tell(
            wrapper_path=Path(journal["wrapper"]),
            store_dir=Path(journal["store"]),
            phase=int(journal["phase"]),
            commit=str(journal["commit"]),
            to_dir=Path(journal["wsl_to"]),
            win_dest=str(journal["win_dest"]),
            install_mod=install_mod,
            on_boundary=on_boundary,
        )
        _sweep_phase_journal_temps(to_dir, j_tx, install_mod)
        jp.unlink()
        install_mod.fsync_dir(to_dir)
        return {
            "phase": int(journal["phase"]),
            "commit": str(journal["commit"]),
            "wsl_to": str(journal["wsl_to"]),
            "wrapper": str(journal["wrapper"]),
            "win_to": str(journal["win_dest"]),
            "win_to_arg": journal.get("win_to_arg"),
            "reg_key": str(journal["reg_key"]),
            "store": str(journal["store"]),
            "wsl_changed": False,
            "win_changed": False,
            "notice_id": msg_id,
            "resumed_notification": True,
        }

    _rollback_phase_journal(
        journal,
        python_exe=python_exe,
        install_py_win=install_py_win,
        win_install_py_win=win_install_py_win,
        install_mod=install_mod,
        on_boundary=on_boundary,
    )
    return None


def run_phase_install(
    *,
    repo: Path,
    phase: int,
    to_dir: Path,
    store_dir: Path,
    wrapper_path: Path,
    win_to_raw: str | None,
    reg_key: str,
    sdk_url: str,
    install_mod: Any,
    on_boundary: Callable[[str], None] | None = None,
    on_after_pre_proof_hash: Callable[[Path, Path], None] | None = None,
    on_verify: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Execute the full `--phase <n>` gated replacement across WSL and Windows."""
    python_exe, _ = _check_interop(install_mod)
    if win_to_raw is not None:
        if not win_to_raw.strip() or ";" in win_to_raw or any(ord(c) < 32 for c in win_to_raw):
            raise install_mod.InstallError(f"invalid --win-to path: {win_to_raw!r}", code=2)
    win_mod = install_mod._load_win_install()
    install_py_win = win_mod.wsl_to_windows_path(Path(install_mod.__file__).resolve())
    win_install_py_win = win_mod.wsl_to_windows_path(Path(win_mod.__file__).resolve())

    to_dir = to_dir.resolve()
    store_dir = store_dir.resolve()
    wrapper_path = install_mod._normalize_wrapper_path(wrapper_path)
    win_store = win_mod.wsl_to_windows_path(store_dir)

    def _resumed_matches_request(resumed: dict[str, Any], target_commit: str) -> bool:
        return (
            resumed["phase"] == phase
            and resumed["commit"] == target_commit
            and resumed["wsl_to"] == str(to_dir)
            and resumed["wrapper"] == str(wrapper_path)
            and resumed["store"] == str(store_dir)
            and resumed["win_to_arg"] == win_to_raw
            and resumed["reg_key"] == reg_key
        )

    # First: if a previous `--phase` run left a durable journal in to_dir, acquire the phase lock and recover it.
    if (to_dir / PHASE_JOURNAL_NAME).exists() or (to_dir / PHASE_JOURNAL_NAME).is_symlink():
        with install_mod.acquire_lock(to_dir / PHASE_LOCK_NAME, blocking=False):
            resumed = _recover_pending_phase_journal(
                to_dir=to_dir,
                wrapper_path=wrapper_path,
                store_dir=store_dir,
                win_to_raw=win_to_raw,
                reg_key=reg_key,
                python_exe=python_exe,
                install_py_win=install_py_win,
                win_install_py_win=win_install_py_win,
                install_mod=install_mod,
                on_boundary=on_boundary,
            )
            if resumed is not None:
                head_now = _git(repo, "rev-parse", "HEAD")
                if head_now.returncode == 0 and _resumed_matches_request(resumed, head_now.stdout.strip()):
                    return resumed

    # Pre-flight gates (all read-only; never touch installed destinations on refusal).
    commit = _check_clean_and_origin_main(repo, install_mod)
    _validate_phase_verified_and_closed(repo, phase, commit, install_mod)
    sdk_dir = _find_sdk_dir(repo, sdk_url, install_mod)

    # Gate 3: run app_build.py in WSL and verify the Windows-vs-WSL race test ran and passed.
    build_res = _run([sys.executable, str(sdk_dir / "tools" / "app_build.py"), str(repo)], cwd=repo, timeout=1800, install_mod=install_mod)
    combined_build = build_res.stdout + "\n" + build_res.stderr
    if build_res.returncode != 0:
        raise install_mod.InstallError(
            f"refusing phase install: app_build.py failed (exit {build_res.returncode})",
            code=3,
        )
    _verify_race_test_ran(combined_build, install_mod)

    build_wsl_bin = repo / "build" / "linux" / "linux" / "helio"
    build_win_bin = repo / "build" / "linux" / "helio.exe"
    for p in (build_wsl_bin, build_win_bin):
        if not p.is_file():
            raise install_mod.InstallError(f"refusing phase install: expected built binary missing: {p}", code=3)

    # Gate 4: capture SHA-256 and mtime_ns BEFORE prove.py, and copy into an isolated read-only snapshot dir.
    pre_wsl_sha = install_mod.sha256_file(build_wsl_bin)
    pre_win_sha = install_mod.sha256_file(build_win_bin)
    pre_wsl_mtime = build_wsl_bin.stat().st_mtime_ns
    pre_win_mtime = build_win_bin.stat().st_mtime_ns

    with tempfile.TemporaryDirectory(prefix="helio-phase-snap-") as snap_tmp:
        snap_root = Path(snap_tmp)
        (snap_root / "linux").mkdir()
        snap_wsl = snap_root / "linux" / "helio"
        snap_win = snap_root / "helio.exe"
        shutil.copy2(build_wsl_bin, snap_wsl)
        shutil.copy2(build_win_bin, snap_win)
        os.chmod(snap_wsl, 0o555)
        os.chmod(snap_win, 0o555)
        # Also copy companion test binaries (`racer.exe`, `linux/racer`) if present so Windows test discovery
        # (including `test_many_sessions_take_each_job_once`) executes cleanly against the isolated snapshot dir.
        for rel_extra in (Path("racer.exe"), Path("linux") / "racer"):
            src_extra = repo / "build" / "linux" / rel_extra
            if src_extra.is_file():
                dst_extra = snap_root / rel_extra
                shutil.copy2(src_extra, dst_extra)
                os.chmod(dst_extra, 0o555)
        if install_mod.sha256_file(snap_wsl) != pre_wsl_sha or install_mod.sha256_file(snap_win) != pre_win_sha:
            raise install_mod.InstallError("refusing phase install: snapshot digest mismatch", code=3)

        if on_after_pre_proof_hash:
            on_after_pre_proof_hash(build_wsl_bin, build_win_bin)

        # Run prove.py on the checkout.
        prove_res = _run([sys.executable, str(sdk_dir / "tools" / "prove.py"), str(repo)], cwd=repo, timeout=3600, install_mod=install_mod)
        combined_prove = prove_res.stdout + "\n" + prove_res.stderr
        if prove_res.returncode != 0 or "proved on Windows:" not in combined_prove:
            raise install_mod.InstallError(
                f"refusing phase install: prove.py failed (exit {prove_res.returncode})",
                code=3,
            )

        # Refuse if either binary in build/linux/ was rebuilt or altered during/after prove.py.
        post_wsl_sha = install_mod.sha256_file(build_wsl_bin)
        post_win_sha = install_mod.sha256_file(build_win_bin)
        post_wsl_mtime = build_wsl_bin.stat().st_mtime_ns
        post_win_mtime = build_win_bin.stat().st_mtime_ns
        if (
            post_wsl_sha != pre_wsl_sha
            or post_win_sha != pre_win_sha
            or post_wsl_mtime != pre_wsl_mtime
            or post_win_mtime != pre_win_mtime
        ):
            raise install_mod.InstallError(
                "refusing phase install: built binaries changed or were rebuilt after pre-proof SHA-256 capture",
                code=3,
            )

        # Bind the Windows proof directly to the isolated read-only snapshot directory so Windows exercises
        # the exact captured binaries (cannot be fooled by swapping build/linux/ during prove.py).
        win_snap_dir = win_mod.wsl_to_windows_path(snap_root)
        win_tests_dir = win_mod.wsl_to_windows_path(repo / "tests")
        win_sdk_dir = win_mod.wsl_to_windows_path(sdk_dir)
        snap_env = {
            **os.environ,
            "IMPOSSIBLE_BUILD": win_snap_dir,
            "IMPOSSIBLE_SDK_DIR": win_sdk_dir,
            "PYTHONDONTWRITEBYTECODE": "1",
            "WSLENV": "IMPOSSIBLE_BUILD:IMPOSSIBLE_SDK_DIR:PYTHONDONTWRITEBYTECODE",
        }
        snap_proof = _run(
            [python_exe, "-B", "-m", "unittest", "discover", "-s", win_tests_dir, "-v"],
            cwd=Path("/mnt/c/Windows/Temp"),
            env=snap_env,
            timeout=3600,
            install_mod=install_mod,
        )
        if snap_proof.returncode != 0:
            raise install_mod.InstallError(
                f"refusing phase install: isolated snapshot Windows proof failed (exit {snap_proof.returncode})",
                code=3,
            )

        # Gate 5: recheck clean tree, HEAD == origin/main == commit, and snapshot digests right before publication.
        _check_clean_and_origin_main(repo, install_mod, expected_commit=commit)
        if install_mod.sha256_file(snap_wsl) != pre_wsl_sha or install_mod.sha256_file(snap_win) != pre_win_sha:
            raise install_mod.InstallError("refusing phase install: snapshot digests changed before publication", code=3)

        # Now enter the durable cross-host phase transaction.
        to_dir.mkdir(parents=True, exist_ok=True)
        with install_mod.acquire_lock(to_dir / PHASE_LOCK_NAME, blocking=False):
            # Re-check for a pending journal under the lock in case another phase run crashed while we were proving!
            if (to_dir / PHASE_JOURNAL_NAME).exists() or (to_dir / PHASE_JOURNAL_NAME).is_symlink():
                resumed = _recover_pending_phase_journal(
                    to_dir=to_dir,
                    wrapper_path=wrapper_path,
                    store_dir=store_dir,
                    win_to_raw=win_to_raw,
                    reg_key=reg_key,
                    python_exe=python_exe,
                    install_py_win=install_py_win,
                    win_install_py_win=win_install_py_win,
                    install_mod=install_mod,
                    on_boundary=on_boundary,
                )
                if resumed is not None and _resumed_matches_request(resumed, commit):
                    return resumed

            # Write durable `acquiring` journal FIRST, THEN acquire the Windows phase marker, THEN probe
            # Windows pre-transaction state under that exclusive marker! This guarantees:
            # (a) a crash right after Windows marker acquisition never strands the marker (WSL journal already has tx_id), and
            # (b) no concurrent Windows installer can slip in between probing Windows state and acquiring the marker!
            tx_id = secrets.token_hex(6)
            journal: dict[str, Any] = {
                "schema_version": 2,
                "managed_by": "heliograph-phase-installer",
                "tx_id": tx_id,
                "phase": phase,
                "commit": commit,
                "phase_state": "acquiring",
                "wsl_to": str(to_dir),
                "wrapper": str(wrapper_path),
                "store": str(store_dir),
                "win_store": win_store,
                "win_to_arg": win_to_raw,
                "win_dest": win_to_raw or "",
                "reg_key": reg_key,
                "proved_wsl_sha": pre_wsl_sha,
                "proved_win_sha": pre_win_sha,
                "wsl_had_install": False,
                "wsl_had_legacy": False,
                "wsl_pre_helio_sha": "",
                "wsl_pre_commit": "",
                "wsl_pre_store": "",
                "wsl_pre_wrapper": "",
                "win_had_install": False,
                "win_pre_exe_sha": "",
                "win_pre_commit": "",
                "win_pre_store": "",
                "wsl_changed": False,
                "win_changed": False,
            }
            _create_initial_phase_journal(to_dir / PHASE_JOURNAL_NAME, journal, install_mod)
            try:
                _win_phase_marker_call(
                    python_exe,
                    win_install_py_win,
                    "begin",
                    win_to_raw,
                    reg_key,
                    tx_id,
                    phase=phase,
                    commit=commit,
                    install_mod=install_mod,
                )

                # Capture pre-transaction state for both hosts while BOTH locks/markers are held!
                wsl_pre_manifest = install_mod.read_manifest(to_dir)
                wsl_pre_status, _ = install_mod.status(to_dir=to_dir, wrapper_path=wrapper_path)
                wsl_had_install = wsl_pre_manifest is not None
                wsl_had_legacy = (
                    wsl_pre_manifest is None
                    and (to_dir / "helio").is_file()
                    and not (to_dir / "helio").is_symlink()
                    and (to_dir / "helio.exe").is_file()
                    and not (to_dir / "helio.exe").is_symlink()
                )
                wsl_pre_cur = (wsl_pre_manifest or {}).get("current") or {}
                wsl_pre_helio_sha = (
                    wsl_pre_cur.get("helio_sha256", "")
                    if wsl_had_install
                    else (install_mod.sha256_file(to_dir / "helio") if wsl_had_legacy else "")
                )

                wsl_already_current = (
                    wsl_had_install
                    and wsl_pre_status == 0
                    and wsl_pre_cur.get("commit") == commit
                    and not bool(wsl_pre_cur.get("dirty", False))
                    and wsl_pre_cur.get("helio_sha256") == pre_wsl_sha
                    and wsl_pre_cur.get("helio_exe_sha256") == pre_win_sha
                    and wsl_pre_cur.get("store") == str(store_dir)
                    and wsl_pre_cur.get("wrapper") == str(wrapper_path)
                )

                win_pre = _probe_win_state(python_exe, win_install_py_win, win_to_raw, reg_key, install_mod=install_mod)
                win_dest = win_pre["dest"]
                win_pre_manifest = win_pre["manifest"]
                win_had_install = win_pre_manifest is not None
                win_pre_cur = (win_pre_manifest or {}).get("current") or {}

                win_already_current = (
                    win_had_install
                    and win_pre["status_code"] == 0
                    and win_pre_cur.get("commit") == commit
                    and not bool(win_pre_cur.get("dirty", False))
                    and win_pre_cur.get("helio_exe_sha256") == pre_win_sha
                    and win_pre_cur.get("store") == win_store
                )

                journal.update(
                    {
                        "phase_state": "planned",
                        "win_dest": win_dest,
                        "wsl_had_install": wsl_had_install,
                        "wsl_had_legacy": wsl_had_legacy,
                        "wsl_pre_helio_sha": wsl_pre_helio_sha,
                        "wsl_pre_commit": wsl_pre_cur.get("commit", "legacy" if wsl_had_legacy else ""),
                        "wsl_pre_store": wsl_pre_cur.get("store", ""),
                        "wsl_pre_wrapper": wsl_pre_cur.get("wrapper", ""),
                        "win_had_install": win_had_install,
                        "win_pre_exe_sha": win_pre.get("exe_sha", ""),
                        "win_pre_commit": win_pre_cur.get("commit", ""),
                        "win_pre_store": win_pre_cur.get("store", ""),
                    }
                )
                install_mod.write_json_atomic(to_dir / PHASE_JOURNAL_NAME, journal, tx_id=str(journal.get("tx_id") or "") or None)
                if on_boundary:
                    on_boundary("after_phase_journal")

                if not wsl_already_current:
                    if wsl_had_install:
                        wsl_pre_prev = (wsl_pre_manifest or {}).get("previous")
                        if isinstance(wsl_pre_prev, dict):
                            b64_h, b64_e = _capture_wsl_previous_b64(to_dir, wsl_pre_prev, install_mod)
                            journal["wsl_pre_previous"] = wsl_pre_prev
                            journal["wsl_pre_prev_helio_b64"] = b64_h
                            journal["wsl_pre_prev_helio_exe_b64"] = b64_e
                        else:
                            journal["wsl_pre_previous"] = None
                    journal["wsl_changed"] = True
                    install_mod.write_json_atomic(to_dir / PHASE_JOURNAL_NAME, journal, tx_id=str(journal.get("tx_id") or "") or None)
                    install_mod.install(
                        repo=repo,
                        to_dir=to_dir,
                        store_dir=store_dir,
                        wrapper_path=wrapper_path,
                        sdk_url=sdk_url,
                        prebuilt=(snap_wsl, snap_win),
                        commit_override=commit,
                        dirty_override=False,
                        allow_phase_journal=True,
                    )
                    journal["phase_state"] = "wsl_done"
                    install_mod.write_json_atomic(to_dir / PHASE_JOURNAL_NAME, journal, tx_id=str(journal.get("tx_id") or "") or None)
                    if on_boundary:
                        on_boundary("after_wsl_install")

                if not win_already_current:
                    if win_had_install:
                        win_pre_prev = (win_pre_manifest or {}).get("previous")
                        if isinstance(win_pre_prev, dict):
                            # Read-only capture of Windows bin\helio.exe.old stored inside phase.journal.json (zero separate backup files on either host!).
                            b64_raw = _win_phase_marker_call(
                                python_exe,
                                win_install_py_win,
                                "snapshot_prev",
                                win_to_raw,
                                reg_key,
                                tx_id,
                                install_mod=install_mod,
                            ).strip()
                            if not b64_raw:
                                raise install_mod.InstallError("missing Windows previous generation binary during snapshot", code=3)
                            raw_prev = base64.b64decode(b64_raw.encode("ascii"))
                            exp_prev_sha = str(win_pre_prev.get("helio_exe_sha256") or "")
                            if exp_prev_sha and hashlib.sha256(raw_prev).hexdigest() != exp_prev_sha:
                                raise install_mod.InstallError("Windows previous generation binary hash mismatch during snapshot", code=3)
                            journal["win_pre_previous"] = win_pre_prev
                            journal["win_pre_prev_exe_b64"] = b64_raw
                            install_mod.write_json_atomic(to_dir / PHASE_JOURNAL_NAME, journal, tx_id=str(journal.get("tx_id") or "") or None)
                        else:
                            journal["win_pre_previous"] = None
                    journal["win_changed"] = True
                    install_mod.write_json_atomic(to_dir / PHASE_JOURNAL_NAME, journal, tx_id=str(journal.get("tx_id") or "") or None)
                    win_snap_exe = win_mod.wsl_to_windows_path(snap_win)
                    win_args = [
                        "--source-exe",
                        win_snap_exe,
                        "--source-sha256",
                        pre_win_sha,
                        "--commit",
                        commit,
                        "--store",
                        win_store,
                        "--reg-key",
                        reg_key,
                        "--phase-tx",
                        tx_id,
                    ]
                    if win_to_raw:
                        win_args.extend(["--to", win_to_raw])
                    wres = _run_win_cli(python_exe, install_py_win, win_args)
                    if wres.returncode != 0:
                        raise install_mod.InstallError(
                            f"Windows phase install failed (exit {wres.returncode}): {(wres.stderr or wres.stdout).strip()}",
                            code=3,
                        )
                    journal["phase_state"] = "both_done"
                    install_mod.write_json_atomic(to_dir / PHASE_JOURNAL_NAME, journal, tx_id=str(journal.get("tx_id") or "") or None)
                    if on_boundary:
                        on_boundary("after_win_install")

                _verify_live_installations(
                    to_dir=to_dir,
                    wrapper_path=wrapper_path,
                    store_dir=store_dir,
                    win_to_raw=win_to_raw,
                    reg_key=reg_key,
                    commit=commit,
                    proved_wsl_sha=pre_wsl_sha,
                    proved_win_sha=pre_win_sha,
                    python_exe=python_exe,
                    win_install_py_win=win_install_py_win,
                    install_mod=install_mod,
                    win_mod=win_mod,
                    on_verify=on_verify,
                )

                # Durable commit point: write `phase_state = "notifying"` FIRST while `.phase-active.json` is STILL held
                # so rollback is permanently retired before any post-commit cleanup or marker release!
                journal["phase_state"] = "notifying"
                install_mod.write_json_atomic(to_dir / PHASE_JOURNAL_NAME, journal, tx_id=str(journal.get("tx_id") or "") or None)
            except BaseException as exc:
                _rollback_phase_journal(
                    journal,
                    python_exe=python_exe,
                    install_py_win=install_py_win,
                    win_install_py_win=win_install_py_win,
                    install_mod=install_mod,
                    on_boundary=on_boundary,
                )
                raise install_mod.InstallError(f"phase install failed and rolled back: {exc}", code=3) from exc

            # Post-commit marker release lives OUTSIDE the rollback try/except so a failure here
            # never re-enters rollback; retrying resumes cleanly from `phase_state == "notifying"`.
            _win_phase_marker_call(python_exe, win_install_py_win, "end", win_to_raw, reg_key, tx_id, install_mod=install_mod)

            # Post-verification notification: if this fails, keep `phase_state == "notifying"` so retry only re-sends.
            msg_id = _send_phase_tell(
                wrapper_path=wrapper_path,
                store_dir=store_dir,
                phase=phase,
                commit=commit,
                to_dir=to_dir,
                win_dest=win_dest,
                install_mod=install_mod,
                on_boundary=on_boundary,
            )
            _sweep_phase_journal_temps(to_dir, tx_id, install_mod)
            jp = to_dir / PHASE_JOURNAL_NAME
            if jp.exists():
                jp.unlink()
                install_mod.fsync_dir(to_dir)

            return {
                "phase": phase,
                "commit": commit,
                "wsl_to": str(to_dir),
                "win_to": win_dest,
                "store": str(store_dir),
                "wsl_changed": bool(journal["wsl_changed"]),
                "win_changed": bool(journal["win_changed"]),
                "notice_id": msg_id,
                "resumed_notification": False,
            }
