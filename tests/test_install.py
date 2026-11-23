import concurrent.futures
import errno
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO / "tools" / "install.py"


def load_installer():
    spec = importlib.util.spec_from_file_location("helio_install", INSTALL_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def find_built_binaries() -> tuple[Path, Path]:
    build_dir = Path(os.environ.get("IMPOSSIBLE_BUILD") or (REPO / "build" / "linux"))
    helio = build_dir / "linux" / "helio"
    helio_exe = build_dir / "helio.exe"
    return helio, helio_exe


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_distinct_binary(src: Path, dst: Path, tag: bytes) -> None:
    """Copy a real executable and append a trailing marker so its SHA-256 is distinct while staying runnable."""
    data = src.read_bytes() + b"\n#HELIO_TEST_TAG=" + tag + b"\n"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)
    if os.name == "posix":
        os.chmod(dst, 0o755)


def init_git_repo(path: Path, message: str = "init") -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.txt").write_text("fixture\n", encoding="utf-8")
    (path / ".gitignore").write_text("build/\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=path, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True).stdout.strip()


def make_fixture_sdk_bare_repo(root: Path, helio_src: Path, exe_src: Path) -> Path:
    """Create a local bare git repo acting as ImpossibleSDK with a nonrecursive tools/app_build.py."""
    work = root / "sdk-work"
    work.mkdir(parents=True, exist_ok=True)
    tools = work / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    script = f"""#!/usr/bin/env python3
import os, shutil, sys
from pathlib import Path
repo = Path(sys.argv[1]).resolve()
if (repo / "FAIL_TEST").exists():
    print("simulated test failure", file=sys.stderr)
    raise SystemExit(1)
out_linux = repo / "build" / "linux" / "linux"
out_linux.mkdir(parents=True, exist_ok=True)
tag = (repo / "VERSION").read_bytes() if (repo / "VERSION").exists() else b"v1"
src_h = Path({str(helio_src)!r}).read_bytes() + b"\\n#TAG=" + tag + b"\\n"
src_e = Path({str(exe_src)!r}).read_bytes() + b"\\n#TAG=" + tag + b"\\n"
h_dst = out_linux / "helio"
e_dst = repo / "build" / "linux" / "helio.exe"
h_dst.write_bytes(src_h)
os.chmod(h_dst, 0o755)
e_dst.write_bytes(src_e)
os.chmod(e_dst, 0o755)
"""
    (tools / "app_build.py").write_text(script, encoding="utf-8")
    os.chmod(tools / "app_build.py", 0o755)
    init_git_repo(work, "fixture sdk")
    bare = root / "ImpossibleSDK.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(work), str(bare)], check=True)
    return bare


class SimulatedCrash(RuntimeError):
    _leave_journal_for_test = True


@unittest.skipUnless(os.name == "posix", "WSL installer tests run on Linux/WSL")
class TestQuickInstaller(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = load_installer()
        cls.helio_bin, cls.helio_exe = find_built_binaries()
        if not cls.helio_bin.is_file() or not cls.helio_exe.is_file():
            raise unittest.SkipTest(f"built binaries not found at {cls.helio_bin} and {cls.helio_exe}")

    def _run_cli(self, *args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = {**os.environ}
        env.pop("HELIO_STORE", None)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(INSTALL_SCRIPT), *args],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_readme_documents_quick_start_and_subcommands(self) -> None:
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("python3 Heliograph/tools/install.py", readme)
        for flag in ("--status", "--back", "--remove", "--to"):
            self.assertIn(flag, readme)

    def test_missing_sdk_clone_build_install_and_failed_build_preserves_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare_sdk = make_fixture_sdk_bare_repo(root, self.helio_bin, self.helio_exe)
            ws = root / "workspace"
            fixture_repo = ws / "Heliograph"
            (fixture_repo).mkdir(parents=True)
            (fixture_repo / "VERSION").write_text("v1", encoding="utf-8")
            commit1 = init_git_repo(fixture_repo, "v1")

            home = root / "home"
            home.mkdir()
            env = {
                "HOME": str(home),
                "HELIO_REPO": str(fixture_repo),
                "HELIO_SDK_URL": str(bare_sdk),
            }

            # 1. Missing ../ImpossibleSDK is cloned, built, tested and installed.
            res = self._run_cli(env_extra=env)
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
            self.assertTrue((ws / "ImpossibleSDK" / "tools" / "app_build.py").is_file())
            to_dir = ws / "bin"
            store_dir = ws / "Heliograph-signals"
            wrapper = home / ".local" / "bin" / "helio"
            self.assertTrue((to_dir / "helio").is_file())
            self.assertTrue((to_dir / "helio.exe").is_file())
            self.assertTrue((to_dir / "installed.json").is_file())
            self.assertTrue(store_dir.is_dir())
            self.assertTrue(wrapper.is_file())

            manifest = json.loads((to_dir / "installed.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["current"]["commit"], commit1)
            self.assertFalse(manifest["current"]["dirty"])
            self.assertIsNone(manifest["previous"])
            old_sha = self.mod.sha256_file(to_dir / "helio")

            # 2. Failed build/test installs nothing and preserves existing installation.
            (fixture_repo / "FAIL_TEST").write_text("1", encoding="utf-8")
            (fixture_repo / "VERSION").write_text("v2-broken", encoding="utf-8")
            fail = self._run_cli(env_extra=env)
            self.assertNotEqual(fail.returncode, 0)
            self.assertEqual(self.mod.sha256_file(to_dir / "helio"), old_sha)
            st = self._run_cli("--status", env_extra=env)
            self.assertEqual(st.returncode, 0, st.stdout + st.stderr)
            self.assertIn(commit1, st.stdout)

    def test_wrapper_store_precedence_spaces_fresh_shell_and_existing_store_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            init_git_repo(repo)
            to_dir = root / "my bin dir"
            default_store = root / "my default store" / "Heliograph-signals"
            env_store = root / "my env store"
            cli_store = root / "my cli store"
            home = root / "my home"
            wrapper = home / ".local" / "bin" / "helio"

            self.mod.install(
                repo=repo,
                to_dir=to_dir,
                store_dir=default_store,
                wrapper_path=wrapper,
                prebuilt=(self.helio_bin, self.helio_exe),
            )

            # Initialize the two override stores using the installed wrapper in a fresh shell.
            shell_env = {
                "HOME": str(home),
                "PATH": f"{wrapper.parent}:/usr/bin:/bin",
            }
            for s in (env_store, cli_store):
                r = subprocess.run(
                    ["/bin/sh", "-c", 'helio init --store "$1"', "_", str(s)],
                    env=shell_env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r.returncode, 0, r.stderr)

            # Send distinct messages to default, HELIO_STORE, and --store.
            def send_and_list(cmd: str, extra_env: dict[str, str] | None = None) -> tuple[str, str]:
                e = {**shell_env, **(extra_env or {})}
                sent = subprocess.run(
                    ["/bin/sh", "-c", cmd],
                    input="hello body\n",
                    env=e,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(sent.returncode, 0, sent.stderr + sent.stdout)
                msg_id = sent.stdout.strip()
                self.assertTrue(msg_id)
                shown = subprocess.run(
                    ["/bin/sh", "-c", f"helio show {msg_id} --as proj/a" + ("" if "--store" not in cmd else f' --store "{cli_store}"')],
                    env=e,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(shown.returncode, 0, shown.stderr + shown.stdout)
                return msg_id, shown.stdout

            id_def, out_def = send_and_list('helio tell proj/a "default-subject" --as proj/b')
            self.assertIn("default-subject", out_def)

            id_env, out_env = send_and_list(
                'helio tell proj/a "env-subject" --as proj/b',
                extra_env={"HELIO_STORE": str(env_store)},
            )
            self.assertIn("env-subject", out_env)

            # --store overrides HELIO_STORE.
            id_cli, out_cli = send_and_list(
                f'helio tell proj/a "cli-subject" --as proj/b --store "{cli_store}"',
                extra_env={"HELIO_STORE": str(env_store)},
            )
            self.assertIn("cli-subject", out_cli)

            # Verify isolation: default store does not contain env/cli messages.
            miss = subprocess.run(
                ["/bin/sh", "-c", f"helio show {id_cli} --as proj/a"],
                env=shell_env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(miss.returncode, 2)

            # Existing store keeps every byte on reinstall.
            sentinel = default_store / "messages" / "sentinel.keep"
            sentinel.write_bytes(b"keep-every-byte\x00\xff")
            self.mod.install(
                repo=repo,
                to_dir=to_dir,
                store_dir=default_store,
                wrapper_path=wrapper,
                prebuilt=(self.helio_bin, self.helio_exe),
            )
            self.assertEqual(sentinel.read_bytes(), b"keep-every-byte\x00\xff")
            still_there = subprocess.run(
                ["/bin/sh", "-c", f"helio show {id_def} --as proj/a"],
                env=shell_env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(still_there.returncode, 0)
            self.assertIn("default-subject", still_there.stdout)

    def test_status_dirty_back_corruption_and_idempotent_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            commit_a = init_git_repo(repo, "commit A")
            to_dir = root / "bin"
            store_dir = root / "store"
            wrapper = root / "home" / ".local" / "bin" / "helio"

            # Absent status -> exit 1.
            code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(code, 1)

            # Install A (clean).
            a_h = root / "a_helio"
            a_e = root / "a_helio.exe"
            make_distinct_binary(self.helio_bin, a_h, b"A")
            make_distinct_binary(self.helio_exe, a_e, b"A")
            self.mod.install(repo=repo, to_dir=to_dir, store_dir=store_dir, wrapper_path=wrapper, prebuilt=(a_h, a_e))

            code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(code, 0)
            self.assertIn(commit_a, "\n".join(lines))
            self.assertNotIn("(dirty)", "\n".join(lines))

            # First-install --back refuses and leaves live pair intact.
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.go_back(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(ctx.exception.code, 2)
            self.assertEqual(self.mod.sha256_file(to_dir / "helio"), self.mod.sha256_file(a_h))

            # Modify tracked file so working tree is dirty, then install B.
            (repo / "README.txt").write_text("dirty change\n", encoding="utf-8")
            b_h = root / "b_helio"
            b_e = root / "b_helio.exe"
            make_distinct_binary(self.helio_bin, b_h, b"B")
            make_distinct_binary(self.helio_exe, b_e, b"B")
            self.mod.install(repo=repo, to_dir=to_dir, store_dir=store_dir, wrapper_path=wrapper, prebuilt=(b_h, b_e))

            code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(code, 0)
            self.assertIn(f"{commit_a} (dirty)", "\n".join(lines))

            # Tamper with live helio -> status reports mismatch (exit 3) naming helio.
            saved_b_bytes = (to_dir / "helio").read_bytes()
            (to_dir / "helio").write_bytes(saved_b_bytes + b"X")
            code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(code, 3)
            self.assertIn("helio", "\n".join(lines))
            (to_dir / "helio").write_bytes(saved_b_bytes)

            # Delete live helio.exe -> status reports missing (exit 3) naming helio.exe.
            saved_be_bytes = (to_dir / "helio.exe").read_bytes()
            (to_dir / "helio.exe").unlink()
            code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(code, 3)
            self.assertIn("helio.exe", "\n".join(lines))
            (to_dir / "helio.exe").write_bytes(saved_be_bytes)

            # Corrupt backup helio.old -> --back refuses and leaves B intact.
            saved_old_bytes = (to_dir / "helio.old").read_bytes()
            (to_dir / "helio.old").write_bytes(b"corrupt")
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.go_back(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(ctx.exception.code, 2)
            self.assertEqual(self.mod.sha256_file(to_dir / "helio"), self.mod.sha256_file(b_h))
            (to_dir / "helio.old").write_bytes(saved_old_bytes)

            # Valid --back restores A and updates manifest so --status reports clean A.
            self.mod.go_back(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(self.mod.sha256_file(to_dir / "helio"), self.mod.sha256_file(a_h))
            self.assertEqual(self.mod.sha256_file(to_dir / "helio.exe"), self.mod.sha256_file(a_e))
            code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(code, 0)
            self.assertNotIn("(dirty)", "\n".join(lines))

            # Place an unrelated file in bin and verify --remove keeps it and the store, and is idempotent.
            unrelated = to_dir / "other-tool"
            unrelated.write_text("do not delete\n", encoding="utf-8")
            self.assertTrue(self.mod.remove(to_dir=to_dir, wrapper_path=wrapper))
            self.assertFalse((to_dir / "helio").exists())
            self.assertFalse((to_dir / "helio.exe").exists())
            self.assertFalse((to_dir / "helio.old").exists())
            self.assertFalse((to_dir / "installed.json").exists())
            self.assertFalse(wrapper.exists())
            self.assertTrue(unrelated.is_file())
            self.assertTrue(store_dir.is_dir())
            self.assertFalse(self.mod.remove(to_dir=to_dir, wrapper_path=wrapper))
            code, _ = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(code, 1)

    def test_legacy_adoption_and_reserved_name_collision_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            init_git_repo(repo)
            to_dir = root / "bin"
            to_dir.mkdir()
            store_dir = root / "store"
            wrapper = root / "home" / ".local" / "bin" / "helio"
            wrapper.parent.mkdir(parents=True)

            # Seed legacy manual installation without installed.json.
            make_distinct_binary(self.helio_bin, to_dir / "helio", b"legacy")
            make_distinct_binary(self.helio_exe, to_dir / "helio.exe", b"legacy")
            make_distinct_binary(self.helio_bin, to_dir / "helio.old", b"older")
            make_distinct_binary(self.helio_exe, to_dir / "helio.exe.old", b"older")
            legacy_sha = self.mod.sha256_file(to_dir / "helio")
            wrapper.write_text(
                f"#!/bin/sh\n# Heliograph with this machine's store. HELIO_STORE or --store overrides it.\n"
                f'HELIO_STORE="${{HELIO_STORE:-{store_dir}}}" exec "{to_dir}/helio" "$@"\n',
                encoding="utf-8",
            )
            os.chmod(wrapper, 0o755)

            new_h = root / "new_h"
            new_e = root / "new_e"
            make_distinct_binary(self.helio_bin, new_h, b"new")
            make_distinct_binary(self.helio_exe, new_e, b"new")
            manifest = self.mod.install(repo=repo, to_dir=to_dir, store_dir=store_dir, wrapper_path=wrapper, prebuilt=(new_h, new_e))
            self.assertEqual(manifest["previous"]["commit"], "legacy")
            self.assertEqual(manifest["previous"]["helio_sha256"], legacy_sha)

            # --back restores the adopted legacy pair.
            self.mod.go_back(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(self.mod.sha256_file(to_dir / "helio"), legacy_sha)

            # Now test unrelated file at every reserved name refuses before mutating anything.
            for reserved in self.mod.RESERVED_NAMES:
                bad_dir = root / f"bad_{reserved}"
                bad_dir.mkdir()
                (bad_dir / reserved).write_text("unrelated user content\n", encoding="utf-8")
                bad_wrap = root / f"wrap_{reserved}" / "helio"
                with self.assertRaises(self.mod.InstallError) as ctx:
                    self.mod.install(repo=repo, to_dir=bad_dir, store_dir=store_dir, wrapper_path=bad_wrap, prebuilt=(new_h, new_e))
                self.assertEqual(ctx.exception.code, 2)
                self.assertEqual((bad_dir / reserved).read_text(encoding="utf-8"), "unrelated user content\n")
                self.assertFalse(bad_wrap.exists())

            # Unowned wrapper refuses and is named.
            ok_dir = root / "ok_dir"
            foreign_wrap = root / "foreign" / "helio"
            foreign_wrap.parent.mkdir(parents=True)
            foreign_wrap.write_text("#!/bin/sh\necho my own script\n", encoding="utf-8")
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=repo, to_dir=ok_dir, store_dir=store_dir, wrapper_path=foreign_wrap, prebuilt=(new_h, new_e))
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn(str(foreign_wrap), str(ctx.exception))
            self.assertEqual(foreign_wrap.read_text(encoding="utf-8"), "#!/bin/sh\necho my own script\n")

    def test_locks_and_superseded_destination_does_not_remove_active_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            init_git_repo(repo)
            dir_a = root / "binA"
            dir_b = root / "binB"
            store = root / "store"
            wrapper = root / "home" / ".local" / "bin" / "helio"

            # Same-destination lock refuses concurrent run.
            dir_a.mkdir()
            with self.mod.acquire_lock(dir_a / self.mod.LOCK_NAME, blocking=False):
                with self.assertRaises(self.mod.InstallError) as ctx:
                    self.mod.install(repo=repo, to_dir=dir_a, store_dir=store, wrapper_path=wrapper, prebuilt=(self.helio_bin, self.helio_exe))
                self.assertEqual(ctx.exception.code, 2)
                self.assertFalse((dir_a / "helio").exists())

            # Install to A, then B; removing A must leave B's wrapper intact.
            self.mod.install(repo=repo, to_dir=dir_a, store_dir=store, wrapper_path=wrapper, prebuilt=(self.helio_bin, self.helio_exe))
            self.mod.install(repo=repo, to_dir=dir_b, store_dir=store, wrapper_path=wrapper, prebuilt=(self.helio_bin, self.helio_exe))
            self.assertTrue(self.mod.wrapper_belongs_to(wrapper, dir_b))

            self.mod.remove(to_dir=dir_a, wrapper_path=wrapper)
            self.assertTrue(wrapper.is_file())
            self.assertTrue(self.mod.wrapper_belongs_to(wrapper, dir_b))

            # Concurrent installs to A and B serialize on the shared wrapper lock cleanly.
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(
                    self.mod.install, repo=repo, to_dir=dir_a, store_dir=store, wrapper_path=wrapper, prebuilt=(self.helio_bin, self.helio_exe)
                )
                f2 = pool.submit(
                    self.mod.install, repo=repo, to_dir=dir_b, store_dir=store, wrapper_path=wrapper, prebuilt=(self.helio_bin, self.helio_exe)
                )
                f1.result()
                f2.result()
            self.assertTrue(self.mod.wrapper_belongs_to(wrapper, dir_a) or self.mod.wrapper_belongs_to(wrapper, dir_b))

    def test_crash_recovery_at_every_publication_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            init_git_repo(repo)
            store = root / "store"
            a_h, a_e = root / "a_h", root / "a_e"
            b_h, b_e = root / "b_h", root / "b_e"
            make_distinct_binary(self.helio_bin, a_h, b"A")
            make_distinct_binary(self.helio_exe, a_e, b"A")
            make_distinct_binary(self.helio_bin, b_h, b"B")
            make_distinct_binary(self.helio_exe, b_e, b"B")

            # Install crash boundaries.
            for boundary in ("after_journal", "after_backups", "after_helio", "after_helio_exe", "after_wrapper", "after_manifest"):
                to_dir = root / f"inst_{boundary}"
                wrapper = root / f"w_inst_{boundary}" / "helio"
                self.mod.install(repo=repo, to_dir=to_dir, store_dir=store, wrapper_path=wrapper, prebuilt=(a_h, a_e))

                def fail_at(stage: str, target=boundary) -> None:
                    if stage == target:
                        raise SimulatedCrash(stage)

                with self.assertRaises(SimulatedCrash):
                    self.mod.install(
                        repo=repo, to_dir=to_dir, store_dir=store, wrapper_path=wrapper, prebuilt=(b_h, b_e), on_boundary=fail_at
                    )
                self.assertTrue((to_dir / self.mod.JOURNAL_NAME).exists())
                # Recovery on next call restores consistent A (before manifest) or completes B (after manifest).
                self.assertTrue(self.mod.recover_if_needed(to_dir, wrapper))
                self.assertFalse((to_dir / self.mod.JOURNAL_NAME).exists())
                code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
                self.assertEqual(code, 0, f"{boundary}: {lines}")
                expected_h = b_h if boundary == "after_manifest" else a_h
                expected_e = b_e if boundary == "after_manifest" else a_e
                self.assertEqual(self.mod.sha256_file(to_dir / "helio"), self.mod.sha256_file(expected_h))
                self.assertEqual(self.mod.sha256_file(to_dir / "helio.exe"), self.mod.sha256_file(expected_e))

            # --back crash boundaries.
            for boundary in ("after_journal", "after_backups", "after_helio", "after_helio_exe", "after_manifest"):
                to_dir = root / f"back_{boundary}"
                wrapper = root / f"w_back_{boundary}" / "helio"
                self.mod.install(repo=repo, to_dir=to_dir, store_dir=store, wrapper_path=wrapper, prebuilt=(a_h, a_e))
                self.mod.install(repo=repo, to_dir=to_dir, store_dir=store, wrapper_path=wrapper, prebuilt=(b_h, b_e))

                def fail_at(stage: str, target=boundary) -> None:
                    if stage == target:
                        raise SimulatedCrash(stage)

                with self.assertRaises(SimulatedCrash):
                    self.mod.go_back(to_dir=to_dir, wrapper_path=wrapper, on_boundary=fail_at)
                self.assertTrue(self.mod.recover_if_needed(to_dir, wrapper))
                code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
                self.assertEqual(code, 0, f"back {boundary}: {lines}")
                expected_h = a_h if boundary == "after_manifest" else b_h
                self.assertEqual(self.mod.sha256_file(to_dir / "helio"), self.mod.sha256_file(expected_h))

            # --remove crash boundaries (forward recovery completes removal cleanly).
            for boundary in ("after_journal", "after_wrapper", "after_binaries"):
                to_dir = root / f"rem_{boundary}"
                wrapper = root / f"w_rem_{boundary}" / "helio"
                self.mod.install(repo=repo, to_dir=to_dir, store_dir=store, wrapper_path=wrapper, prebuilt=(a_h, a_e))

                def fail_at(stage: str, target=boundary) -> None:
                    if stage == target:
                        raise SimulatedCrash(stage)

                with self.assertRaises(SimulatedCrash):
                    self.mod.remove(to_dir=to_dir, wrapper_path=wrapper, on_boundary=fail_at)
                self.assertTrue(self.mod.recover_if_needed(to_dir, wrapper))
                self.assertFalse((to_dir / "helio").exists())
                self.assertFalse(wrapper.exists())
                code, _ = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
                self.assertEqual(code, 1)

    def test_zero_gap_replacement_busy_deferral_and_held_windows_exe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            init_git_repo(repo)
            to_dir = root / "bin"
            store = root / "store"
            wrapper = root / "home" / ".local" / "bin" / "helio"
            a_h, a_e = root / "a_h", root / "a_e"
            b_h, b_e = root / "b_h", root / "b_e"
            make_distinct_binary(self.helio_bin, a_h, b"A")
            make_distinct_binary(self.helio_exe, a_e, b"A")
            make_distinct_binary(self.helio_bin, b_h, b"B")
            make_distinct_binary(self.helio_exe, b_e, b"B")

            self.mod.install(repo=repo, to_dir=to_dir, store_dir=store, wrapper_path=wrapper, prebuilt=(a_h, a_e))

            # 1. Busy executable refusal rolls back without removing or breaking the existing live pair.
            real_replace = os.replace

            def busy_exe_replace(src, dst):
                if Path(dst).name == "helio.exe":
                    raise PermissionError(errno.EACCES, "busy", str(dst))
                return real_replace(src, dst)

            with mock.patch("os.replace", side_effect=busy_exe_replace):
                with self.assertRaises(self.mod.BusyExecutableError) as ctx:
                    self.mod.install(repo=repo, to_dir=to_dir, store_dir=store, wrapper_path=wrapper, prebuilt=(b_h, b_e))
            self.assertEqual(ctx.exception.code, 3)
            # Both live binaries are still A and callable.
            self.assertEqual(self.mod.sha256_file(to_dir / "helio"), self.mod.sha256_file(a_h))
            self.assertEqual(self.mod.sha256_file(to_dir / "helio.exe"), self.mod.sha256_file(a_e))
            code, _ = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(code, 0)

            # 2. Concurrent caller during live replacement gets old or new, never ENOENT/error.
            stop = threading.Event()
            failures: list[str] = []

            def caller():
                while not stop.is_set():
                    res = subprocess.run([str(to_dir / "helio"), "help"], capture_output=True, text=True)
                    if res.returncode != 0:
                        failures.append(f"rc={res.returncode} stderr={res.stderr}")

            t = threading.Thread(target=caller)
            t.start()
            try:
                for pair in ((b_h, b_e), (a_h, a_e), (b_h, b_e)):
                    self.mod.install(repo=repo, to_dir=to_dir, store_dir=store, wrapper_path=wrapper, prebuilt=pair)
            finally:
                stop.set()
                t.join(timeout=10)
            self.assertEqual(failures, [])

            # 3. Exercise a real held Windows helio.exe and a second Windows caller if WSL interop is available.
            if shutil.which("python.exe") and shutil.which("wslpath"):
                win_exe = subprocess.run(
                    ["wslpath", "-w", str(to_dir / "helio.exe")],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                # Hold the Windows executable open while replacing it and running a second Windows caller.
                holder = subprocess.Popen(
                    ["python.exe", "-c", f"import time; f = open({win_exe!r}, 'rb'); print('HELD', flush=True); time.sleep(5)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    assert holder.stdout is not None
                    ready = holder.stdout.readline().strip()
                    self.assertEqual(ready, "HELD")
                    self.mod.install(repo=repo, to_dir=to_dir, store_dir=store, wrapper_path=wrapper, prebuilt=(a_h, a_e))
                    second = subprocess.run(
                        [str(to_dir / "helio.exe"), "help"],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(second.returncode, 0, second.stderr)
                finally:
                    holder.terminate()
                    holder.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
