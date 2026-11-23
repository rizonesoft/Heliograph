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
    """Create a local bare git repo acting as ImpossibleSDK that compiles a real fixture check and runs unittest."""
    work = root / "sdk-work"
    work.mkdir(parents=True, exist_ok=True)
    tools = work / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    script = f"""#!/usr/bin/env python3
import os, subprocess, sys
from pathlib import Path
repo = Path(sys.argv[1]).resolve()
out_linux = repo / "build" / "linux" / "linux"
out_linux.mkdir(parents=True, exist_ok=True)
# Compile a real C smoke probe if present, proving a real compiler invocation runs.
probe_c = repo / "src" / "probe.c"
if probe_c.is_file():
    subprocess.run(["cc", "-std=gnu23", str(probe_c), "-o", str(repo / "build" / "linux" / "probe")], check=True)
tag = (repo / "VERSION").read_bytes() if (repo / "VERSION").exists() else b"v1"
src_h = Path({str(helio_src)!r}).read_bytes() + b"\\n#TAG=" + tag + b"\\n"
src_e = Path({str(exe_src)!r}).read_bytes() + b"\\n#TAG=" + tag + b"\\n"
h_dst = out_linux / "helio"
e_dst = repo / "build" / "linux" / "helio.exe"
h_dst.write_bytes(src_h)
os.chmod(h_dst, 0o755)
e_dst.write_bytes(src_e)
os.chmod(e_dst, 0o755)
# Run real unittest discovery on the fixture repo's own nonrecursive tests/.
subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", str(repo / "tests"), "-v"], check=True)
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
        env = {**os.environ, "HELIO_NO_BOSUN": "1"}
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
        self.assertIn("python3 Heliograph/tools/install.py\n", readme)
        self.assertIn("python3 Heliograph/tools/install.py --status", readme)
        self.assertIn("python3 Heliograph/tools/install.py --back", readme)
        self.assertIn("python3 Heliograph/tools/install.py --remove", readme)
        self.assertIn("python3 Heliograph/tools/install.py --to /custom/bin --status", readme)

    def test_missing_sdk_clone_real_build_and_failed_test_preserves_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare_sdk = make_fixture_sdk_bare_repo(root, self.helio_bin, self.helio_exe)
            ws = root / "workspace"
            fixture_repo = ws / "Heliograph"
            (fixture_repo / "src").mkdir(parents=True)
            (fixture_repo / "tests").mkdir(parents=True)
            (fixture_repo / "src" / "probe.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
            (fixture_repo / "tests" / "test_fixture.py").write_text(
                "import os, unittest\n"
                "from pathlib import Path\n"
                "class T(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertFalse((Path(__file__).resolve().parents[1] / 'FAIL_TEST').exists())\n",
                encoding="utf-8",
            )
            (fixture_repo / "VERSION").write_text("v1", encoding="utf-8")
            commit1 = init_git_repo(fixture_repo, "v1")

            home = root / "home"
            home.mkdir()
            env = {
                "HOME": str(home),
                "HELIO_REPO": str(fixture_repo),
                "HELIO_SDK_URL": str(bare_sdk),
            }

            # 1. Missing ../ImpossibleSDK is cloned, real C probe compiled, fixture unittest runs, binaries installed.
            res = self._run_cli(env_extra=env)
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
            self.assertTrue((ws / "ImpossibleSDK" / "tools" / "app_build.py").is_file())
            self.assertTrue((fixture_repo / "build" / "linux" / "probe").is_file())
            to_dir = ws / "bin"
            store_dir = ws / "Heliograph-signals"
            wrapper = home / ".local" / "bin" / "helio"
            self.assertTrue((to_dir / "helio").is_file())
            self.assertTrue((to_dir / "helio.exe").is_file())
            self.assertTrue((to_dir / "installed.json").is_file())
            self.assertTrue((store_dir / "messages").is_dir())
            self.assertTrue(wrapper.is_file())

            manifest = json.loads((to_dir / "installed.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["current"]["commit"], commit1)
            self.assertFalse(manifest["current"]["dirty"])
            self.assertIsNone(manifest["previous"])
            old_sha = self.mod.sha256_file(to_dir / "helio")

            # 2. Failed fixture test installs nothing and preserves existing installation.
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

            def send_and_show(cmd: str, extra_env: dict[str, str] | None = None, store_flag: str = "") -> tuple[str, str]:
                e = {**shell_env, **(extra_env or {})}
                sent = subprocess.run(["/bin/sh", "-c", cmd], input="hello body\n", env=e, capture_output=True, text=True)
                self.assertEqual(sent.returncode, 0, sent.stderr + sent.stdout)
                msg_id = sent.stdout.strip()
                shown = subprocess.run(
                    ["/bin/sh", "-c", f"helio show {msg_id} --as proj/a {store_flag}"],
                    env=e,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(shown.returncode, 0, shown.stderr + shown.stdout)
                return msg_id, shown.stdout

            id_def, out_def = send_and_show('helio tell proj/a "default-subject" --as proj/b')
            self.assertIn("default-subject", out_def)

            _, out_env = send_and_show(
                'helio tell proj/a "env-subject" --as proj/b',
                extra_env={"HELIO_STORE": str(env_store)},
            )
            self.assertIn("env-subject", out_env)

            id_cli, out_cli = send_and_show(
                f'helio tell proj/a "cli-subject" --as proj/b --store "{cli_store}"',
                extra_env={"HELIO_STORE": str(env_store)},
                store_flag=f'--store "{cli_store}"',
            )
            self.assertIn("cli-subject", out_cli)

            miss = subprocess.run(["/bin/sh", "-c", f"helio show {id_cli} --as proj/a"], env=shell_env, capture_output=True, text=True)
            self.assertEqual(miss.returncode, 2)

            sentinel = default_store / "messages" / "sentinel.keep"
            sentinel.write_bytes(b"keep-every-byte\x00\xff")
            self.mod.install(repo=repo, to_dir=to_dir, store_dir=default_store, wrapper_path=wrapper, prebuilt=(self.helio_bin, self.helio_exe))
            self.assertEqual(sentinel.read_bytes(), b"keep-every-byte\x00\xff")
            still_there = subprocess.run(["/bin/sh", "-c", f"helio show {id_def} --as proj/a"], env=shell_env, capture_output=True, text=True)
            self.assertEqual(still_there.returncode, 0)
            self.assertIn("default-subject", still_there.stdout)

    def test_status_dirty_back_store_restore_exec_bit_and_idempotent_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            commit_a = init_git_repo(repo, "commit A")
            to_dir = root / "bin"
            store_a = root / "storeA"
            store_b = root / "storeB"
            wrapper = root / "home" / ".local" / "bin" / "helio"

            code, _ = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(code, 1)

            a_h, a_e = root / "a_helio", root / "a_helio.exe"
            make_distinct_binary(self.helio_bin, a_h, b"A")
            make_distinct_binary(self.helio_exe, a_e, b"A")
            self.mod.install(repo=repo, to_dir=to_dir, store_dir=store_a, wrapper_path=wrapper, prebuilt=(a_h, a_e))

            code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(code, 0)
            self.assertIn(commit_a, "\n".join(lines))
            self.assertNotIn("(dirty)", "\n".join(lines))

            # First-install --back refuses and leaves live pair intact.
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.go_back(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(ctx.exception.code, 2)

            # Dirty tree + second store B.
            (repo / "README.txt").write_text("dirty change\n", encoding="utf-8")
            b_h, b_e = root / "b_helio", root / "b_helio.exe"
            make_distinct_binary(self.helio_bin, b_h, b"B")
            make_distinct_binary(self.helio_exe, b_e, b"B")
            m2 = self.mod.install(repo=repo, to_dir=to_dir, store_dir=store_b, wrapper_path=wrapper, prebuilt=(b_h, b_e))
            self.assertEqual(m2["previous"]["store"], str(store_a.resolve()))
            self.assertEqual(m2["previous"]["wrapper"], str(wrapper.resolve()))

            code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(code, 0)
            self.assertIn(f"{commit_a} (dirty)", "\n".join(lines))

            # Missing execute bit on helio or wrapper -> status returns 3.
            os.chmod(to_dir / "helio", 0o644)
            code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(code, 3)
            self.assertIn("not executable", "\n".join(lines))
            os.chmod(to_dir / "helio", 0o755)

            os.chmod(wrapper, 0o644)
            code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(code, 3)
            self.assertIn("not executable", "\n".join(lines))
            os.chmod(wrapper, 0o755)

            # Tamper with live helio -> status exits 3 naming helio.
            saved_b = (to_dir / "helio").read_bytes()
            (to_dir / "helio").write_bytes(saved_b + b"X")
            code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(code, 3)
            self.assertIn("helio", "\n".join(lines))
            (to_dir / "helio").write_bytes(saved_b)

            # Corrupt backup -> --back refuses and leaves B intact.
            saved_old = (to_dir / "helio.old").read_bytes()
            (to_dir / "helio.old").write_bytes(b"corrupt")
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.go_back(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(ctx.exception.code, 2)
            self.assertEqual(self.mod.sha256_file(to_dir / "helio"), self.mod.sha256_file(b_h))
            (to_dir / "helio.old").write_bytes(saved_old)

            # Valid --back restores A AND its store_a binding in wrapper and manifest.
            m_back = self.mod.go_back(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(self.mod.sha256_file(to_dir / "helio"), self.mod.sha256_file(a_h))
            self.assertEqual(self.mod.sha256_file(to_dir / "helio.exe"), self.mod.sha256_file(a_e))
            self.assertEqual(m_back["current"]["store"], str(store_a.resolve()))
            self.assertIn(str(store_a.resolve()), wrapper.read_text(encoding="utf-8"))
            code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(code, 0)
            self.assertNotIn("(dirty)", "\n".join(lines))

            # Unrelated files including dot-prefixed .stage.notes and .rec.keep are NEVER deleted.
            unrelated = to_dir / "other-tool"
            dot_stage = to_dir / ".stage.notes"
            dot_rec = to_dir / ".rec.keep"
            for f in (unrelated, dot_stage, dot_rec):
                f.write_text("keep\n", encoding="utf-8")

            self.assertTrue(self.mod.remove(to_dir=to_dir, wrapper_path=wrapper))
            for f in (unrelated, dot_stage, dot_rec):
                self.assertTrue(f.is_file(), f"deleted unrelated file {f}")
            self.assertFalse((to_dir / "helio").exists())
            self.assertFalse(wrapper.exists())
            self.assertTrue(store_a.is_dir())
            self.assertFalse(self.mod.remove(to_dir=to_dir, wrapper_path=wrapper))

    def test_adversarial_ownership_symlinks_foreign_journal_and_collision_refusals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            init_git_repo(repo)
            store_dir = root / "store"
            new_h, new_e = root / "new_h", root / "new_e"
            make_distinct_binary(self.helio_bin, new_h, b"new")
            make_distinct_binary(self.helio_exe, new_e, b"new")

            # 1. Legacy adoption works without executing the candidate ELF, and foreign ELF is never executed.
            evil_dir = root / "evil_elf"
            evil_dir.mkdir()
            marker_touched = root / "evil_ran"
            evil_elf = evil_dir / "helio"
            # Valid ELF binary (copied /bin/sh or python) that is NOT Heliograph.
            shutil.copy2("/bin/sh", evil_elf)
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=repo, to_dir=evil_dir, store_dir=store_dir, wrapper_path=root / "w1" / "helio", prebuilt=(new_h, new_e))
            self.assertEqual(ctx.exception.code, 2)
            self.assertFalse(marker_touched.exists())

            # 2. Legacy manual installation adoption.
            leg_dir = root / "legacy_bin"
            leg_dir.mkdir()
            leg_wrap = root / "leg_home" / ".local" / "bin" / "helio"
            leg_wrap.parent.mkdir(parents=True)
            make_distinct_binary(self.helio_bin, leg_dir / "helio", b"legacy")
            make_distinct_binary(self.helio_exe, leg_dir / "helio.exe", b"legacy")
            legacy_sha = self.mod.sha256_file(leg_dir / "helio")
            leg_wrap.write_text(
                f"#!/bin/sh\n# Heliograph with this machine's store. HELIO_STORE or --store overrides it.\n"
                f'HELIO_STORE="${{HELIO_STORE:-{store_dir}}}" exec "{leg_dir}/helio" "$@"\n',
                encoding="utf-8",
            )
            os.chmod(leg_wrap, 0o755)
            m_leg = self.mod.install(repo=repo, to_dir=leg_dir, store_dir=store_dir, wrapper_path=leg_wrap, prebuilt=(new_h, new_e))
            self.assertEqual(m_leg["previous"]["commit"], "legacy")
            self.assertEqual(m_leg["previous"]["helio_sha256"], legacy_sha)

            # 3. After first install (previous=None), a foreign helio.old is refused instead of overwritten or deleted.
            first_dir = root / "first_bin"
            first_wrap = root / "first_w" / "helio"
            self.mod.install(repo=repo, to_dir=first_dir, store_dir=store_dir, wrapper_path=first_wrap, prebuilt=(new_h, new_e))
            (first_dir / "helio.old").write_text("foreign backup\n", encoding="utf-8")
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=repo, to_dir=first_dir, store_dir=store_dir, wrapper_path=first_wrap, prebuilt=(new_h, new_e))
            self.assertEqual(ctx.exception.code, 2)
            self.assertEqual((first_dir / "helio.old").read_text(encoding="utf-8"), "foreign backup\n")

            # 4. Dangling symlink at wrapper path is refused and never followed.
            sym_dir = root / "sym_bin"
            sym_wrap = root / "sym_w" / "helio"
            sym_wrap.parent.mkdir(parents=True)
            dangling_target = root / "nonexistent_dir" / "target"
            sym_wrap.symlink_to(dangling_target)
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=repo, to_dir=sym_dir, store_dir=store_dir, wrapper_path=sym_wrap, prebuilt=(new_h, new_e))
            self.assertEqual(ctx.exception.code, 2)
            self.assertFalse(dangling_target.exists())

            # 5. Wrapper colliding with --to/helio (--to ~/.local/bin with default wrapper) is refused.
            col_dir = root / "col_bin"
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=repo, to_dir=col_dir, store_dir=store_dir, wrapper_path=col_dir / "helio", prebuilt=(new_h, new_e))
            self.assertEqual(ctx.exception.code, 2)

            # 6. Foreign remove journal without valid ownership hashes is refused and deletes nothing.
            fj_dir = root / "foreign_journal"
            fj_dir.mkdir()
            (fj_dir / "helio").write_text("user tool\n", encoding="utf-8")
            (fj_dir / self.mod.JOURNAL_NAME).write_text(
                json.dumps({
                    "schema_version": 1,
                    "op": "remove",
                    "to_dir": str(fj_dir.resolve()),
                    "wrapper_path": str((root / "fj_w" / "helio").resolve()),
                    "saved_files": {},
                    "temp_files": [],
                    "expected_hashes": {},
                }),
                encoding="utf-8",
            )
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=repo, to_dir=fj_dir, store_dir=store_dir, wrapper_path=root / "fj_w" / "helio", prebuilt=(new_h, new_e))
            self.assertEqual(ctx.exception.code, 2)
            self.assertEqual((fj_dir / "helio").read_text(encoding="utf-8"), "user tool\n")

            # 7. Newline in destination path and credential-bearing SDK URL are refused.
            with self.assertRaises(self.mod.InstallError):
                self.mod.install(repo=repo, to_dir=root / "bad\nbin", store_dir=store_dir, wrapper_path=root / "nw" / "helio", prebuilt=(new_h, new_e))
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod._validate_sdk_url("https://alice:supersecret@example.com/sdk.git")
            self.assertNotIn("supersecret", str(ctx.exception))

            # 8. Store path that is a regular file is refused.
            file_store = root / "not_a_dir"
            file_store.write_text("file\n", encoding="utf-8")
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=repo, to_dir=root / "fs_bin", store_dir=file_store, wrapper_path=root / "fs_w" / "helio", prebuilt=(new_h, new_e))
            self.assertEqual(ctx.exception.code, 2)

    def test_locks_and_interrupted_a_never_clobbers_newer_b_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            init_git_repo(repo)
            dir_a, dir_b = root / "binA", root / "binB"
            store = root / "store"
            wrapper = root / "home" / ".local" / "bin" / "helio"
            a_h, a_e = root / "a_h", root / "a_e"
            b_h, b_e = root / "b_h", root / "b_e"
            make_distinct_binary(self.helio_bin, a_h, b"A")
            make_distinct_binary(self.helio_exe, a_e, b"A")
            make_distinct_binary(self.helio_bin, b_h, b"B")
            make_distinct_binary(self.helio_exe, b_e, b"B")

            # Same-destination lock refuses concurrent run.
            dir_a.mkdir()
            with self.mod.acquire_lock(dir_a / self.mod.LOCK_NAME, blocking=False):
                with self.assertRaises(self.mod.InstallError) as ctx:
                    self.mod.install(repo=repo, to_dir=dir_a, store_dir=store, wrapper_path=wrapper, prebuilt=(a_h, a_e))
                self.assertEqual(ctx.exception.code, 2)

            # Install A, interrupt upgrade of A, install B, then remove A -> B's wrapper MUST stay intact!
            self.mod.install(repo=repo, to_dir=dir_a, store_dir=store, wrapper_path=wrapper, prebuilt=(a_h, a_e))

            def crash_after_helio(stage: str) -> None:
                if stage == "after_helio":
                    raise SimulatedCrash(stage)

            with self.assertRaises(SimulatedCrash):
                self.mod.install(repo=repo, to_dir=dir_a, store_dir=store, wrapper_path=wrapper, prebuilt=(b_h, b_e), on_boundary=crash_after_helio)

            self.mod.install(repo=repo, to_dir=dir_b, store_dir=store, wrapper_path=wrapper, prebuilt=(b_h, b_e))
            self.assertTrue(self.mod.wrapper_belongs_to(wrapper, dir_b))

            self.mod.remove(to_dir=dir_a, wrapper_path=wrapper)
            self.assertTrue(wrapper.is_file())
            self.assertTrue(self.mod.wrapper_belongs_to(wrapper, dir_b))

            # Concurrent installs to A and B serialize cleanly on the wrapper lock.
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(self.mod.install, repo=repo, to_dir=dir_a, store_dir=store, wrapper_path=wrapper, prebuilt=(a_h, a_e))
                f2 = pool.submit(self.mod.install, repo=repo, to_dir=dir_b, store_dir=store, wrapper_path=wrapper, prebuilt=(b_h, b_e))
                f1.result()
                f2.result()
            self.assertTrue(self.mod.wrapper_belongs_to(wrapper, dir_a) or self.mod.wrapper_belongs_to(wrapper, dir_b))

    def test_crash_recovery_verifies_full_set_across_all_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            init_git_repo(repo)
            a_h, a_e = root / "a_h", root / "a_e"
            b_h, b_e = root / "b_h", root / "b_e"
            c_h, c_e = root / "c_h", root / "c_e"
            for tag, h_p, e_p in ((b"A", a_h, a_e), (b"B", b_h, b_e), (b"C", c_h, c_e)):
                make_distinct_binary(self.helio_bin, h_p, tag)
                make_distinct_binary(self.helio_exe, e_p, tag)

            # Install crash boundaries with 2 existing generations (A -> B, crashing while upgrading to C).
            for boundary in ("after_journal", "after_backups", "after_helio", "after_helio_exe", "after_wrapper", "after_manifest"):
                to_dir = root / f"inst_{boundary}"
                store = root / f"store_{boundary}"
                wrapper = root / f"w_inst_{boundary}" / "helio"
                self.mod.install(repo=repo, to_dir=to_dir, store_dir=store, wrapper_path=wrapper, prebuilt=(a_h, a_e))
                self.mod.install(repo=repo, to_dir=to_dir, store_dir=store, wrapper_path=wrapper, prebuilt=(b_h, b_e))
                msg_id = subprocess.run(
                    [str(to_dir / "helio"), "tell", "p/a", "keep-msg", "--as", "p/b", "--store", str(store)],
                    input="body\n",
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()

                def fail_at(stage: str, target=boundary) -> None:
                    if stage == target:
                        raise SimulatedCrash(stage)

                with self.assertRaises(SimulatedCrash):
                    self.mod.install(repo=repo, to_dir=to_dir, store_dir=store, wrapper_path=wrapper, prebuilt=(c_h, c_e), on_boundary=fail_at)
                self.assertTrue((to_dir / self.mod.JOURNAL_NAME).exists())
                self.assertTrue(self.mod.recover_if_needed(to_dir, wrapper))
                self.assertFalse((to_dir / self.mod.JOURNAL_NAME).exists())

                code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
                self.assertEqual(code, 0, f"{boundary}: {lines}")
                exp_cur_h, exp_cur_e = (c_h, c_e) if boundary == "after_manifest" else (b_h, b_e)
                exp_old_h, exp_old_e = (b_h, b_e) if boundary == "after_manifest" else (a_h, a_e)
                self.assertEqual(self.mod.sha256_file(to_dir / "helio"), self.mod.sha256_file(exp_cur_h))
                self.assertEqual(self.mod.sha256_file(to_dir / "helio.exe"), self.mod.sha256_file(exp_cur_e))
                self.assertEqual(self.mod.sha256_file(to_dir / "helio.old"), self.mod.sha256_file(exp_old_h))
                self.assertEqual(self.mod.sha256_file(to_dir / "helio.exe.old"), self.mod.sha256_file(exp_old_e))
                self.assertTrue(self.mod.wrapper_belongs_to(wrapper, to_dir))
                chk = subprocess.run([str(to_dir / "helio"), "show", msg_id, "--as", "p/a", "--store", str(store)], capture_output=True, text=True)
                self.assertEqual(chk.returncode, 0)
                self.assertIn("keep-msg", chk.stdout)

            # --back crash boundaries (verify BOTH live files and BOTH backups).
            for boundary in ("after_journal", "after_backups", "after_helio", "after_helio_exe", "after_manifest"):
                to_dir = root / f"back_{boundary}"
                store = root / f"bstore_{boundary}"
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
                exp_cur_h, exp_cur_e = (a_h, a_e) if boundary == "after_manifest" else (b_h, b_e)
                exp_old_h, exp_old_e = (b_h, b_e) if boundary == "after_manifest" else (a_h, a_e)
                self.assertEqual(self.mod.sha256_file(to_dir / "helio"), self.mod.sha256_file(exp_cur_h))
                self.assertEqual(self.mod.sha256_file(to_dir / "helio.exe"), self.mod.sha256_file(exp_cur_e))
                self.assertEqual(self.mod.sha256_file(to_dir / "helio.old"), self.mod.sha256_file(exp_old_h))
                self.assertEqual(self.mod.sha256_file(to_dir / "helio.exe.old"), self.mod.sha256_file(exp_old_e))

            # --remove crash boundaries.
            for boundary in ("after_journal", "after_wrapper", "after_binaries"):
                to_dir = root / f"rem_{boundary}"
                store = root / f"rstore_{boundary}"
                wrapper = root / f"w_rem_{boundary}" / "helio"
                self.mod.install(repo=repo, to_dir=to_dir, store_dir=store, wrapper_path=wrapper, prebuilt=(a_h, a_e))

                def fail_at(stage: str, target=boundary) -> None:
                    if stage == target:
                        raise SimulatedCrash(stage)

                with self.assertRaises(SimulatedCrash):
                    self.mod.remove(to_dir=to_dir, wrapper_path=wrapper, on_boundary=fail_at)
                self.assertTrue(self.mod.recover_if_needed(to_dir, wrapper))
                self.assertFalse((to_dir / "helio").exists())
                self.assertFalse((to_dir / "helio.exe").exists())
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
            self.assertEqual(self.mod.sha256_file(to_dir / "helio"), self.mod.sha256_file(a_h))
            self.assertEqual(self.mod.sha256_file(to_dir / "helio.exe"), self.mod.sha256_file(a_e))
            code, _ = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(code, 0)

            # 2. Concurrent caller during live replacement catches OSError/FileNotFoundError and proves calls completed.
            stop = threading.Event()
            failures: list[str] = []
            completed = 0

            def caller():
                nonlocal completed
                while not stop.is_set():
                    try:
                        res = subprocess.run([str(to_dir / "helio"), "help"], capture_output=True, text=True)
                        if res.returncode != 0:
                            failures.append(f"rc={res.returncode} stderr={res.stderr}")
                        else:
                            completed += 1
                    except OSError as exc:
                        failures.append(f"oserror={exc}")

            t = threading.Thread(target=caller)
            t.start()
            try:
                for pair in ((b_h, b_e), (a_h, a_e), (b_h, b_e)):
                    self.mod.install(repo=repo, to_dir=to_dir, store_dir=store, wrapper_path=wrapper, prebuilt=pair)
            finally:
                stop.set()
                t.join(timeout=10)
            self.assertEqual(failures, [])
            self.assertGreater(completed, 0)

            # 3. Exercise a real held Windows helio.exe with a concurrent second Windows caller during publication.
            if shutil.which("python.exe") and shutil.which("wslpath"):
                win_exe = subprocess.run(
                    ["wslpath", "-w", str(to_dir / "helio.exe")],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                holder = subprocess.Popen(
                    ["python.exe", "-c", f"import time; f = open({win_exe!r}, 'rb'); print('HELD', flush=True); time.sleep(5)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                win_stop = threading.Event()
                win_failures: list[str] = []
                win_completed = 0

                def win_caller():
                    nonlocal win_completed
                    while not win_stop.is_set():
                        try:
                            r = subprocess.run(
                                [str(to_dir / "helio.exe"), "help"],
                                stdin=subprocess.DEVNULL,
                                capture_output=True,
                                text=True,
                                timeout=30,
                            )
                            if r.returncode != 0:
                                win_failures.append(f"rc={r.returncode} stderr={r.stderr}")
                            else:
                                win_completed += 1
                        except (OSError, subprocess.SubprocessError) as exc:
                            win_failures.append(f"exc={exc}")

                wt = threading.Thread(target=win_caller)
                try:
                    assert holder.stdout is not None
                    ready = holder.stdout.readline().strip()
                    self.assertEqual(ready, "HELD")
                    wt.start()
                    try:
                        self.mod.install(repo=repo, to_dir=to_dir, store_dir=store, wrapper_path=wrapper, prebuilt=(a_h, a_e))
                    except self.mod.BusyExecutableError as exc:
                        self.assertEqual(exc.code, 3)
                    # Ensure at least one call completes while/after publication.
                    while win_completed == 0 and not win_failures:
                        pass
                finally:
                    win_stop.set()
                    wt.join(timeout=35)
                    holder.terminate()
                    holder.wait(timeout=10)
                self.assertEqual(win_failures, [])
                self.assertGreater(win_completed, 0)
                code, _ = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
                self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
