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


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 1800,
) -> subprocess.CompletedProcess[str]:
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

    closed_found = False
    parked_found = False
    for rel in sorted(candidates):
        show = _git(repo, "show", f"{commit}:{rel}")
        if show.returncode != 0:
            continue
        lines = show.stdout.splitlines()
        last_closeout = -1
        last_parked = -1
        for idx, line in enumerate(lines):
            if re.match(r"^##\s+Closeout\b", line):
                last_closeout = idx
            if line.startswith("PARKED "):
                last_parked = idx
        if last_closeout >= 0 and last_closeout > last_parked:
            closed_found = True
        elif last_parked >= 0:
            parked_found = True

    if not closed_found:
        if parked_found:
            raise install_mod.InstallError(
                f"phase {phase} is PARKED in docs/phase-runs/*{suffix} and has no closeout after park",
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

    fetch = _git(repo, "fetch", "origin", "main")
    if fetch.returncode != 0:
        raise install_mod.InstallError(
            f"refusing phase install: cannot verify authoritative origin/main ({fetch.stderr.strip()})",
            code=2,
        )
    origin = _git(repo, "rev-parse", "origin/main")
    if origin.returncode != 0:
        raise install_mod.InstallError(
            f"refusing phase install: origin/main not found ({origin.stderr.strip()})",
            code=2,
        )
    origin_sha = origin.stdout.strip()
    if commit != origin_sha:
        raise install_mod.InstallError(
            f"refusing phase install: HEAD ({commit[:12]}) is not at origin/main ({origin_sha[:12]})",
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
        "print(json.dumps({'dest': str(dest), 'manifest': manifest, 'status_code': st_code, "
        "'status_lines': st_lines, 'exe_sha': exe_sha}))\n"
    )
    res = _run([python_exe, "-B", "-c", code, win_install_py_win, win_to_raw or "", reg_key], timeout=60)
    if res.returncode != 0:
        raise RuntimeError(f"Windows state probe failed (exit {res.returncode}): {(res.stderr or res.stdout).strip()}")
    return json.loads(res.stdout.strip().splitlines()[-1])


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
        "env['PATH'] = (user_path + ';' + sys_path) if user_path else sys_path\n"
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
) -> subprocess.CompletedProcess[str]:
    """Run bare `helio` in a fresh POSIX environment with HELIO_STORE unset and wrapper dir first on PATH."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("HELIO_STORE", "HELIO_DEFAULT_STORE", "HELIO_DEFAULT_STORE_HEX", "HELIO_AS")
    }
    base_path = env.get("PATH", "/usr/bin:/bin")
    env["PATH"] = f"{wrapper_path.parent}:{base_path}"
    return _run(
        ["sh", "-c", 'exec helio "$@"', "sh", *helio_args],
        env=env,
        cwd=Path("/tmp"),
        input_text=input_text,
        timeout=timeout,
    )


def _verify_live_installations(
    *,
    to_dir: Path,
    wrapper_path: Path,
    store_dir: Path,
    win_to_raw: str | None,
    reg_key: str,
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

    # 1. Verify WSL live status and exact binary digests.
    wsl_code, wsl_lines = install_mod.status(to_dir=to_dir, wrapper_path=wrapper_path)
    if wsl_code != 0:
        raise install_mod.InstallError(f"post-install WSL status unhealthy (exit {wsl_code}): {'; '.join(wsl_lines)}", code=3)
    live_wsl_sha = install_mod.sha256_file(to_dir / "helio")
    live_wsl_exe_sha = install_mod.sha256_file(to_dir / "helio.exe")
    if live_wsl_sha != proved_wsl_sha or live_wsl_exe_sha != proved_win_sha:
        raise install_mod.InstallError("post-install WSL binaries do not match proved SHA-256 digests", code=3)

    # 2. Verify Windows live status (includes PATH shadowing check) and exact binary digest.
    win_state = _probe_win_state(python_exe, win_install_py_win, win_to_raw, reg_key)
    if win_state["status_code"] != 0:
        raise install_mod.InstallError(
            f"post-install Windows status unhealthy (exit {win_state['status_code']}): {'; '.join(win_state['status_lines'])}",
            code=3,
        )
    if win_state["exe_sha"] != proved_win_sha:
        raise install_mod.InstallError("post-install Windows helio.exe does not match proved SHA-256 digest", code=3)

    if on_verify:
        on_verify("before_real_store_check")

    # 3. Clean-environment `helio check` on the REAL store from both WSL and Windows entry points.
    wsl_real = _run_clean_wsl_helio(wrapper_path, ["check"])
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
    install_mod.write_json_atomic(to_dir / PHASE_JOURNAL_NAME, journal)
    if on_boundary:
        on_boundary("after_rollback_journal")

    errors: list[str] = []

    if journal.get("win_changed"):
        try:
            cur_win = _probe_win_state(python_exe, win_install_py_win, win_to_raw, reg_key)
        except Exception as exc:
            errors.append(f"Windows rollback probe failed: {exc}")
            cur_win = None
        if cur_win is not None:
            cur_win_m = cur_win.get("manifest") or {}
            cur_win_c = cur_win_m.get("current") or {}
            win_already_restored = (
                (
                    cur_win.get("exe_sha") == journal.get("win_pre_exe_sha", "")
                    and cur_win_c.get("commit") == journal.get("win_pre_commit", "")
                    and cur_win_c.get("store") == journal.get("win_pre_store", "")
                )
                if journal.get("win_had_install")
                else (cur_win.get("manifest") is None and not cur_win.get("exe_sha"))
            )
            if not win_already_restored:
                win_args = ["--back" if journal.get("win_had_install") else "--remove", "--reg-key", reg_key]
                if win_to_raw:
                    win_args.extend(["--to", win_to_raw])
                wres = _run_win_cli(python_exe, install_py_win, win_args)
                if wres.returncode != 0:
                    errors.append(f"Windows rollback failed (exit {wres.returncode}): {(wres.stderr or wres.stdout).strip()}")
                else:
                    try:
                        post_win = _probe_win_state(python_exe, win_install_py_win, win_to_raw, reg_key)
                        if journal.get("win_had_install"):
                            if post_win["exe_sha"] != journal.get("win_pre_exe_sha", ""):
                                errors.append("Windows rollback hash does not match pre-transaction binary")
                        else:
                            if post_win["manifest"] is not None:
                                errors.append("Windows rollback did not remove newly installed manifest")
                    except Exception as exc:
                        errors.append(f"Windows rollback verification failed: {exc}")
        if on_boundary:
            on_boundary("after_win_rollback")

    if journal.get("wsl_changed"):
        try:
            cur_wsl_m = install_mod.read_manifest(to_dir)
            cur_wsl_c = (cur_wsl_m or {}).get("current") or {}
            cur_wsl_sha = install_mod.sha256_file(to_dir / "helio") if (to_dir / "helio").is_file() else ""
            wsl_already_restored = (
                (
                    cur_wsl_sha == journal.get("wsl_pre_helio_sha", "")
                    and cur_wsl_c.get("commit") == journal.get("wsl_pre_commit", "")
                    and cur_wsl_c.get("store") == journal.get("wsl_pre_store", "")
                    and cur_wsl_c.get("wrapper") == journal.get("wsl_pre_wrapper", "")
                )
                if journal.get("wsl_had_install")
                else (cur_wsl_m is None and not (to_dir / "helio").exists())
            )
            if not wsl_already_restored:
                if journal.get("wsl_had_install"):
                    install_mod.go_back(to_dir=to_dir, wrapper_path=wrapper_path)
                    live_sha = install_mod.sha256_file(to_dir / "helio")
                    if live_sha != journal.get("wsl_pre_helio_sha", ""):
                        errors.append("WSL rollback hash does not match pre-transaction binary")
                else:
                    install_mod.remove(to_dir=to_dir, wrapper_path=wrapper_path)
                    if install_mod.read_manifest(to_dir) is not None:
                        errors.append("WSL rollback did not remove newly installed manifest")
        except Exception as exc:
            errors.append(f"WSL rollback failed: {exc}")
        if on_boundary:
            on_boundary("after_wsl_rollback")

    if errors:
        raise install_mod.InstallError("phase rollback incomplete: " + "; ".join(errors), code=3)

    jp = to_dir / PHASE_JOURNAL_NAME
    if jp.exists():
        jp.unlink()
        install_mod.fsync_dir(to_dir)


def _recover_pending_phase_journal(
    *,
    to_dir: Path,
    python_exe: str,
    install_py_win: str,
    win_install_py_win: str,
    install_mod: Any,
    on_boundary: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    """Recover an existing phase.journal.json. Returns notification result if a `notifying` journal was completed."""
    jp = to_dir / PHASE_JOURNAL_NAME
    if not jp.exists():
        return None
    try:
        journal = json.loads(jp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise install_mod.InstallError(f"corrupt phase journal at {jp}: {exc}", code=3) from exc

    state = journal.get("phase_state")
    if state == "notifying":
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
        jp.unlink()
        install_mod.fsync_dir(to_dir)
        return {
            "phase": int(journal["phase"]),
            "commit": str(journal["commit"]),
            "wsl_to": str(journal["wsl_to"]),
            "win_to": str(journal["win_dest"]),
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

    # First: if a previous `--phase` run left a durable journal in to_dir, acquire the phase lock and recover it.
    if (to_dir / PHASE_JOURNAL_NAME).exists():
        with install_mod.acquire_lock(to_dir / PHASE_LOCK_NAME, blocking=False):
            resumed = _recover_pending_phase_journal(
                to_dir=to_dir,
                python_exe=python_exe,
                install_py_win=install_py_win,
                win_install_py_win=win_install_py_win,
                install_mod=install_mod,
                on_boundary=on_boundary,
            )
            if resumed is not None:
                # If the resumed notification matches the requested phase and current HEAD, we are done without reinstalling!
                head_now = _git(repo, "rev-parse", "HEAD")
                if head_now.returncode == 0 and resumed["phase"] == phase and resumed["commit"] == head_now.stdout.strip():
                    return resumed

    # Pre-flight gates (all read-only; never touch installed destinations on refusal).
    commit = _check_clean_and_origin_main(repo, install_mod)
    _validate_phase_verified_and_closed(repo, phase, commit, install_mod)
    sdk_dir = _find_sdk_dir(repo, sdk_url, install_mod)

    # Gate 3: run app_build.py in WSL and verify the Windows-vs-WSL race test ran and passed.
    build_res = _run([sys.executable, str(sdk_dir / "tools" / "app_build.py"), str(repo)], cwd=repo, timeout=1800)
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
        if install_mod.sha256_file(snap_wsl) != pre_wsl_sha or install_mod.sha256_file(snap_win) != pre_win_sha:
            raise install_mod.InstallError("refusing phase install: snapshot digest mismatch", code=3)

        if on_after_pre_proof_hash:
            on_after_pre_proof_hash(build_wsl_bin, build_win_bin)

        # Run prove.py on the checkout.
        prove_res = _run([sys.executable, str(sdk_dir / "tools" / "prove.py"), str(repo)], cwd=repo, timeout=3600)
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
            # Capture pre-transaction state for both hosts.
            wsl_pre_manifest = install_mod.read_manifest(to_dir)
            wsl_pre_status, _ = install_mod.status(to_dir=to_dir, wrapper_path=wrapper_path)
            wsl_had_install = wsl_pre_manifest is not None
            wsl_pre_cur = (wsl_pre_manifest or {}).get("current") or {}

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

            win_pre = _probe_win_state(python_exe, win_install_py_win, win_to_raw, reg_key)
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

            journal: dict[str, Any] = {
                "schema_version": 1,
                "tx_id": secrets.token_hex(6),
                "phase": phase,
                "commit": commit,
                "phase_state": "planned",
                "wsl_to": str(to_dir),
                "wrapper": str(wrapper_path),
                "store": str(store_dir),
                "win_to_arg": win_to_raw,
                "win_dest": win_dest,
                "reg_key": reg_key,
                "proved_wsl_sha": pre_wsl_sha,
                "proved_win_sha": pre_win_sha,
                "wsl_had_install": wsl_had_install,
                "wsl_pre_helio_sha": wsl_pre_cur.get("helio_sha256", ""),
                "wsl_pre_commit": wsl_pre_cur.get("commit", ""),
                "wsl_pre_store": wsl_pre_cur.get("store", ""),
                "wsl_pre_wrapper": wsl_pre_cur.get("wrapper", ""),
                "win_had_install": win_had_install,
                "win_pre_exe_sha": win_pre.get("exe_sha", ""),
                "win_pre_commit": win_pre_cur.get("commit", ""),
                "win_pre_store": win_pre_cur.get("store", ""),
                "wsl_changed": False,
                "win_changed": False,
            }
            install_mod.write_json_atomic(to_dir / PHASE_JOURNAL_NAME, journal)
            if on_boundary:
                on_boundary("after_phase_journal")

            try:
                if not wsl_already_current:
                    journal["wsl_changed"] = True
                    install_mod.write_json_atomic(to_dir / PHASE_JOURNAL_NAME, journal)
                    install_mod.install(
                        repo=repo,
                        to_dir=to_dir,
                        store_dir=store_dir,
                        wrapper_path=wrapper_path,
                        sdk_url=sdk_url,
                        prebuilt=(snap_wsl, snap_win),
                        commit_override=commit,
                        dirty_override=False,
                    )
                    journal["phase_state"] = "wsl_done"
                    install_mod.write_json_atomic(to_dir / PHASE_JOURNAL_NAME, journal)
                    if on_boundary:
                        on_boundary("after_wsl_install")

                if not win_already_current:
                    journal["win_changed"] = True
                    install_mod.write_json_atomic(to_dir / PHASE_JOURNAL_NAME, journal)
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
                    install_mod.write_json_atomic(to_dir / PHASE_JOURNAL_NAME, journal)
                    if on_boundary:
                        on_boundary("after_win_install")

                _verify_live_installations(
                    to_dir=to_dir,
                    wrapper_path=wrapper_path,
                    store_dir=store_dir,
                    win_to_raw=win_to_raw,
                    reg_key=reg_key,
                    proved_wsl_sha=pre_wsl_sha,
                    proved_win_sha=pre_win_sha,
                    python_exe=python_exe,
                    win_install_py_win=win_install_py_win,
                    install_mod=install_mod,
                    win_mod=win_mod,
                    on_verify=on_verify,
                )

                journal["phase_state"] = "notifying"
                install_mod.write_json_atomic(to_dir / PHASE_JOURNAL_NAME, journal)
            except BaseException as exc:
                _rollback_phase_journal(
                    journal,
                    python_exe=python_exe,
                    install_py_win=install_py_win,
                    win_install_py_win=win_install_py_win,
                    install_mod=install_mod,
                    on_boundary=on_boundary,
                )
                if isinstance(exc, install_mod.InstallError):
                    raise
                raise install_mod.InstallError(f"phase install failed and rolled back: {exc}", code=3) from exc

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
