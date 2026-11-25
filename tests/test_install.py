import concurrent.futures
import contextlib
import errno
import importlib.util
import json
import os
import secrets
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


def find_mingw_gcc() -> Path | None:
    sdk_dir = Path(os.environ.get("IMPOSSIBLE_SDK_DIR") or (REPO.parent / "ImpossibleSDK"))
    bootstrap_py = sdk_dir / "tools" / "bootstrap.py"
    if not bootstrap_py.is_file():
        return None
    spec = importlib.util.spec_from_file_location("sdk_bootstrap", bootstrap_py)
    if not spec or not spec.loader:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    part = {p["name"]: p for p in mod.components()}
    cc = mod.home(part["llvm-mingw"]) / "bin" / "x86_64-w64-mingw32-gcc"
    return cc if cc.is_file() else None


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


def make_fixture_sdk_bare_repo(root: Path, mingw_gcc: Path) -> Path:
    """Create a local bare git repo acting as ImpossibleSDK that compiles BOTH helio and helio.exe from C source and runs unittest."""
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
h_dst = out_linux / "helio"
e_dst = repo / "build" / "linux" / "helio.exe"
subprocess.run(["cc", "-std=gnu23", "-O2", str(repo / "src" / "helio_fixture.c"), "-o", str(h_dst)], check=True)
os.chmod(h_dst, 0o755)
subprocess.run([{str(mingw_gcc)!r}, "-O2", str(repo / "src" / "helio_fixture_win.c"), "-o", str(e_dst)], check=True)
os.chmod(e_dst, 0o755)
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
        cls.mingw_gcc = find_mingw_gcc()

    def test_readme_documents_quick_start_and_subcommands(self) -> None:
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("python3 Heliograph/tools/install.py\n", readme)
        self.assertIn("python3 Heliograph/tools/install.py --status", readme)
        self.assertIn("python3 Heliograph/tools/install.py --back", readme)
        self.assertIn("python3 Heliograph/tools/install.py --remove", readme)
        self.assertIn("python3 Heliograph/tools/install.py --to /custom/bin --status", readme)

    def test_documented_command_missing_sdk_compiles_both_and_failed_test_preserves(self) -> None:
        self.assertIsNotNone(self.mingw_gcc, "pinned llvm-mingw cross-compiler not found")
        assert self.mingw_gcc is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bare_sdk = make_fixture_sdk_bare_repo(root, self.mingw_gcc)
            ws = root / "workspace"
            fixture_repo = ws / "Heliograph"
            (fixture_repo / "src").mkdir(parents=True)
            (fixture_repo / "tests").mkdir(parents=True)
            (fixture_repo / "tools").mkdir(parents=True)
            shutil.copy2(INSTALL_SCRIPT, fixture_repo / "tools" / "install.py")

            def write_fixture_sources(tag: str) -> None:
                (fixture_repo / "src" / "helio_fixture.c").write_text(
                    '#include <stdio.h>\n'
                    '#include <string.h>\n'
                    '#include <sys/stat.h>\n'
                    'static const char *m1 = "HELIO_STORE";\n'
                    'static const char *m2 = "helio passes messages";\n'
                    'int main(int argc, char **argv) {\n'
                    '    if (argc >= 4 && strcmp(argv[1], "init") == 0 && strcmp(argv[2], "--store") == 0) {\n'
                    '        mkdir(argv[3], 0755);\n'
                    '        char buf[1024];\n'
                    '        snprintf(buf, sizeof buf, "%s/messages", argv[3]);\n'
                    '        mkdir(buf, 0755);\n'
                    '        return 0;\n'
                    '    }\n'
                    f'    printf("%s %s {tag}\\n", m1, m2);\n'
                    '    return 0;\n'
                    '}\n',
                    encoding="utf-8",
                )
                (fixture_repo / "src" / "helio_fixture_win.c").write_text(
                    '#include <stdio.h>\n'
                    'static const char *m1 = "HELIO_STORE";\n'
                    'static const char *m2 = "helio passes messages";\n'
                    f'int main(void) {{ printf("%s %s {tag}\\n", m1, m2); return 0; }}\n',
                    encoding="utf-8",
                )

            write_fixture_sources("fixture-v1")
            (fixture_repo / "tests" / "test_fixture.py").write_text(
                "import unittest\n"
                "from pathlib import Path\n"
                "class T(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertFalse((Path(__file__).resolve().parents[1] / 'FAIL_TEST').exists())\n",
                encoding="utf-8",
            )
            commit1 = init_git_repo(fixture_repo, "v1")

            home = root / "home"
            home.mkdir()
            env = {**os.environ, "HOME": str(home), "HELIO_SDK_URL": str(bare_sdk), "HELIO_NO_BOSUN": "1"}
            env.pop("HELIO_STORE", None)
            env.pop("HELIO_REPO", None)

            # Exact documented Quick start command from workspace parent without HELIO_REPO.
            res = subprocess.run([sys.executable, "Heliograph/tools/install.py"], cwd=ws, env=env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
            to_dir = ws / "bin"
            built_h = fixture_repo / "build" / "linux" / "linux" / "helio"
            built_e = fixture_repo / "build" / "linux" / "helio.exe"
            self.assertEqual(self.mod.sha256_file(to_dir / "helio"), self.mod.sha256_file(built_h))
            self.assertEqual(self.mod.sha256_file(to_dir / "helio.exe"), self.mod.sha256_file(built_e))
            probe = subprocess.run([str(home / ".local" / "bin" / "helio")], env=env, capture_output=True, text=True)
            self.assertEqual(probe.returncode, 0)
            self.assertIn("fixture-v1", probe.stdout)

            manifest = json.loads((to_dir / "installed.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["current"]["commit"], commit1)
            self.assertFalse(manifest["current"]["dirty"])
            old_h_sha = self.mod.sha256_file(to_dir / "helio")
            old_e_sha = self.mod.sha256_file(to_dir / "helio.exe")

            # Change BOTH C sources so the failing build produces distinct bytes, then fail tests.
            write_fixture_sources("fixture-v2-broken")
            (fixture_repo / "FAIL_TEST").write_text("1", encoding="utf-8")
            fail = subprocess.run([sys.executable, "Heliograph/tools/install.py"], cwd=ws, env=env, capture_output=True, text=True)
            self.assertNotEqual(fail.returncode, 0)
            # Confirm the rejected build produced distinct bytes on disk, yet NEITHER was installed.
            self.assertNotEqual(self.mod.sha256_file(built_h), old_h_sha)
            self.assertNotEqual(self.mod.sha256_file(built_e), old_e_sha)
            self.assertEqual(self.mod.sha256_file(to_dir / "helio"), old_h_sha)
            self.assertEqual(self.mod.sha256_file(to_dir / "helio.exe"), old_e_sha)
            st = subprocess.run([sys.executable, "Heliograph/tools/install.py", "--status"], cwd=ws, env=env, capture_output=True, text=True)
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

            self.mod.install(repo=repo, to_dir=to_dir, store_dir=default_store, wrapper_path=wrapper, prebuilt=(self.helio_bin, self.helio_exe))

            shell_env = {"HOME": str(home), "PATH": f"{wrapper.parent}:/usr/bin:/bin"}
            for s in (env_store, cli_store):
                r = subprocess.run(["/bin/sh", "-c", 'helio init --store "$1"', "_", str(s)], env=shell_env, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)

            def send_and_show(cmd: str, extra_env: dict[str, str] | None = None, store_flag: str = "") -> tuple[str, str]:
                e = {**shell_env, **(extra_env or {})}
                sent = subprocess.run(["/bin/sh", "-c", cmd], input="hello body\n", env=e, capture_output=True, text=True)
                self.assertEqual(sent.returncode, 0, sent.stderr + sent.stdout)
                msg_id = sent.stdout.strip()
                shown = subprocess.run(["/bin/sh", "-c", f"helio show {msg_id} --as proj/a {store_flag}"], env=e, capture_output=True, text=True)
                self.assertEqual(shown.returncode, 0, shown.stderr + shown.stdout)
                return msg_id, shown.stdout

            id_def, out_def = send_and_show('helio tell proj/a "default-subject" --as proj/b')
            self.assertIn("default-subject", out_def)

            _, out_env = send_and_show('helio tell proj/a "env-subject" --as proj/b', extra_env={"HELIO_STORE": str(env_store)})
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

    def test_status_dirty_back_wrapper_repair_refusal_and_idempotent_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            commit_a = init_git_repo(repo, "commit A")
            to_dir = root / "bin"
            store_a, store_b = root / "storeA", root / "storeB"
            wrap_a = root / "home" / ".local" / "bin" / "helio"
            wrap_b = root / "home" / ".local" / "bin" / "helio-alt"

            code, _ = self.mod.status(to_dir=to_dir, wrapper_path=wrap_a)
            self.assertEqual(code, 1)

            a_h, a_e = root / "a_h", root / "a_e"
            b_h, b_e = root / "b_h", root / "b_e"
            c_h, c_e = root / "c_h", root / "c_e"
            for tag, h_p, e_p in ((b"A", a_h, a_e), (b"B", b_h, b_e), (b"C", c_h, c_e)):
                make_distinct_binary(self.helio_bin, h_p, tag)
                make_distinct_binary(self.helio_exe, e_p, tag)

            # Install A (clean) with wrap_a + store_a, then upgrade to B (dirty) with wrap_b + store_b.
            self.mod.install(repo=repo, to_dir=to_dir, store_dir=store_a, wrapper_path=wrap_a, prebuilt=(a_h, a_e))
            (repo / "README.txt").write_text("dirty\n", encoding="utf-8")
            m_b = self.mod.install(repo=repo, to_dir=to_dir, store_dir=store_b, wrapper_path=wrap_b, prebuilt=(b_h, b_e))
            # Unconditional post-condition: previous pair is complete and matches backups.
            self.assertIsNotNone(m_b["previous"])
            self.assertEqual(self.mod.sha256_file(to_dir / "helio.old"), m_b["previous"]["helio_sha256"])
            self.assertEqual(self.mod.sha256_file(to_dir / "helio.exe.old"), m_b["previous"]["helio_exe_sha256"])

            # Direct dirty status check on B.
            code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrap_b)
            self.assertEqual(code, 0)
            self.assertIn(f"{commit_a} (dirty)", "\n".join(lines))

            # Unreadable live binary -> status returns exit 3.
            with mock.patch.object(self.mod, "sha256_file", side_effect=PermissionError(errno.EACCES, "denied")):
                code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrap_b)
            self.assertEqual(code, 3)
            self.assertIn("unreadable binary", "\n".join(lines))

            # Missing execute bit on helio or wrapper -> status returns exit 3.
            os.chmod(to_dir / "helio", 0o644)
            code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrap_b)
            self.assertEqual(code, 3)
            self.assertIn("not executable", "\n".join(lines))
            os.chmod(to_dir / "helio", 0o755)

            os.chmod(wrap_b, 0o644)
            code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrap_b)
            self.assertEqual(code, 3)
            self.assertIn("not executable", "\n".join(lines))
            os.chmod(wrap_b, 0o755)

            # Live binary hash mismatch -> status returns exit 3 naming helio.
            saved_b = (to_dir / "helio").read_bytes()
            (to_dir / "helio").write_bytes(saved_b + b"X")
            code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrap_b)
            self.assertEqual(code, 3)
            self.assertIn("modified binary", "\n".join(lines))
            (to_dir / "helio").write_bytes(saved_b)

            # Corrupt backup helio.old -> --back refuses with exit 2 leaving live pair intact.
            saved_old = (to_dir / "helio.old").read_bytes()
            (to_dir / "helio.old").write_bytes(b"corrupt")
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.go_back(to_dir=to_dir, wrapper_path=wrap_b)
            self.assertEqual(ctx.exception.code, 2)
            self.assertEqual(self.mod.sha256_file(to_dir / "helio"), self.mod.sha256_file(b_h))
            (to_dir / "helio.old").write_bytes(saved_old)

            # Modified live binary -> --back refuses with exit 2 and leaves live+backups unchanged.
            (to_dir / "helio").write_bytes(b"foreign live replacement")
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.go_back(to_dir=to_dir, wrapper_path=wrap_b)
            self.assertEqual(ctx.exception.code, 2)
            self.assertEqual((to_dir / "helio").read_bytes(), b"foreign live replacement")
            self.assertEqual(self.mod.sha256_file(to_dir / "helio.old"), self.mod.sha256_file(a_h))
            (to_dir / "helio").write_bytes(saved_b)

            # Missing shared wrapper on a two-generation installation: --back recreates the wrapper cleanly!
            shared_dir = root / "shared_wrap_back"
            shared_wrap = root / "sw_home" / "helio"
            self.mod.install(repo=repo, to_dir=shared_dir, store_dir=store_a, wrapper_path=shared_wrap, prebuilt=(a_h, a_e))
            self.mod.install(repo=repo, to_dir=shared_dir, store_dir=store_a, wrapper_path=shared_wrap, prebuilt=(b_h, b_e))
            shared_wrap.unlink()
            self.mod.go_back(to_dir=shared_dir, wrapper_path=shared_wrap)
            self.assertTrue(shared_wrap.is_file())
            code, _ = self.mod.status(to_dir=shared_dir, wrapper_path=shared_wrap)
            self.assertEqual(code, 0)

            # If wrap_a was vacated and another destination claimed it, --back refuses with exit 2 without clobbering it.
            other_dir = root / "other_bin"
            wrap_a.unlink()
            self.mod.install(repo=repo, to_dir=other_dir, store_dir=store_a, wrapper_path=wrap_a, prebuilt=(c_h, c_e))
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.go_back(to_dir=to_dir, wrapper_path=wrap_b)
            self.assertEqual(ctx.exception.code, 2)
            self.assertTrue(self.mod.wrapper_belongs_to(wrap_a, other_dir))
            self.mod.remove(to_dir=other_dir, wrapper_path=wrap_a)

            # Valid --back from wrap_b restores A AND moves wrapper binding back to wrap_a (removing wrap_b).
            m_back = self.mod.go_back(to_dir=to_dir, wrapper_path=wrap_b)
            self.assertEqual(m_back["current"]["wrapper"], str(wrap_a.resolve()))
            self.assertEqual(m_back["current"]["store"], str(store_a.resolve()))
            self.assertTrue(wrap_a.is_file())
            self.assertFalse(wrap_b.exists())
            code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrap_a)
            self.assertEqual(code, 0, lines)
            self.assertIn(commit_a, "\n".join(lines))
            self.assertNotIn("(dirty)", "\n".join(lines))

            # Missing live binary -> install refuses with exit 2 instead of committing an incomplete previous generation.
            saved_exe = (to_dir / "helio.exe").read_bytes()
            (to_dir / "helio.exe").unlink()
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=repo, to_dir=to_dir, store_dir=store_a, wrapper_path=wrap_a, prebuilt=(c_h, c_e))
            self.assertEqual(ctx.exception.code, 2)
            (to_dir / "helio.exe").write_bytes(saved_exe)
            os.chmod(to_dir / "helio.exe", 0o755)

            # Unrelated files including dot-prefixed .stage.notes and .rec.keep survive --remove; repeated remove is idempotent.
            unrelated = to_dir / "other-tool"
            dot_stage = to_dir / ".stage.notes"
            dot_rec = to_dir / ".rec.keep"
            for f in (unrelated, dot_stage, dot_rec):
                f.write_text("keep\n", encoding="utf-8")
            self.assertTrue(self.mod.remove(to_dir=to_dir, wrapper_path=wrap_a))
            for f in (unrelated, dot_stage, dot_rec):
                self.assertTrue(f.is_file(), f"deleted unrelated file {f}")
            self.assertFalse((to_dir / "helio").exists())
            self.assertFalse(wrap_a.exists())
            self.assertTrue(store_a.is_dir())
            self.assertFalse(self.mod.remove(to_dir=to_dir, wrapper_path=wrap_a))
            code, _ = self.mod.status(to_dir=to_dir, wrapper_path=wrap_a)
            self.assertEqual(code, 1)

            # Partial installation (only helio.old without manifest) -> status returns 3, not 1.
            partial_dir = root / "partial_bin"
            partial_dir.mkdir()
            (partial_dir / "helio.old").write_bytes(b"leftover")
            code, lines = self.mod.status(to_dir=partial_dir, wrapper_path=root / "pw" / "helio")
            self.assertEqual(code, 3)
            self.assertIn("helio.old", "\n".join(lines))

    def test_adversarial_journal_manifest_ownership_and_staging_crash_cleanup(self) -> None:
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

            # 1. Non-object JSON journal raises InstallError(code=2).
            jdir = root / "j_non_obj"
            jdir.mkdir()
            (jdir / self.mod.JOURNAL_NAME).write_text("[]\n", encoding="utf-8")
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.recover_if_needed(jdir, root / "jw" / "helio")
            self.assertEqual(ctx.exception.code, 2)

            # 2. Foreign journal trying to delete an unrelated dotfile or write an arbitrary alt_wrapper_path is refused.
            fdir = root / "j_dotfile"
            fdir.mkdir()
            secret_dot = fdir / ".env"
            secret_dot.write_text("keep=1\n", encoding="utf-8")
            outside_target = root / "pwned_outside"
            (fdir / self.mod.JOURNAL_NAME).write_text(
                json.dumps({
                    "schema_version": 1,
                    "op": "back",
                    "phase": "publishing",
                    "tx_id": "0123456789ab",
                    "to_dir": str(fdir.resolve()),
                    "wrapper_path": str((root / "fw" / "helio").resolve()),
                    "alt_wrapper_path": str(outside_target),
                    "saved_files": {},
                    "temp_files": [],
                }),
                encoding="utf-8",
            )
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.recover_if_needed(fdir, root / "fw" / "helio")
            self.assertEqual(ctx.exception.code, 2)
            self.assertFalse(outside_target.exists())

            # 3. Simulated SIGKILL during staging (after_snapshot, leaving staging journal on disk) cleans up ALL snapshots on retry.
            leak_dir = root / "sigkill_staging"
            leak_wrap = root / "lw" / "helio"
            self.mod.install(repo=repo, to_dir=leak_dir, store_dir=store, wrapper_path=leak_wrap, prebuilt=(a_h, a_e))

            def kill_during_staging(stage: str) -> None:
                if stage == "after_snapshot":
                    raise SimulatedCrash(stage)

            with self.assertRaises(SimulatedCrash):
                self.mod.install(repo=repo, to_dir=leak_dir, store_dir=store, wrapper_path=leak_wrap, prebuilt=(b_h, b_e), on_boundary=kill_during_staging)
            self.assertTrue((leak_dir / self.mod.JOURNAL_NAME).exists())
            self.assertTrue(any(p.name.endswith(".rec") for p in leak_dir.iterdir()))
            # Retry recovers staging journal and leaves zero orphaned snapshots.
            self.mod.install(repo=repo, to_dir=leak_dir, store_dir=store, wrapper_path=leak_wrap, prebuilt=(b_h, b_e))
            leftovers = [p.name for p in leak_dir.iterdir() if p.name.endswith((".stage", ".rec"))]
            self.assertEqual(leftovers, [])

            # 4. Recovery refuses to overwrite an externally modified manifest after an interrupted upgrade.
            ext_dir = root / "ext_manifest"
            ext_wrap = root / "ew" / "helio"
            self.mod.install(repo=repo, to_dir=ext_dir, store_dir=store, wrapper_path=ext_wrap, prebuilt=(a_h, a_e))

            def crash_after_helio(stage: str) -> None:
                if stage == "after_helio":
                    raise SimulatedCrash(stage)

            with self.assertRaises(SimulatedCrash):
                self.mod.install(repo=repo, to_dir=ext_dir, store_dir=store, wrapper_path=ext_wrap, prebuilt=(b_h, b_e), on_boundary=crash_after_helio)
            m = json.loads((ext_dir / self.mod.MANIFEST_NAME).read_text(encoding="utf-8"))
            m["current"]["commit"] = "0" * 40
            (ext_dir / self.mod.MANIFEST_NAME).write_text(json.dumps(m), encoding="utf-8")
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.recover_if_needed(ext_dir, ext_wrap)
            self.assertEqual(ctx.exception.code, 2)

            # 5. Committed branch refuses to discard snapshots if wrapper is missing, non-executable, OR points to wrong store.
            comm_dir = root / "comm_check"
            comm_wrap = root / "cw" / "helio"
            self.mod.install(repo=repo, to_dir=comm_dir, store_dir=store, wrapper_path=comm_wrap, prebuilt=(a_h, a_e))

            def crash_after_manifest(stage: str) -> None:
                if stage == "after_manifest":
                    raise SimulatedCrash(stage)

            with self.assertRaises(SimulatedCrash):
                self.mod.install(repo=repo, to_dir=comm_dir, store_dir=store, wrapper_path=comm_wrap, prebuilt=(b_h, b_e), on_boundary=crash_after_manifest)
            # 5a. Non-executable wrapper -> refuses to discard snapshots.
            os.chmod(comm_wrap, 0o644)
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.recover_if_needed(comm_dir, comm_wrap)
            self.assertEqual(ctx.exception.code, 3)
            self.assertTrue((comm_dir / self.mod.JOURNAL_NAME).exists())
            os.chmod(comm_wrap, 0o755)
            # 5b. Wrong store in wrapper -> refuses to discard snapshots.
            saved_wrap_text = comm_wrap.read_text(encoding="utf-8")
            comm_wrap.write_text(self.mod.render_wrapper(comm_dir, root / "wrong_store"), encoding="utf-8")
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.recover_if_needed(comm_dir, comm_wrap)
            self.assertEqual(ctx.exception.code, 3)
            # 5c. Missing wrapper -> refuses to discard snapshots.
            comm_wrap.unlink()
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.recover_if_needed(comm_dir, comm_wrap)
            self.assertEqual(ctx.exception.code, 3)
            self.assertTrue((comm_dir / self.mod.JOURNAL_NAME).exists())
            # Restore valid wrapper -> recovery now succeeds.
            comm_wrap.write_text(saved_wrap_text, encoding="utf-8")
            os.chmod(comm_wrap, 0o755)
            self.assertTrue(self.mod.recover_if_needed(comm_dir, comm_wrap))

            # 6. Hand-crafted foreign remove AND foreign install journals with matching SHA-256 of an unrelated file are refused.
            frem_dir = root / "foreign_rem_hash"
            frem_dir.mkdir()
            unrelated_helio = frem_dir / "helio"
            unrelated_helio.write_text("unrelated user script\n", encoding="utf-8")
            unrelated_sha = self.mod.sha256_file(unrelated_helio)
            (frem_dir / self.mod.JOURNAL_NAME).write_text(
                json.dumps({
                    "schema_version": 2,
                    "op": "remove",
                    "phase": "publishing",
                    "tx_id": "abcdef012345",
                    "to_dir": str(frem_dir.resolve()),
                    "wrapper_path": str((root / "frw" / "helio").resolve()),
                    "saved_files": {},
                    "temp_files": [],
                    "expected_hashes": {"helio": unrelated_sha},
                }),
                encoding="utf-8",
            )
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.recover_if_needed(frem_dir, root / "frw" / "helio")
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("not a Heliograph artifact", str(ctx.exception))
            self.assertEqual(unrelated_helio.read_text(encoding="utf-8"), "unrelated user script\n")

            # Foreign install journal trying to unlink unrelated_helio when saved_files has existed=False.
            (frem_dir / self.mod.JOURNAL_NAME).write_text(
                json.dumps({
                    "schema_version": 2,
                    "op": "install",
                    "phase": "publishing",
                    "tx_id": "abcdef012345",
                    "to_dir": str(frem_dir.resolve()),
                    "wrapper_path": str((root / "frw" / "helio").resolve()),
                    "had_manifest": False,
                    "previous_manifest": None,
                    "pre_wrappers": {},
                    "saved_files": {"helio": {"existed": False, "rec_name": None, "sha256": None}},
                    "temp_files": [],
                    "desired_manifest": {
                        "schema_version": 1,
                        "current": {"commit": "a" * 40, "helio_sha256": unrelated_sha, "helio_exe_sha256": unrelated_sha},
                        "previous": None,
                    },
                }),
                encoding="utf-8",
            )
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.recover_if_needed(frem_dir, root / "frw" / "helio")
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("not a Heliograph artifact", str(ctx.exception))
            self.assertEqual(unrelated_helio.read_text(encoding="utf-8"), "unrelated user script\n")

            # 7. Class-level regression for interrupted --back across ALL wrapper-aliasing combinations:
            #    (X,X,X), (X,X,Y), (X,Y,Y), (X,Y,X), and (X,Y,Z), plus deleted wrapper parent dir locking.
            for case_name, a_k, b_k, inv_k in (
                ("shared_all", "X", "X", "X"),
                ("shared_ab_other_inv", "X", "X", "Y"),
                ("distinct_inv_b", "X", "Y", "Y"),
                ("distinct_inv_a", "X", "Y", "X"),
                ("three_distinct", "X", "Y", "Z"),
            ):
                wdir = root / f"back_class_{case_name}"
                sa, sb = root / f"sa_{case_name}", root / f"sb_{case_name}"
                wmap = {k: root / f"w_{case_name}_{k}" / "helio" for k in ("X", "Y", "Z")}
                wa, wb, winv = wmap[a_k], wmap[b_k], wmap[inv_k]
                self.mod.install(repo=repo, to_dir=wdir, store_dir=sa, wrapper_path=wa, prebuilt=(a_h, a_e))
                self.mod.install(repo=repo, to_dir=wdir, store_dir=sb, wrapper_path=wb, prebuilt=(b_h, b_e))

                def crash_before_manifest(stage: str) -> None:
                    if stage == "after_wrapper":
                        raise SimulatedCrash(stage)

                with self.assertRaises(SimulatedCrash):
                    self.mod.go_back(to_dir=wdir, wrapper_path=winv, on_boundary=crash_before_manifest)
                self.assertTrue(self.mod.recover_if_needed(wdir, winv))
                # Current generation B's wrapper MUST have store B (never overwritten by store A!).
                self.assertEqual(wb.read_text(encoding="utf-8"), self.mod.render_wrapper(wdir, sb), case_name)
                if wa != wb:
                    self.assertEqual(wa.read_text(encoding="utf-8"), self.mod.render_wrapper(wdir, sa), case_name)
                if winv not in (wa, wb):
                    self.assertFalse(winv.exists(), case_name)
                # Now complete --back cleanly.
                self.mod.go_back(to_dir=wdir, wrapper_path=winv)
                self.assertEqual(wa.read_text(encoding="utf-8"), self.mod.render_wrapper(wdir, sa), case_name)
                if wb != wa:
                    self.assertFalse(wb.exists(), case_name)

            # 7b. Foreign rollback journal in a destination with GENUINE binaries and NO manifest cannot publish
            #     a foreign installed.json, create an external wrapper, or create an external lock/directory.
            #     Populate saved_files and desired_manifest with matching hashes so only the had_manifest/absent-manifest
            #     guard stops recovery (reverting that guard causes this test to fail).
            fman_dir = root / "foreign_manifest_pub"
            fman_dir.mkdir()
            make_distinct_binary(self.helio_bin, fman_dir / "helio", b"gen_legacy")
            make_distinct_binary(self.helio_exe, fman_dir / "helio.exe", b"gen_legacy")
            fman_h_sha = self.mod.sha256_file(fman_dir / "helio")
            fman_e_sha = self.mod.sha256_file(fman_dir / "helio.exe")
            existing_ext_dir = root / "existing_ext_dir"
            existing_ext_dir.mkdir()
            (fman_dir / self.mod.JOURNAL_NAME).write_text(
                json.dumps({
                    "schema_version": 2,
                    "op": "back",
                    "phase": "publishing",
                    "tx_id": "abcdef012345",
                    "to_dir": str(fman_dir.resolve()),
                    "wrapper_path": str((root / "fmw" / "helio").resolve()),
                    "had_manifest": True,
                    "previous_manifest": {
                        "schema_version": 1,
                        "current": {
                            "commit": "a" * 40,
                            "helio_sha256": fman_h_sha,
                            "helio_exe_sha256": fman_e_sha,
                            "store": str(store),
                            "wrapper": str(existing_ext_dir / "helio"),
                        },
                        "previous": None,
                    },
                    "pre_wrappers": {str(existing_ext_dir / "helio"): str(store)},
                    "saved_files": {
                        "helio": {"existed": True, "rec_name": ".helio.abcdef012345.rec", "sha256": fman_h_sha},
                        "helio.exe": {"existed": True, "rec_name": ".helio.exe.abcdef012345.rec", "sha256": fman_e_sha},
                    },
                    "temp_files": [],
                    "desired_manifest": {
                        "schema_version": 1,
                        "current": {
                            "commit": "b" * 40,
                            "helio_sha256": fman_h_sha,
                            "helio_exe_sha256": fman_e_sha,
                            "store": str(store),
                            "wrapper": str((root / "fmw" / "helio").resolve()),
                        },
                        "previous": None,
                    },
                }),
                encoding="utf-8",
            )
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=repo, to_dir=fman_dir, store_dir=store, wrapper_path=root / "fmw" / "helio", prebuilt=(a_h, a_e))
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("absent", str(ctx.exception))
            self.assertFalse((fman_dir / self.mod.MANIFEST_NAME).exists())
            self.assertFalse((existing_ext_dir / self.mod.WRAPPER_LOCK_NAME).exists())
            self.assertFalse((existing_ext_dir / "helio").exists())

            # 7c. Interrupted reinstall of C through A's wrapper (after A and B had distinct wrappers/stores) restores A's store (not B's!).
            re_dir = root / "reinst_prev_wrap"
            r_wa, r_wb = root / "r_wa" / "helio", root / "r_wb" / "helio"
            r_sa, r_sb, r_sc = root / "r_sa", root / "r_sb", root / "r_sc"
            self.mod.install(repo=repo, to_dir=re_dir, store_dir=r_sa, wrapper_path=r_wa, prebuilt=(a_h, a_e))
            self.mod.install(repo=repo, to_dir=re_dir, store_dir=r_sb, wrapper_path=r_wb, prebuilt=(b_h, b_e))

            def crash_after_wrap(stage: str) -> None:
                if stage == "after_wrapper":
                    raise SimulatedCrash(stage)

            with self.assertRaises(SimulatedCrash):
                self.mod.install(repo=repo, to_dir=re_dir, store_dir=r_sc, wrapper_path=r_wa, prebuilt=(a_h, a_e), on_boundary=crash_after_wrap)
            self.assertTrue(self.mod.recover_if_needed(re_dir, r_wa))
            self.assertEqual(r_wa.read_text(encoding="utf-8"), self.mod.render_wrapper(re_dir, r_sa))

            # 7d. Third successive wrapper retires the outgoing first wrapper (no orphan on --remove),
            #     and interrupted --back with a pre-existing third wrapper C still recovers all three.
            w3_dir = root / "back_preexist_c"
            w3a, w3b, w3c = root / "w3a2" / "helio", root / "w3b2" / "helio", root / "w3c2" / "helio"
            s3a, s3b, s3c = root / "s3a2", root / "s3b2", root / "s3c2"
            self.mod.install(repo=repo, to_dir=w3_dir, store_dir=s3c, wrapper_path=w3c, prebuilt=(a_h, a_e))
            self.mod.install(repo=repo, to_dir=w3_dir, store_dir=s3a, wrapper_path=w3a, prebuilt=(a_h, a_e))
            self.mod.install(repo=repo, to_dir=w3_dir, store_dir=s3b, wrapper_path=w3b, prebuilt=(b_h, b_e))
            # Outgoing w3c was retired on the 3rd install so at most 2 wrappers remain.
            self.assertFalse(w3c.exists())
            self.assertTrue(w3a.exists())
            self.assertTrue(w3b.exists())
            # Plant w3c as a pre-existing third managed wrapper to exercise interrupted --back through C.
            w3c.parent.mkdir(parents=True, exist_ok=True)
            w3c.write_text(self.mod.render_wrapper(w3_dir, s3c), encoding="utf-8")
            os.chmod(w3c, 0o755)

            def crash_at_unlink_0(stage: str) -> None:
                if stage == "after_unlink_old_wrapper_0":
                    raise SimulatedCrash(stage)

            with self.assertRaises(SimulatedCrash):
                self.mod.go_back(to_dir=w3_dir, wrapper_path=w3c, on_boundary=crash_at_unlink_0)
            self.assertTrue(self.mod.recover_if_needed(w3_dir, w3c))
            self.assertEqual(w3a.read_text(encoding="utf-8"), self.mod.render_wrapper(w3_dir, s3a))
            self.assertEqual(w3b.read_text(encoding="utf-8"), self.mod.render_wrapper(w3_dir, s3b))
            self.assertEqual(w3c.read_text(encoding="utf-8"), self.mod.render_wrapper(w3_dir, s3c))
            # Verify 3-generation install + remove leaves zero owned wrappers and preserves unrelated files.
            w3_rem_dir = root / "three_gen_remove"
            w3_rem_dir.mkdir()
            unrelated_keep = w3_rem_dir / "keep.txt"
            unrelated_keep.write_text("keep", encoding="utf-8")
            rw1, rw2, rw3 = root / "rw1" / "helio", root / "rw2" / "helio", root / "rw3" / "helio"
            self.mod.install(repo=repo, to_dir=w3_rem_dir, store_dir=s3a, wrapper_path=rw1, prebuilt=(a_h, a_e))
            self.mod.install(repo=repo, to_dir=w3_rem_dir, store_dir=s3b, wrapper_path=rw2, prebuilt=(b_h, b_e))
            self.mod.install(repo=repo, to_dir=w3_rem_dir, store_dir=s3c, wrapper_path=rw3, prebuilt=(a_h, a_e))
            self.assertFalse(rw1.exists())
            self.assertTrue(self.mod.remove(to_dir=w3_rem_dir, wrapper_path=rw3))
            self.assertFalse(rw1.exists())
            self.assertFalse(rw2.exists())
            self.assertFalse(rw3.exists())
            self.assertEqual(unrelated_keep.read_text(encoding="utf-8"), "keep")

            # 7e. Foreign journal with had_manifest=False cannot write an external wrapper or create its parent dir.
            fnom_dir = root / "foreign_no_manifest"
            fnom_dir.mkdir()
            absent_ext_dir = root / "absent_ext_dir_no_manifest"
            (fnom_dir / self.mod.JOURNAL_NAME).write_text(
                json.dumps({
                    "schema_version": 2,
                    "op": "install",
                    "phase": "publishing",
                    "tx_id": "abcdef012345",
                    "to_dir": str(fnom_dir.resolve()),
                    "wrapper_path": str((root / "fnom_w" / "helio").resolve()),
                    "had_manifest": False,
                    "previous_manifest": None,
                    "pre_wrappers": {str(absent_ext_dir / "helio"): str(store)},
                    "saved_files": {},
                    "temp_files": [],
                    "desired_manifest": {
                        "schema_version": 1,
                        "current": {
                            "commit": "a" * 40,
                            "helio_sha256": "0" * 64,
                            "helio_exe_sha256": "0" * 64,
                            "store": str(store),
                            "wrapper": str(absent_ext_dir / "helio"),
                        },
                        "previous": None,
                    },
                }),
                encoding="utf-8",
            )
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.recover_if_needed(fnom_dir, root / "fnom_w" / "helio")
            self.assertEqual(ctx.exception.code, 2)
            self.assertFalse(absent_ext_dir.exists())

            # 7f. Interrupted --back whose wrapper is acquired by another destination reports exit 3 without clobbering the other destination.
            sup_a, sup_d = root / "sup_a", root / "sup_d"
            sup_wa, sup_wb = root / "sup_wa" / "helio", root / "sup_wb" / "helio"
            self.mod.install(repo=repo, to_dir=sup_a, store_dir=r_sa, wrapper_path=sup_wa, prebuilt=(a_h, a_e))
            self.mod.install(repo=repo, to_dir=sup_a, store_dir=r_sb, wrapper_path=sup_wb, prebuilt=(b_h, b_e))

            with self.assertRaises(SimulatedCrash):
                self.mod.go_back(to_dir=sup_a, wrapper_path=sup_wb, on_boundary=crash_after_wrap)
            # Another destination D legitimately takes vacant sup_wb.
            self.mod.install(repo=repo, to_dir=sup_d, store_dir=r_sc, wrapper_path=sup_wb, prebuilt=(a_h, a_e))
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.recover_if_needed(sup_a, sup_wb)
            self.assertEqual(ctx.exception.code, 3)
            self.assertTrue(self.mod.wrapper_belongs_to(sup_wb, sup_d))
            self.assertTrue((sup_a / self.mod.JOURNAL_NAME).exists())
            # Once D releases sup_wb, retrying recovery finishes cleanly and restores sup_wb with r_sb!
            self.mod.remove(to_dir=sup_d, wrapper_path=sup_wb)
            self.assertTrue(self.mod.recover_if_needed(sup_a, sup_wb))
            self.assertEqual(sup_wb.read_text(encoding="utf-8"), self.mod.render_wrapper(sup_a, r_sb))
            code, _ = self.mod.status(to_dir=sup_a, wrapper_path=sup_wb)
            self.assertEqual(code, 0)

            # 7g. Legacy adoption with a different --store preserves the legacy wrapper's store in previous.store for --back.
            leg2_dir = root / "legacy_diff_store"
            leg2_dir.mkdir()
            leg2_old_store = root / "legacy_old_store"
            leg2_new_store = root / "legacy_new_store"
            leg2_wrap = root / "leg2_w" / "helio"
            leg2_wrap.parent.mkdir(parents=True)
            make_distinct_binary(self.helio_bin, leg2_dir / "helio", b"leg2")
            make_distinct_binary(self.helio_exe, leg2_dir / "helio.exe", b"leg2")
            leg2_wrap.write_text(
                f"#!/bin/sh\n# Heliograph with this machine's store. HELIO_STORE or --store overrides it.\n"
                f'HELIO_STORE="${{HELIO_STORE:-{leg2_old_store}}}" exec "{leg2_dir}/helio" "$@"\n',
                encoding="utf-8",
            )
            os.chmod(leg2_wrap, 0o755)
            m_leg2 = self.mod.install(repo=repo, to_dir=leg2_dir, store_dir=leg2_new_store, wrapper_path=leg2_wrap, prebuilt=(b_h, b_e))
            self.assertEqual(m_leg2["previous"]["store"], str(leg2_old_store))
            self.mod.go_back(to_dir=leg2_dir, wrapper_path=leg2_wrap)
            self.assertEqual(leg2_wrap.read_text(encoding="utf-8"), self.mod.render_wrapper(leg2_dir, leg2_old_store))

            # 8. Foreign journal with an unrelated .rec copy and absent target is refused without publishing it,
            #    and a foreign journal naming a nonexistent external dir creates neither the dir nor a lock file.
            frec_dir = root / "foreign_rec_src"
            frec_dir.mkdir()
            fake_rec = frec_dir / ".helio.abcdef012345.rec"
            fake_rec.write_text("unrelated binary payload\n", encoding="utf-8")
            fake_ext_dir = root / "must_not_be_created_dir"
            (frec_dir / self.mod.JOURNAL_NAME).write_text(
                json.dumps({
                    "schema_version": 2,
                    "op": "install",
                    "phase": "publishing",
                    "tx_id": "abcdef012345",
                    "to_dir": str(frec_dir.resolve()),
                    "wrapper_path": str((root / "frec_w" / "helio").resolve()),
                    "had_manifest": False,
                    "previous_manifest": None,
                    "pre_wrappers": {str(fake_ext_dir / "helio"): str(store)},
                    "saved_files": {"helio": {"existed": True, "rec_name": fake_rec.name, "sha256": self.mod.sha256_file(fake_rec)}},
                    "temp_files": [fake_rec.name],
                    "desired_manifest": None,
                }),
                encoding="utf-8",
            )
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=repo, to_dir=frec_dir, store_dir=store, wrapper_path=root / "frec_w" / "helio", prebuilt=(a_h, a_e))
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("unowned path", str(ctx.exception))
            self.assertFalse((frec_dir / "helio").exists())
            self.assertFalse(fake_ext_dir.exists())

            # Clear pre_wrappers so journal passes validation and hits the independent .rec identity check.
            j_only_rec = json.loads((frec_dir / self.mod.JOURNAL_NAME).read_text(encoding="utf-8"))
            j_only_rec["pre_wrappers"] = {}
            (frec_dir / self.mod.JOURNAL_NAME).write_text(json.dumps(j_only_rec), encoding="utf-8")
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.recover_if_needed(frec_dir, root / "frec_w" / "helio")
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("not a Heliograph artifact", str(ctx.exception))
            self.assertFalse((frec_dir / "helio").exists())

            # 8b. Schema version 1 staging and removal journals still recover losslessly.
            v1_stage_dir = root / "v1_stage_rec"
            v1_stage_dir.mkdir()
            v1_tmp = v1_stage_dir / ".helio.abcdef012345.rec"
            v1_tmp.write_bytes(b"temp")
            (v1_stage_dir / self.mod.JOURNAL_NAME).write_text(
                json.dumps({
                    "schema_version": 1,
                    "op": "install",
                    "phase": "staging",
                    "tx_id": "abcdef012345",
                    "to_dir": str(v1_stage_dir.resolve()),
                    "wrapper_path": str((root / "v1_sw" / "helio").resolve()),
                    "temp_files": [v1_tmp.name],
                }),
                encoding="utf-8",
            )
            self.assertTrue(self.mod.recover_if_needed(v1_stage_dir, root / "v1_sw" / "helio"))
            self.assertFalse(v1_tmp.exists())
            self.assertFalse((v1_stage_dir / self.mod.JOURNAL_NAME).exists())

            v1_rem_dir = root / "v1_rem_rec"
            v1_rem_wrap = root / "v1_rw" / "helio"
            m_v1_rem = self.mod.install(repo=repo, to_dir=v1_rem_dir, store_dir=store, wrapper_path=v1_rem_wrap, prebuilt=(a_h, a_e))
            (v1_rem_dir / self.mod.JOURNAL_NAME).write_text(
                json.dumps({
                    "schema_version": 1,
                    "op": "remove",
                    "phase": "publishing",
                    "tx_id": "abcdef012345",
                    "to_dir": str(v1_rem_dir.resolve()),
                    "wrapper_path": str(v1_rem_wrap.resolve()),
                    "remove_wrappers": [str(v1_rem_wrap.resolve())],
                    "store_dir": "",
                    "had_manifest": True,
                    "previous_manifest": m_v1_rem,
                    "previous_wrapper_existed": True,
                    "saved_files": {},
                    "temp_files": [],
                    "expected_hashes": {
                        "helio": self.mod.sha256_file(v1_rem_dir / "helio"),
                        "helio.exe": self.mod.sha256_file(v1_rem_dir / "helio.exe"),
                        self.mod.MANIFEST_NAME: self.mod.sha256_file(v1_rem_dir / self.mod.MANIFEST_NAME),
                    },
                    "desired_manifest": None,
                }),
                encoding="utf-8",
            )
            self.assertTrue(self.mod.recover_if_needed(v1_rem_dir, v1_rem_wrap))
            self.assertFalse((v1_rem_dir / "helio").exists())
            self.assertFalse(v1_rem_wrap.exists())
            self.assertFalse((v1_rem_dir / self.mod.JOURNAL_NAME).exists())

            # Unrecognized schema version 1 publish journal for install/back is refused without mutating wrappers.
            v1_back_dir = root / "v1_back_rec"
            v1_bw = root / "v1_bw" / "helio"
            v1_bs_old, v1_bs_new = root / "v1_bs_old", root / "v1_bs_new"
            self.mod.install(repo=repo, to_dir=v1_back_dir, store_dir=v1_bs_old, wrapper_path=v1_bw, prebuilt=(a_h, a_e))
            self.mod.install(repo=repo, to_dir=v1_back_dir, store_dir=v1_bs_new, wrapper_path=v1_bw, prebuilt=(b_h, b_e))

            def crash_v1_back(stage: str) -> None:
                if stage == "after_wrapper":
                    raise SimulatedCrash(stage)

            with self.assertRaises(SimulatedCrash):
                self.mod.go_back(to_dir=v1_back_dir, wrapper_path=v1_bw, on_boundary=crash_v1_back)
            j_v1_back = json.loads((v1_back_dir / self.mod.JOURNAL_NAME).read_text(encoding="utf-8"))
            j_v1_back["schema_version"] = 1
            (v1_back_dir / self.mod.JOURNAL_NAME).write_text(json.dumps(j_v1_back), encoding="utf-8")
            before_wrap = v1_bw.read_text(encoding="utf-8")
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.recover_if_needed(v1_back_dir, v1_bw)
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("unrecognized install journal schema", str(ctx.exception))
            self.assertEqual(v1_bw.read_text(encoding="utf-8"), before_wrap)

            # 9. Removing a two-wrapper installation through the CURRENT wrapper (wb) removes BOTH wa and wb.
            rem_cur_dir = root / "rem_via_current_wrap"
            rwa, rwb = root / "rwa" / "helio", root / "rwb" / "helio"
            self.mod.install(repo=repo, to_dir=rem_cur_dir, store_dir=sa, wrapper_path=rwa, prebuilt=(a_h, a_e))
            self.mod.install(repo=repo, to_dir=rem_cur_dir, store_dir=sb, wrapper_path=rwb, prebuilt=(b_h, b_e))
            self.assertTrue(self.mod.remove(to_dir=rem_cur_dir, wrapper_path=rwb))
            self.assertFalse(rwa.exists())
            self.assertFalse(rwb.exists())

    def test_legacy_adoption_symlinks_collisions_and_locks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            init_git_repo(repo)
            store_dir = root / "store"
            a_h, a_e = root / "a_h", root / "a_e"
            b_h, b_e = root / "b_h", root / "b_e"
            make_distinct_binary(self.helio_bin, a_h, b"A")
            make_distinct_binary(self.helio_exe, a_e, b"A")
            make_distinct_binary(self.helio_bin, b_h, b"B")
            make_distinct_binary(self.helio_exe, b_e, b"B")

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
            m_leg = self.mod.install(repo=repo, to_dir=leg_dir, store_dir=store_dir, wrapper_path=leg_wrap, prebuilt=(b_h, b_e))
            self.assertEqual(m_leg["previous"]["commit"], "legacy")
            self.assertEqual(m_leg["previous"]["helio_sha256"], legacy_sha)

            # Unrelated ELF, dangling symlink wrapper, wrapper==to/helio, newline path, credential URL.
            evil_dir = root / "evil_elf"
            evil_dir.mkdir()
            shutil.copy2("/bin/sh", evil_dir / "helio")
            with self.assertRaises(self.mod.InstallError):
                self.mod.install(repo=repo, to_dir=evil_dir, store_dir=store_dir, wrapper_path=root / "w1" / "helio", prebuilt=(b_h, b_e))

            sym_wrap = root / "sym_w" / "helio"
            sym_wrap.parent.mkdir(parents=True)
            sym_wrap.symlink_to(root / "nonexistent" / "target")
            with self.assertRaises(self.mod.InstallError):
                self.mod.install(repo=repo, to_dir=root / "sym_bin", store_dir=store_dir, wrapper_path=sym_wrap, prebuilt=(b_h, b_e))

            col_dir = root / "col_bin"
            with self.assertRaises(self.mod.InstallError):
                self.mod.install(repo=repo, to_dir=col_dir, store_dir=store_dir, wrapper_path=col_dir / "helio", prebuilt=(b_h, b_e))

            with self.assertRaises(self.mod.InstallError):
                self.mod.install(repo=repo, to_dir=root / "bad\nbin", store_dir=store_dir, wrapper_path=root / "nw" / "helio", prebuilt=(b_h, b_e))

            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod._validate_sdk_url("https://alice:supersecret@example.com/sdk.git")
            self.assertNotIn("supersecret", str(ctx.exception))

            dir_a, dir_b = root / "binA", root / "binB"
            wrapper = root / "home_ab" / ".local" / "bin" / "helio"
            dir_a.mkdir()
            with self.mod.acquire_lock(dir_a / self.mod.LOCK_NAME, blocking=False):
                with self.assertRaises(self.mod.InstallError) as ctx:
                    self.mod.install(repo=repo, to_dir=dir_a, store_dir=store_dir, wrapper_path=wrapper, prebuilt=(a_h, a_e))
                self.assertEqual(ctx.exception.code, 2)

            self.mod.install(repo=repo, to_dir=dir_a, store_dir=store_dir, wrapper_path=wrapper, prebuilt=(a_h, a_e))

            def crash_after_helio(stage: str) -> None:
                if stage == "after_helio":
                    raise SimulatedCrash(stage)

            with self.assertRaises(SimulatedCrash):
                self.mod.install(repo=repo, to_dir=dir_a, store_dir=store_dir, wrapper_path=wrapper, prebuilt=(b_h, b_e), on_boundary=crash_after_helio)
            # While dir_a still owns wrapper, dir_b is refused from taking it over.
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=repo, to_dir=dir_b, store_dir=store_dir, wrapper_path=wrapper, prebuilt=(b_h, b_e))
            self.assertEqual(ctx.exception.code, 2)
            # If wrapper is vacated and dir_b claims it, removing dir_a leaves dir_b's wrapper intact.
            wrapper.unlink()
            self.mod.install(repo=repo, to_dir=dir_b, store_dir=store_dir, wrapper_path=wrapper, prebuilt=(b_h, b_e))
            self.mod.remove(to_dir=dir_a, wrapper_path=wrapper)
            self.assertTrue(wrapper.is_file())
            self.assertTrue(self.mod.wrapper_belongs_to(wrapper, dir_b))

            # Concurrent installs to two different destinations targeting one vacant wrapper serialize:
            # exactly one wins the wrapper and the other is refused with exit 2.
            self.mod.remove(to_dir=dir_b, wrapper_path=wrapper)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(self.mod.install, repo=repo, to_dir=dir_a, store_dir=store_dir, wrapper_path=wrapper, prebuilt=(a_h, a_e))
                f2 = pool.submit(self.mod.install, repo=repo, to_dir=dir_b, store_dir=store_dir, wrapper_path=wrapper, prebuilt=(b_h, b_e))
                outcomes = []
                for fut in (f1, f2):
                    try:
                        fut.result()
                        outcomes.append("ok")
                    except self.mod.InstallError as exc:
                        self.assertEqual(exc.code, 2)
                        outcomes.append("refused")
            self.assertEqual(sorted(outcomes), ["ok", "refused"])
            self.assertTrue(self.mod.wrapper_belongs_to(wrapper, dir_a) or self.mod.wrapper_belongs_to(wrapper, dir_b))

    def test_crash_recovery_every_distinct_boundary_two_wrappers_and_store_message(self) -> None:
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

            # Install: every distinct publication boundary + store message readability check.
            for boundary in ("after_journal", "after_backup_helio", "after_backup_exe", "after_helio", "after_helio_exe", "after_wrapper", "after_manifest"):
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
                self.assertTrue(self.mod.recover_if_needed(to_dir, wrapper))
                code, lines = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
                self.assertEqual(code, 0, f"{boundary}: {lines}")
                exp_cur_h, exp_cur_e = (c_h, c_e) if boundary == "after_manifest" else (b_h, b_e)
                exp_old_h, exp_old_e = (b_h, b_e) if boundary == "after_manifest" else (a_h, a_e)
                self.assertEqual(self.mod.sha256_file(to_dir / "helio"), self.mod.sha256_file(exp_cur_h))
                self.assertEqual(self.mod.sha256_file(to_dir / "helio.exe"), self.mod.sha256_file(exp_cur_e))
                self.assertEqual(self.mod.sha256_file(to_dir / "helio.old"), self.mod.sha256_file(exp_old_h))
                self.assertEqual(self.mod.sha256_file(to_dir / "helio.exe.old"), self.mod.sha256_file(exp_old_e))
                chk = subprocess.run([str(to_dir / "helio"), "show", msg_id, "--as", "p/a", "--store", str(store)], capture_output=True, text=True)
                self.assertEqual(chk.returncode, 0)
                self.assertIn("keep-msg", chk.stdout)

            # --back with TWO DISTINCT WRAPPERS (wrap_a != wrap_b) across every boundary including after_target_wrapper.
            for boundary in ("after_journal", "after_backup_helio", "after_backup_exe", "after_helio", "after_helio_exe", "after_target_wrapper", "after_wrapper", "after_manifest"):
                to_dir = root / f"back_{boundary}"
                store_a, store_b = root / f"bsa_{boundary}", root / f"bsb_{boundary}"
                wrap_a = root / f"wa_back_{boundary}" / "helio"
                wrap_b = root / f"wb_back_{boundary}" / "helio-alt"
                self.mod.install(repo=repo, to_dir=to_dir, store_dir=store_a, wrapper_path=wrap_a, prebuilt=(a_h, a_e))
                self.mod.install(repo=repo, to_dir=to_dir, store_dir=store_b, wrapper_path=wrap_b, prebuilt=(b_h, b_e))

                def fail_at(stage: str, target=boundary) -> None:
                    if stage == target:
                        raise SimulatedCrash(stage)

                with self.assertRaises(SimulatedCrash):
                    self.mod.go_back(to_dir=to_dir, wrapper_path=wrap_b, on_boundary=fail_at)
                self.assertTrue(self.mod.recover_if_needed(to_dir, wrap_b))
                active_wrap = wrap_a if boundary == "after_manifest" else wrap_b
                code, lines = self.mod.status(to_dir=to_dir, wrapper_path=active_wrap)
                self.assertEqual(code, 0, f"back {boundary}: {lines}")
                # Verify wrap_a keeps store_a (never overwritten with store_b!) and wrap_b keeps store_b when rolled back.
                self.assertEqual(wrap_a.read_text(encoding="utf-8"), self.mod.render_wrapper(to_dir, store_a))
                if boundary == "after_manifest":
                    self.assertFalse(wrap_b.exists())
                else:
                    self.assertEqual(wrap_b.read_text(encoding="utf-8"), self.mod.render_wrapper(to_dir, store_b))
                exp_cur_h, exp_cur_e = (a_h, a_e) if boundary == "after_manifest" else (b_h, b_e)
                exp_old_h, exp_old_e = (b_h, b_e) if boundary == "after_manifest" else (a_h, a_e)
                self.assertEqual(self.mod.sha256_file(to_dir / "helio"), self.mod.sha256_file(exp_cur_h))
                self.assertEqual(self.mod.sha256_file(to_dir / "helio.exe"), self.mod.sha256_file(exp_cur_e))
                self.assertEqual(self.mod.sha256_file(to_dir / "helio.old"), self.mod.sha256_file(exp_old_h))
                self.assertEqual(self.mod.sha256_file(to_dir / "helio.exe.old"), self.mod.sha256_file(exp_old_e))

            # --remove on a TWO-GENERATION, TWO-WRAPPER installation across EVERY individual unlink boundary.
            rem_boundaries = (
                "after_journal",
                "after_invocation_wrapper",
                "after_wrapper",
                "after_unlink_helio",
                "after_unlink_helio_exe",
                "after_unlink_helio_old",
                "after_unlink_helio_exe_old",
                "after_manifest",
            )
            for boundary in rem_boundaries:
                to_dir = root / f"rem2_{boundary}"
                store = root / f"rstore2_{boundary}"
                wrap_inv = root / f"w_rem2_inv_{boundary}" / "helio"
                wrap_rec = root / f"w_rem2_rec_{boundary}" / "helio-rec"
                self.mod.install(repo=repo, to_dir=to_dir, store_dir=store, wrapper_path=wrap_inv, prebuilt=(a_h, a_e))
                self.mod.install(repo=repo, to_dir=to_dir, store_dir=store, wrapper_path=wrap_rec, prebuilt=(b_h, b_e))

                def fail_at(stage: str, target=boundary) -> None:
                    if stage == target:
                        raise SimulatedCrash(stage)

                with self.assertRaises(SimulatedCrash):
                    self.mod.remove(to_dir=to_dir, wrapper_path=wrap_inv, on_boundary=fail_at)
                self.assertTrue(self.mod.recover_if_needed(to_dir, wrap_inv))
                for name in self.mod.BINARY_NAMES + self.mod.BACKUP_NAMES + (self.mod.MANIFEST_NAME,):
                    self.assertFalse((to_dir / name).exists())
                self.assertFalse(wrap_inv.exists())
                self.assertFalse(wrap_rec.exists())
                code, _ = self.mod.status(to_dir=to_dir, wrapper_path=wrap_rec)
                self.assertEqual(code, 1)

    def test_zero_gap_posix_replacement_and_busy_deferral(self) -> None:
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

            stop = threading.Event()
            failures: list[str] = []
            completed = 0

            def caller():
                nonlocal completed
                while not stop.is_set():
                    try:
                        res = subprocess.run([str(to_dir / "helio"), "help"], capture_output=True, text=True)
                        if res.returncode != 0:
                            failures.append(f"rc={res.returncode}")
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

    def test_held_windows_exe_concurrent_caller_during_publication(self) -> None:
        import platform

        is_wsl = "microsoft" in platform.release().lower()
        if not (shutil.which("python.exe") and shutil.which("wslpath")):
            if is_wsl:
                self.fail("WSL detected but python.exe or wslpath is unavailable for required Windows replacement proof")
            self.skipTest("WSL Windows interop (python.exe/wslpath) not available on non-WSL host")
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
            win_exe = subprocess.run(["wslpath", "-w", str(to_dir / "helio.exe")], capture_output=True, text=True, check=True).stdout.strip()
            holder = subprocess.Popen(
                ["python.exe", "-c", f"import time; f = open({win_exe!r}, 'rb'); print('HELD', flush=True); time.sleep(10)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            win_stop = threading.Event()
            win_failures: list[str] = []
            bg_calls = 0

            def win_caller():
                nonlocal bg_calls
                while not win_stop.is_set():
                    proc = None
                    try:
                        proc = subprocess.Popen(
                            [str(to_dir / "helio.exe"), "help"],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                        )
                        try:
                            _out, err = proc.communicate(timeout=30)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.communicate()
                            win_failures.append("helio.exe timed out")
                            continue
                        if proc.returncode != 0:
                            win_failures.append(f"rc={proc.returncode} stderr={err}")
                        else:
                            bg_calls += 1
                    except (OSError, subprocess.SubprocessError) as exc:
                        if proc is not None and proc.poll() is None:
                            proc.kill()
                            proc.communicate()
                        win_failures.append(f"exc={exc}")

            pinned_proc: list[subprocess.Popen[str]] = []
            pinned_alive_after: list[bool] = [False]

            def wait_for_overlap(stage: str) -> None:
                if stage == "after_helio":
                    # Launch a live helio.exe process blocked on stdin BEFORE replace_binary_atomic(stage_exe, helio.exe),
                    # proving it is genuinely executing across the replacement boundary itself.
                    win_store = subprocess.run(["wslpath", "-w", str(store)], capture_output=True, text=True, check=True).stdout.strip()
                    p = subprocess.Popen(
                        [str(to_dir / "helio.exe"), "tell", "demo/lane", "overlap-proof", "--as", "demo/sender", "--store", win_store],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    pinned_proc.append(p)
                    self.assertIsNone(p.poll(), "overlapping helio.exe exited before replacement started")
                elif stage == "after_helio_exe":
                    if pinned_proc:
                        pinned_alive_after[0] = pinned_proc[0].poll() is None
                    # Verify a fresh caller can open and execute helio.exe by name immediately after replacement.
                    after_r = subprocess.run(
                        [str(to_dir / "helio.exe"), "help"],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if after_r.returncode != 0:
                        win_failures.append(f"post-replace helio.exe rc={after_r.returncode} stderr={after_r.stderr}")

            wt = threading.Thread(target=win_caller)
            exe_target = (to_dir / "helio.exe").resolve()
            real_os_unlink = os.unlink
            real_os_remove = os.remove
            real_os_rename = os.rename
            real_os_replace = os.replace
            saw_atomic_replace = False

            def _probe_exe_during_gap(op_name: str, path_arg: object) -> None:
                if Path(os.fspath(path_arg)).resolve() == exe_target:
                    # Non-atomic removal/rename-away of helio.exe anywhere in install() creates a gap; probe by name right now.
                    try:
                        r = subprocess.run(
                            [str(to_dir / "helio.exe"), "help"],
                            stdin=subprocess.DEVNULL,
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        detail = f"probe rc={r.returncode}"
                    except OSError as exc:
                        detail = f"probe raised {type(exc).__name__}: {exc}"
                    win_failures.append(f"non-atomic {op_name} of helio.exe during install ({detail})")

            def _spy_unlink(p, *args, **kwargs):
                res = real_os_unlink(p, *args, **kwargs)
                _probe_exe_during_gap("unlink", p)
                return res

            def _spy_remove(p, *args, **kwargs):
                res = real_os_remove(p, *args, **kwargs)
                _probe_exe_during_gap("remove", p)
                return res

            def _spy_rename(src, dst, *args, **kwargs):
                res = real_os_rename(src, dst, *args, **kwargs)
                _probe_exe_during_gap("rename", src)
                return res

            def _spy_replace(src, dst, *args, **kwargs):
                nonlocal saw_atomic_replace
                is_exe_dst = Path(os.fspath(dst)).resolve() == exe_target
                if is_exe_dst:
                    saw_atomic_replace = True
                res = real_os_replace(src, dst, *args, **kwargs)
                if not is_exe_dst:
                    _probe_exe_during_gap("replace-away", src)
                return res

            try:
                assert holder.stdout is not None
                self.assertEqual(holder.stdout.readline().strip(), "HELD")
                wt.start()
                try:
                    with (
                        mock.patch("os.unlink", side_effect=_spy_unlink),
                        mock.patch("os.remove", side_effect=_spy_remove),
                        mock.patch("os.rename", side_effect=_spy_rename),
                        mock.patch("os.replace", side_effect=_spy_replace),
                    ):
                        self.mod.install(
                            repo=repo,
                            to_dir=to_dir,
                            store_dir=store,
                            wrapper_path=wrapper,
                            prebuilt=(b_h, b_e),
                            on_boundary=wait_for_overlap,
                        )
                except self.mod.BusyExecutableError as exc:
                    self.assertEqual(exc.code, 3)
                    if pinned_proc:
                        pinned_alive_after[0] = pinned_proc[0].poll() is None
                finally:
                    if pinned_proc:
                        p = pinned_proc[0]
                        try:
                            _o, err = p.communicate(input="overlap body\n", timeout=30)
                        except subprocess.TimeoutExpired:
                            p.kill()
                            p.communicate()
                            win_failures.append("pinned overlapping helio.exe timed out")
                        else:
                            if p.returncode != 0:
                                win_failures.append(f"pinned helio.exe rc={p.returncode} stderr={err}")
            finally:
                win_stop.set()
                wt.join(timeout=35)
                holder.terminate()
                holder.wait(timeout=10)
                if holder.stdout:
                    holder.stdout.close()
                if holder.stderr:
                    holder.stderr.close()
            self.assertEqual(win_failures, [])
            self.assertTrue(saw_atomic_replace, "helio.exe replacement never executed")
            self.assertTrue(pinned_alive_after[0], "overlapping helio.exe did not remain live across replacement")
            self.assertGreater(bg_calls, 0)
            code, _ = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(code, 0)

    def test_chain2_hardening_cross_dest_status_drift_partial_legacy_oserror_and_clean_home(self) -> None:
        import errno as errno_mod
        import io
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            init_git_repo(repo)
            a_h, a_e = root / "a_h", root / "a_e"
            b_h, b_e = root / "b_h", root / "b_e"
            make_distinct_binary(self.helio_bin, a_h, b"A")
            make_distinct_binary(self.helio_exe, a_e, b"A")
            make_distinct_binary(self.helio_bin, b_h, b"B")
            make_distinct_binary(self.helio_exe, b_e, b"B")

            # 1. Cross-destination wrapper takeover is refused before mutating anything.
            dir1, dir2 = root / "dest1", root / "dest2"
            s1, s2 = root / "store1", root / "store2"
            shared_wrap = root / "shared_bin" / "helio"
            self.mod.install(repo=repo, to_dir=dir1, store_dir=s1, wrapper_path=shared_wrap, prebuilt=(a_h, a_e))
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=repo, to_dir=dir2, store_dir=s2, wrapper_path=shared_wrap, prebuilt=(b_h, b_e))
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn(str(dir1.resolve()), str(ctx.exception))
            self.assertEqual(shared_wrap.read_text(encoding="utf-8"), self.mod.render_wrapper(dir1, s1))
            chk1 = subprocess.run([str(shared_wrap), "check"], capture_output=True, text=True)
            self.assertEqual(chk1.returncode, 0, chk1.stderr)

            # 2. --status checks exact wrapper store routing, active wrapper path, and previous backup hashes.
            self.mod.install(repo=repo, to_dir=dir1, store_dir=s2, wrapper_path=shared_wrap, prebuilt=(b_h, b_e))
            code, _ = self.mod.status(to_dir=dir1, wrapper_path=shared_wrap)
            self.assertEqual(code, 0)

            # 2a. Wrapper content edited to point at wrong store while keeping target comment.
            orig_wrap = shared_wrap.read_text(encoding="utf-8")
            shared_wrap.write_text(self.mod.render_wrapper(dir1, s1), encoding="utf-8")
            os.chmod(shared_wrap, 0o755)
            code, lines = self.mod.status(to_dir=dir1, wrapper_path=shared_wrap)
            self.assertEqual(code, 3)
            self.assertTrue(any(str(shared_wrap) in line for line in lines), lines)
            shared_wrap.write_text(orig_wrap, encoding="utf-8")
            os.chmod(shared_wrap, 0o755)

            # 2b. Status invoked with a different wrapper path than recorded active wrapper.
            other_wrap = root / "other_bin" / "helio"
            other_wrap.parent.mkdir(parents=True, exist_ok=True)
            other_wrap.write_text(orig_wrap, encoding="utf-8")
            os.chmod(other_wrap, 0o755)
            code, lines = self.mod.status(to_dir=dir1, wrapper_path=other_wrap)
            self.assertEqual(code, 3)
            self.assertTrue(any(str(other_wrap) in line for line in lines), lines)

            # 2c. Corrupt helio.old fails status with exit 3 naming helio.old.
            old_h_bytes = (dir1 / "helio.old").read_bytes()
            (dir1 / "helio.old").write_bytes(b"corrupt-old")
            code, lines = self.mod.status(to_dir=dir1, wrapper_path=shared_wrap)
            self.assertEqual(code, 3)
            self.assertTrue(any("helio.old" in line for line in lines), lines)
            (dir1 / "helio.old").write_bytes(old_h_bytes)

            # 2d. Missing helio.exe.old fails status with exit 3 naming helio.exe.old.
            old_e_bytes = (dir1 / "helio.exe.old").read_bytes()
            (dir1 / "helio.exe.old").unlink()
            code, lines = self.mod.status(to_dir=dir1, wrapper_path=shared_wrap)
            self.assertEqual(code, 3)
            self.assertTrue(any("helio.exe.old" in line for line in lines), lines)
            (dir1 / "helio.exe.old").write_bytes(old_e_bytes)
            code, _ = self.mod.status(to_dir=dir1, wrapper_path=shared_wrap)
            self.assertEqual(code, 0)

            # 3. Build-time source drift refuses publication with exit 3 and installs nothing.
            drift_ws = root / "drift_ws"
            drift_repo = drift_ws / "Heliograph"
            init_git_repo(drift_repo)
            drift_sdk = drift_ws / "ImpossibleSDK" / "tools"
            drift_sdk.mkdir(parents=True)
            (drift_sdk / "app_build.py").write_text(
                "import pathlib, shutil, sys\n"
                "repo = pathlib.Path(sys.argv[1])\n"
                "(repo / 'README.md').write_text('drifted during build\\n', encoding='utf-8')\n"
                "(repo / 'build' / 'linux' / 'linux').mkdir(parents=True, exist_ok=True)\n"
                f"shutil.copy2({str(a_h)!r}, repo / 'build' / 'linux' / 'linux' / 'helio')\n"
                f"shutil.copy2({str(a_e)!r}, repo / 'build' / 'linux' / 'helio.exe')\n",
                encoding="utf-8",
            )
            drift_to = root / "drift_bin"
            drift_wrap = root / "drift_wrap" / "helio"
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=drift_repo, to_dir=drift_to, store_dir=root / "drift_store", wrapper_path=drift_wrap)
            self.assertEqual(ctx.exception.code, 3)
            self.assertIn("source tree changed", str(ctx.exception))
            self.assertFalse((drift_to / "helio").exists())

            # 4. Partial legacy pair is refused in both directions, naming the missing partner and preserving bytes.
            part_h_dir = root / "partial_helio_only"
            part_h_dir.mkdir()
            shutil.copy2(a_h, part_h_dir / "helio")
            h_before = (part_h_dir / "helio").read_bytes()
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=repo, to_dir=part_h_dir, store_dir=s1, wrapper_path=root / "pw1" / "helio", prebuilt=(b_h, b_e))
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("helio.exe", str(ctx.exception))
            self.assertEqual((part_h_dir / "helio").read_bytes(), h_before)

            part_e_dir = root / "partial_exe_only"
            part_e_dir.mkdir()
            shutil.copy2(a_e, part_e_dir / "helio.exe")
            e_before = (part_e_dir / "helio.exe").read_bytes()
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=repo, to_dir=part_e_dir, store_dir=s1, wrapper_path=root / "pw2" / "helio", prebuilt=(b_h, b_e))
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("helio", str(ctx.exception))
            self.assertEqual((part_e_dir / "helio.exe").read_bytes(), e_before)

            # 5. Ordinary OSError during publication gives exit 3 with structured stderr (no traceback), recovers, and retries cleanly.
            oserr_dir = root / "oserr_dir"
            oserr_wrap = root / "oserr_wrap" / "helio"
            self.mod.install(repo=repo, to_dir=oserr_dir, store_dir=s1, wrapper_path=oserr_wrap, prebuilt=(a_h, a_e))

            def fail_enospc(path: Path, data: bytes, mode: int = 0o644) -> None:
                raise OSError(errno_mod.ENOSPC, "No space left on device", str(path))

            stderr_buf = io.StringIO()
            with mock.patch.object(self.mod, "write_bytes_atomic", side_effect=fail_enospc):
                with mock.patch.object(self.mod, "ensure_sdk_and_build", return_value=(b_h, b_e, "a" * 40, False)):
                    with mock.patch("sys.stderr", stderr_buf):
                        rc = self.mod.main([
                            "--to", str(oserr_dir),
                            "--store", str(s2),
                            "--wrapper", str(oserr_wrap),
                        ])
            self.assertEqual(rc, 3)
            self.assertNotIn("Traceback", stderr_buf.getvalue())
            self.assertIn("No space left on device", stderr_buf.getvalue())
            # Retry after disk condition clears succeeds cleanly.
            self.mod.install(repo=repo, to_dir=oserr_dir, store_dir=s2, wrapper_path=oserr_wrap, prebuilt=(b_h, b_e))
            code, _ = self.mod.status(to_dir=oserr_dir, wrapper_path=oserr_wrap)
            self.assertEqual(code, 0)

            # 6. Clean home with system login profile finds `helio` by name in one fresh login shell
            #    without test-injected PATH, verifying wrapper identity and unset HELIO_STORE BEFORE any write.
            clean_home = root / "clean_home"
            clean_home.mkdir()
            skel_profile = Path("/etc/skel/.profile")
            if skel_profile.is_file():
                shutil.copy2(skel_profile, clean_home / ".profile")
            clean_to = root / "clean_to"
            clean_store = root / "clean_store"
            clean_wrap = clean_home / ".local" / "bin" / "helio"
            self.mod.install(repo=repo, to_dir=clean_to, store_dir=clean_store, wrapper_path=clean_wrap, prebuilt=(a_h, a_e))
            expected_wrap = str(clean_wrap.resolve())
            before_msgs = set((clean_store / "messages").iterdir())
            login_script = (
                'resolved="$(command -v helio)" || exit 11; '
                '[ "$resolved" = "$1" ] || exit 12; '
                '[ -z "${HELIO_STORE:-}" ] || exit 13; '
                'helio check && helio tell demo/lane clean-home-msg --as demo/sender <<< body && helio inbox --as demo/lane'
            )
            login_res = subprocess.run(
                ["env", "-i", f"HOME={clean_home}", "PATH=/usr/bin:/bin", "bash", "-lc", login_script, "_", expected_wrap],
                capture_output=True,
                text=True,
            )
            self.assertEqual(login_res.returncode, 0, login_res.stdout + login_res.stderr)
            self.assertIn("clean-home-msg", login_res.stdout)
            after_msgs = set((clean_store / "messages").iterdir())
            self.assertGreater(len(after_msgs), len(before_msgs), "message was not written to temporary clean_store")

            # 7. Round 1 regressions: stale .restore cleanup, cross-destination legacy wrapper refusal,
            #    SDK URL query redaction, CRLF wrapper rejection, --back missing store recovery,
            #    already-dirty source drift, and stray helio.old with previous=None.
            # 7a. Stale .restore file from interrupted recovery does not cause EEXIST and is cleaned up.
            rest_dir = root / "stale_restore"
            rest_wrap = root / "stale_restore_w" / "helio"
            self.mod.install(repo=repo, to_dir=rest_dir, store_dir=s1, wrapper_path=rest_wrap, prebuilt=(a_h, a_e))

            def crash_after_helio_r(stage: str) -> None:
                if stage == "after_helio":
                    raise SimulatedCrash(stage)

            with self.assertRaises(SimulatedCrash):
                self.mod.install(repo=repo, to_dir=rest_dir, store_dir=s2, wrapper_path=rest_wrap, prebuilt=(b_h, b_e), on_boundary=crash_after_helio_r)
            j_rest = json.loads((rest_dir / self.mod.JOURNAL_NAME).read_text(encoding="utf-8"))
            stale_restore = rest_dir / f".helio.{j_rest['tx_id']}.restore"
            stale_restore.write_bytes(b"interrupted-restore-temp")
            self.assertTrue(self.mod.recover_if_needed(rest_dir, rest_wrap))
            self.assertFalse(stale_restore.exists())

            # 7b. Cross-destination legacy wrapper is refused without overwriting it.
            leg_other_dir = root / "leg_other_dest"
            leg_other_dir.mkdir()
            leg_wrap = root / "leg_shared_wrap" / "helio"
            leg_wrap.parent.mkdir(parents=True, exist_ok=True)
            leg_wrap.write_text(
                f"#!/bin/sh\n# Heliograph with this machine's store.\n"
                f'HELIO_STORE="${{HELIO_STORE:-{s1}}}" exec "{leg_other_dir}/helio" "$@"\n',
                encoding="utf-8",
            )
            os.chmod(leg_wrap, 0o755)
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=repo, to_dir=root / "leg_new_dest", store_dir=s2, wrapper_path=leg_wrap, prebuilt=(a_h, a_e))
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn(str(leg_other_dir.resolve()), str(ctx.exception))

            # 7c. SDK URL with query token is refused and redacted.
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod._validate_sdk_url("https://example.invalid/sdk?token=SUPERSECRET")
            self.assertEqual(ctx.exception.code, 2)
            self.assertNotIn("SUPERSECRET", str(ctx.exception))

            # 7d. CRLF-edited wrapper fails status with exit 3.
            crlf_dir = root / "crlf_dir"
            crlf_wrap = root / "crlf_w" / "helio"
            self.mod.install(repo=repo, to_dir=crlf_dir, store_dir=s1, wrapper_path=crlf_wrap, prebuilt=(a_h, a_e))
            crlf_bytes = crlf_wrap.read_bytes().replace(b"\n", b"\r\n")
            crlf_wrap.write_bytes(crlf_bytes)
            os.chmod(crlf_wrap, 0o755)
            code, lines = self.mod.status(to_dir=crlf_dir, wrapper_path=crlf_wrap)
            self.assertEqual(code, 3, lines)

            # 7e. Stray helio.old when manifest records previous=None fails status with exit 3 naming helio.old.
            stray_dir = root / "stray_old_dir"
            stray_wrap = root / "stray_old_w" / "helio"
            self.mod.install(repo=repo, to_dir=stray_dir, store_dir=s1, wrapper_path=stray_wrap, prebuilt=(a_h, a_e))
            shutil.copy2(a_h, stray_dir / "helio.old")
            code, lines = self.mod.status(to_dir=stray_dir, wrapper_path=stray_wrap)
            self.assertEqual(code, 3)
            self.assertTrue(any("helio.old" in line for line in lines), lines)

            # 7f. --back when previous generation's store was removed re-initializes it so immediate status returns 0.
            back_store_dir = root / "back_missing_store"
            back_wrap = root / "back_ms_w" / "helio"
            ms1, ms2 = root / "ms_store1", root / "ms_store2"
            self.mod.install(repo=repo, to_dir=back_store_dir, store_dir=ms1, wrapper_path=back_wrap, prebuilt=(a_h, a_e))
            self.mod.install(repo=repo, to_dir=back_store_dir, store_dir=ms2, wrapper_path=back_wrap, prebuilt=(b_h, b_e))
            shutil.rmtree(ms1)
            self.mod.go_back(to_dir=back_store_dir, wrapper_path=back_wrap)
            code, lines = self.mod.status(to_dir=back_store_dir, wrapper_path=back_wrap)
            self.assertEqual(code, 0, lines)

            # 7g. Already-dirty tracked file edited AGAIN during build is refused with exit 3.
            dirty_ws = root / "dirty_drift_ws"
            dirty_repo = dirty_ws / "Heliograph"
            init_git_repo(dirty_repo)
            (dirty_repo / "README.md").write_text("already dirty before build\n", encoding="utf-8")
            dirty_sdk = dirty_ws / "ImpossibleSDK" / "tools"
            dirty_sdk.mkdir(parents=True)
            (dirty_sdk / "app_build.py").write_text(
                "import pathlib, shutil, sys\n"
                "repo = pathlib.Path(sys.argv[1])\n"
                "(repo / 'README.md').write_text('edited again during build\\n', encoding='utf-8')\n"
                "(repo / 'build' / 'linux' / 'linux').mkdir(parents=True, exist_ok=True)\n"
                f"shutil.copy2({str(a_h)!r}, repo / 'build' / 'linux' / 'linux' / 'helio')\n"
                f"shutil.copy2({str(a_e)!r}, repo / 'build' / 'linux' / 'helio.exe')\n",
                encoding="utf-8",
            )
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=dirty_repo, to_dir=root / "dd_bin", store_dir=root / "dd_store", wrapper_path=root / "dd_w" / "helio")
            self.assertEqual(ctx.exception.code, 3)
            self.assertIn("source tree changed", str(ctx.exception))

            # 7h. Untracked symlink whose external target changes during build is also refused with exit 3.
            sym_ws = root / "sym_drift_ws"
            sym_repo = sym_ws / "Heliograph"
            init_git_repo(sym_repo)
            ext_hdr = root / "external_header.h"
            ext_hdr.write_text("#define V 1\n", encoding="utf-8")
            (sym_repo / "linked.h").symlink_to(ext_hdr)
            sym_sdk = sym_ws / "ImpossibleSDK" / "tools"
            sym_sdk.mkdir(parents=True)
            (sym_sdk / "app_build.py").write_text(
                "import pathlib, shutil, sys\n"
                "repo = pathlib.Path(sys.argv[1])\n"
                f"pathlib.Path({str(ext_hdr)!r}).write_text('#define V 2\\n', encoding='utf-8')\n"
                "(repo / 'build' / 'linux' / 'linux').mkdir(parents=True, exist_ok=True)\n"
                f"shutil.copy2({str(a_h)!r}, repo / 'build' / 'linux' / 'linux' / 'helio')\n"
                f"shutil.copy2({str(a_e)!r}, repo / 'build' / 'linux' / 'helio.exe')\n",
                encoding="utf-8",
            )
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=sym_repo, to_dir=root / "sym_bin", store_dir=root / "sym_store", wrapper_path=root / "sym_w" / "helio")
            self.assertEqual(ctx.exception.code, 3)
            self.assertIn("source tree changed", str(ctx.exception))

            # 7i. Unreadable untracked file refuses provenance verification with exit 3 instead of matching a constant.
            if os.geteuid() != 0:
                unr_repo = root / "unr_repo"
                init_git_repo(unr_repo)
                unr_file = unr_repo / "secret_unreadable.h"
                unr_file.write_text("#define X 1\n", encoding="utf-8")
                os.chmod(unr_file, 0o000)
                try:
                    with self.assertRaises(self.mod.InstallError) as ctx:
                        self.mod.git_provenance_snapshot(unr_repo)
                    self.assertEqual(ctx.exception.code, 3)
                    self.assertIn("cannot read build input", str(ctx.exception))
                finally:
                    os.chmod(unr_file, 0o644)

    def test_real_checkout_pinned_sdk_install_and_roundtrip(self) -> None:
        if os.environ.get("HELIO_INSTALL_BUILD_ACTIVE") == "1":
            self.skipTest("already inside tools/install.py build; skipping nested self-install")
        sdk_build = REPO.parent / "ImpossibleSDK" / "tools" / "app_build.py"
        if not sdk_build.is_file():
            self.skipTest("pinned ImpossibleSDK checkout not present beside repo")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            to_dir = root / "real_bin"
            store_dir = root / "real_store"
            wrapper = root / "real_home" / ".local" / "bin" / "helio"
            env = dict(os.environ)
            env["HELIO_NO_BOSUN"] = "1"
            res = subprocess.run(
                [
                    sys.executable,
                    str(INSTALL_SCRIPT),
                    "--to", str(to_dir),
                    "--store", str(store_dir),
                    "--wrapper", str(wrapper),
                ],
                cwd=str(REPO),
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertEqual(self.mod.sha256_file(to_dir / "helio"), self.mod.sha256_file(self.helio_bin))
            self.assertEqual(self.mod.sha256_file(to_dir / "helio.exe"), self.mod.sha256_file(self.helio_exe))
            # Real write + read + check roundtrip through the installed wrapper.
            tell = subprocess.run(
                [str(wrapper), "tell", "demo/lane", "real-sdk-install-check", "--as", "demo/sender"],
                input="hello from real pinned SDK install\n",
                capture_output=True,
                text=True,
            )
            self.assertEqual(tell.returncode, 0, tell.stderr)
            inbox = subprocess.run(
                [str(wrapper), "inbox", "--as", "demo/lane"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(inbox.returncode, 0, inbox.stderr)
            self.assertIn("real-sdk-install-check", inbox.stdout)
            chk = subprocess.run([str(wrapper), "check"], capture_output=True, text=True)
            self.assertEqual(chk.returncode, 0, chk.stderr)

    def test_windows_handoff_from_wsl_shared_store_and_failed_build(self) -> None:
        import platform
        import uuid

        is_wsl = "microsoft" in platform.release().lower()
        if not (shutil.which("python.exe") and shutil.which("wslpath")):
            if is_wsl:
                self.fail("WSL detected but python.exe or wslpath unavailable for --windows handoff test")
            self.skipTest("WSL Windows interop not available on non-WSL host")

        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("install.py --windows", readme)
        self.assertIn("install.py --windows --remove", readme)

        reg_key = f"Software\\HeliographTest\\wsl_{uuid.uuid4().hex[:12]}"
        win_tmp_raw = subprocess.run(
            ["python.exe", "-c", "import tempfile; print(tempfile.mkdtemp(prefix='helio_win_wsl_'))"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        win_to = win_tmp_raw + "\\Heliograph"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                wsl_store = root / "wsl store"
                init_r = subprocess.run([str(self.helio_bin), "init", "--store", str(wsl_store)], capture_output=True, text=True)
                self.assertEqual(init_r.returncode, 0, init_r.stderr)
                msg_subject = f"wsl-to-win-{uuid.uuid4().hex[:8]}"
                tell_r = subprocess.run(
                    [str(self.helio_bin), "tell", "demo/win", msg_subject, "--as", "demo/wsl", "--store", str(wsl_store)],
                    input="message sent from WSL\n",
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(tell_r.returncode, 0, tell_r.stderr)

                # Install Windows session from WSL via the REAL build+store-init handoff (no prebuilt_exe!).
                ok_ws = root / "ok_ws"
                ok_repo = ok_ws / "Heliograph"
                init_git_repo(ok_repo)
                ok_sdk = ok_ws / "ImpossibleSDK" / "tools"
                ok_sdk.mkdir(parents=True)
                (ok_sdk / "app_build.py").write_text(
                    "import pathlib, shutil, sys\n"
                    "repo = pathlib.Path(sys.argv[1])\n"
                    "(repo / 'build' / 'linux' / 'linux').mkdir(parents=True, exist_ok=True)\n"
                    f"shutil.copy2({str(self.helio_bin)!r}, repo / 'build' / 'linux' / 'linux' / 'helio')\n"
                    f"shutil.copy2({str(self.helio_exe)!r}, repo / 'build' / 'linux' / 'helio.exe')\n",
                    encoding="utf-8",
                )
                # Documented NO-OVERRIDE `--windows` install (store_raw=None -> <repo.parent>/Heliograph-signals)
                # initializes the default WSL store itself (NOT pre-initialized!), then reads back a WSL message!
                def_wsl_store = ok_repo.parent / "Heliograph-signals"
                self.assertFalse(def_wsl_store.exists())
                rc_def = self.mod.run_windows_from_wsl(
                    repo=ok_repo,
                    mode="install",
                    win_to_raw=win_to,
                    store_raw=None,
                    reg_key=reg_key,
                )
                self.assertEqual(rc_def, 0)
                self.assertTrue((def_wsl_store / "messages").is_dir())
                def_subj = f"wsl-default-{uuid.uuid4().hex[:8]}"
                subprocess.run(
                    [str(self.helio_bin), "tell", "demo/win", def_subj, "--as", "demo/wsl", "--store", str(def_wsl_store)],
                    input="default store message from WSL\n",
                    capture_output=True,
                    text=True,
                    check=True,
                )

                # Upgrade to explicit wsl_store (second generation -> enables CLI `--windows --back` proof below!).
                rc = self.mod.run_windows_from_wsl(
                    repo=ok_repo,
                    mode="install",
                    win_to_raw=win_to,
                    store_raw=str(wsl_store),
                    reg_key=reg_key,
                )
                self.assertEqual(rc, 0)

                # Explicit new Windows --store directory is initialized automatically on Windows and works on first use!
                new_win_store = win_tmp_raw + "\\BrandNewWinStore"
                win_to_new = win_tmp_raw + "\\HeliographNewStore"
                rc_new = self.mod.run_windows_from_wsl(
                    repo=ok_repo,
                    mode="install",
                    win_to_raw=win_to_new,
                    store_raw=new_win_store,
                    reg_key=reg_key,
                )
                self.assertEqual(rc_new, 0)
                win_store_chk = subprocess.run(
                    [
                        "python.exe",
                        "-c",
                        (
                            "import os, subprocess; "
                            f"r=subprocess.run([{win_to_new + '\\\\helio.cmd'!r}, 'check'], env={{k:v for k,v in os.environ.items() if k!='HELIO_STORE'}}, "
                            "cwd=os.environ.get('TEMP', 'C:\\\\Windows\\\\Temp'), capture_output=True, text=True); "
                            "print(r.stdout + r.stderr); raise SystemExit(r.returncode)"
                        ),
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(win_store_chk.returncode, 0, win_store_chk.stdout + win_store_chk.stderr)
                self.assertEqual(
                    self.mod.run_windows_from_wsl(repo=ok_repo, mode="remove", win_to_raw=win_to_new, store_raw=None, reg_key=reg_key),
                    0,
                )

                # wslpath failure returns exit 3 cleanly from main() instead of uncaught WinInstallError.
                import io
                stderr_buf = io.StringIO()
                with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess(["wslpath"], 1, "", "bad path")):
                    with mock.patch("sys.stderr", stderr_buf):
                        rc_wp = self.mod.main(["--windows", "--status"])
                self.assertEqual(rc_wp, 3)
                self.assertIn("wslpath", stderr_buf.getvalue())

                # Bare `helio` in BOTH Windows cmd.exe and PowerShell (with PATH derived from HKCU\<reg_key>
                # appended to system PATH in real Windows user-PATH order) resolves to <win_to>\helio.cmd
                # AND reads back the message sent from WSL!
                expected_cmd = win_to + "\\helio.cmd"
                shell_probe = subprocess.run(
                    [
                        "python.exe",
                        "-c",
                        (
                            "import os, pathlib, shutil, subprocess, winreg; "
                            f"k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, {reg_key!r}); "
                            "reg_path, _ = winreg.QueryValueEx(k, 'Path'); k.Close(); "
                            "env = dict(os.environ); env.pop('HELIO_STORE', None); "
                            "clean_sys = [d for d in env.get('PATH', '').split(';') if d and not any((pathlib.Path(d)/n).is_file() for n in ('helio','helio.cmd','helio.exe'))]; "
                            "env['PATH'] = ';'.join(clean_sys) + ';' + reg_path; "
                            "cwd = os.environ.get('TEMP', 'C:\\\\Windows\\\\Temp'); "
                            "w_cmd = subprocess.run(['cmd.exe', '/c', 'where', 'helio'], cwd=cwd, env=env, capture_output=True, text=True); "
                            "r_cmd = subprocess.run(['cmd.exe', '/c', 'helio', 'inbox', '--as', 'demo/win'], "
                            "cwd=cwd, env=env, capture_output=True, text=True); "
                            "ps = shutil.which('powershell.exe'); assert ps, 'powershell.exe missing'; "
                            "w_ps = subprocess.run([ps, '-NoProfile', '-Command', '(Get-Command helio).Source'], cwd=cwd, env=env, capture_output=True, text=True); "
                            "r_ps = subprocess.run([ps, '-NoProfile', '-Command', 'helio inbox --as demo/win'], "
                            "cwd=cwd, env=env, capture_output=True, text=True); "
                            "print('WCMD:', '|'.join(ln.strip() for ln in w_cmd.stdout.strip().splitlines() if ln.strip())); "
                            "print('WPS:', w_ps.stdout.strip()); "
                            "print('CMD:', r_cmd.stdout); print('PS:', r_ps.stdout); "
                            "raise SystemExit(w_cmd.returncode or r_cmd.returncode or w_ps.returncode or r_ps.returncode)"
                        ),
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(shell_probe.returncode, 0, shell_probe.stdout + shell_probe.stderr)
                wcmd_lines = [p.lower() for p in [ln for ln in shell_probe.stdout.splitlines() if ln.startswith("WCMD:")][0].split("WCMD:", 1)[1].strip().split("|") if p]
                wps_line = [ln for ln in shell_probe.stdout.splitlines() if ln.startswith("WPS:")][0].split("WPS:", 1)[1].strip()
                self.assertIn(expected_cmd.lower(), wcmd_lines)
                self.assertTrue(wcmd_lines[0].startswith(win_to.lower() + "\\"), wcmd_lines)
                self.assertEqual(wps_line.lower(), expected_cmd.lower())
                _, after_cmd = shell_probe.stdout.split("\nCMD:", 1)
                cmd_out, ps_out = after_cmd.split("\nPS:", 1)
                self.assertIn(msg_subject, cmd_out)
                self.assertIn(msg_subject, ps_out)

                # Actual .claude/settings.json hook command executed in Windows Git Bash (where Claude Code runs hooks):
                # displays the uniquely addressed WSL message within timeout AND stays quiet for an empty inbox!
                settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
                hook_cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
                hook_probe = subprocess.run(
                    [
                        "python.exe",
                        "-c",
                        (
                            "import os, pathlib, shutil, subprocess, winreg; "
                            "cands = []; "
                            "git_exe = shutil.which('git'); "
                            "git_root = pathlib.Path(git_exe).resolve().parent.parent if git_exe else None; "
                            "cands.extend([git_root/'bin'/'bash.exe', git_root/'usr'/'bin'/'bash.exe'] if git_root else []); "
                            "wb = shutil.which('bash'); "
                            "cands.append(pathlib.Path(wb)) if wb and 'system32' not in wb.lower() else None; "
                            "cands.extend([pathlib.Path(r'C:\\Program Files\\Git\\bin\\bash.exe'), pathlib.Path(r'C:\\Program Files (x86)\\Git\\bin\\bash.exe')]); "
                            "bash = next((str(p) for p in cands if p and p.is_file()), None); "
                            "assert bash, 'Git Bash required on Windows'; "
                            f"k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, {reg_key!r}); "
                            "reg_path, _ = winreg.QueryValueEx(k, 'Path'); k.Close(); "
                            "env = dict(os.environ); env.pop('HELIO_STORE', None); "
                            "clean_sys = [d for d in env.get('PATH', '').split(';') if d and not any((pathlib.Path(d)/n).is_file() for n in ('helio','helio.cmd','helio.exe'))]; "
                            "env['PATH'] = ';'.join(clean_sys) + ';' + reg_path; "
                            "cwd = os.environ.get('TEMP', 'C:\\\\Windows\\\\Temp'); "
                            "env_msg = dict(env); env_msg['HELIO_AS'] = 'demo/win'; "
                            f"r_msg = subprocess.run([bash, '-c', {hook_cmd!r}], cwd=cwd, env=env_msg, capture_output=True, text=True); "
                            "env_empty = dict(env); env_empty['HELIO_AS'] = 'demo/nobody'; "
                            f"r_empty = subprocess.run([bash, '-c', {hook_cmd!r}], cwd=cwd, env=env_empty, capture_output=True, text=True); "
                            "print('HMSG:', r_msg.stdout); print('HEMPTY:', repr(r_empty.stdout)); "
                            "raise SystemExit(r_msg.returncode or r_empty.returncode)"
                        ),
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(hook_probe.returncode, 0, hook_probe.stdout + hook_probe.stderr)
                self.assertIn(msg_subject, hook_probe.stdout)
                self.assertIn("HEMPTY: ''", hook_probe.stdout)

                # Documented CLI `--windows --back` rolls back to the default WSL store generation,
                # and bare `helio` reads back `def_subj` from the previous store!
                rc_back = self.mod.main(["--windows", "--back", "--to", win_to, "--reg-key", reg_key])
                self.assertEqual(rc_back, 0)
                back_probe = subprocess.run(
                    [
                        "python.exe",
                        "-c",
                        (
                            "import os, pathlib, subprocess, winreg; "
                            f"k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, {reg_key!r}); "
                            "reg_path, _ = winreg.QueryValueEx(k, 'Path'); k.Close(); "
                            "env = dict(os.environ); env.pop('HELIO_STORE', None); "
                            "clean_sys = [d for d in env.get('PATH', '').split(';') if d and not any((pathlib.Path(d)/n).is_file() for n in ('helio','helio.cmd','helio.exe'))]; "
                            "env['PATH'] = ';'.join(clean_sys) + ';' + reg_path; "
                            "r = subprocess.run(['cmd.exe', '/c', 'helio', 'inbox', '--as', 'demo/win'], "
                            "cwd=os.environ.get('TEMP', 'C:\\\\Windows\\\\Temp'), env=env, capture_output=True, text=True); "
                            "print(r.stdout); raise SystemExit(r.returncode)"
                        ),
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(back_probe.returncode, 0, back_probe.stdout + back_probe.stderr)
                self.assertIn(def_subj, back_probe.stdout)

                # Reachable WSL directory that is NOT initialized (missing messages/) reports "there is no store here",
                # NOT an offline WSL distro error!
                empty_wsl_dir = root / "reachable_empty_wsl_dir"
                empty_wsl_dir.mkdir()
                empty_wsl_unc = subprocess.run(["wslpath", "-w", str(empty_wsl_dir)], capture_output=True, text=True, check=True).stdout.strip()
                r_empty_wsl = subprocess.run(
                    ["python.exe", "-c", f"import subprocess; r=subprocess.run([{win_to + '\\\\bin\\\\helio.exe'!r}, 'check', '--store', {empty_wsl_unc!r}], capture_output=True, text=True); print(r.stderr); raise SystemExit(r.returncode)"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_empty_wsl.returncode, 3)
                self.assertIn("there is no store here", r_empty_wsl.stdout)
                self.assertNotIn("cannot reach WSL store", r_empty_wsl.stdout)

                # Failed build from WSL leaves the existing Windows installation untouched!
                fail_ws = root / "fail_ws"
                fail_repo = fail_ws / "Heliograph"
                init_git_repo(fail_repo)
                fail_sdk = fail_ws / "ImpossibleSDK" / "tools"
                fail_sdk.mkdir(parents=True)
                (fail_sdk / "app_build.py").write_text("import sys; sys.exit(4)\n", encoding="utf-8")
                with self.assertRaises(self.mod.InstallError) as ctx:
                    self.mod.run_windows_from_wsl(
                        repo=fail_repo,
                        mode="install",
                        win_to_raw=win_to,
                        store_raw=str(wsl_store),
                        reg_key=reg_key,
                    )
                self.assertEqual(ctx.exception.code, 4)

                # Status and remove via WSL handoff both succeed.
                self.assertEqual(
                    self.mod.run_windows_from_wsl(repo=REPO, mode="status", win_to_raw=win_to, store_raw=None, reg_key=reg_key),
                    0,
                )
                self.assertEqual(
                    self.mod.run_windows_from_wsl(repo=REPO, mode="remove", win_to_raw=win_to, store_raw=None, reg_key=reg_key),
                    0,
                )
        finally:
            subprocess.run(
                [
                    "python.exe",
                    "-c",
                    (
                        "import shutil, winreg; "
                        f"shutil.rmtree({win_tmp_raw!r}, ignore_errors=True); "
                        f"winreg.DeleteKey(winreg.HKEY_CURRENT_USER, {reg_key!r})"
                    ),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            )


@unittest.skipUnless(os.name == "nt", "Windows-native installer tests run on Windows via prove.py")
class TestWindowsInstaller(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = load_installer()
        cls.win_mod = cls.mod._load_win_install()
        build_dir = Path(os.environ.get("IMPOSSIBLE_BUILD") or (REPO / "build" / "linux"))
        cls.helio_exe = build_dir / "helio.exe"
        if not cls.helio_exe.is_file():
            raise RuntimeError(f"required built helio.exe not found at {cls.helio_exe}")

    def _new_reg_key(self) -> str:
        import uuid

        return f"Software\\HeliographTest\\win_{uuid.uuid4().hex[:12]}"

    def _delete_reg_key(self, reg_key: str) -> None:
        import winreg

        with contextlib.suppress(OSError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, reg_key)

    def test_windows_install_bare_helio_cmd_powershell_store_precedence_and_unreachable_wsl(self) -> None:
        import uuid

        reg_key = self._new_reg_key()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                win_to = root / "Heliograph Install"
                default_store = root / "default store with spaces"
                env_store = root / "env store"
                flag_store = root / "flag store"
                outside_cwd = root / "outside"
                outside_cwd.mkdir()

                for sp, label in ((default_store, "msg-default"), (env_store, "msg-env"), (flag_store, "msg-flag")):
                    r = subprocess.run([str(self.helio_exe), "init", "--store", str(sp)], capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    t = subprocess.run(
                        [str(self.helio_exe), "tell", "demo/win", label, "--as", "demo/sender", "--store", str(sp)],
                        input=f"body for {label}\n",
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(t.returncode, 0, t.stderr)

                exe_sha = self.win_mod.sha256_file(self.helio_exe)
                self.win_mod.win_install(
                    source_exe=self.helio_exe,
                    source_sha256=exe_sha,
                    store_path=str(default_store),
                    commit="a" * 40,
                    dirty=False,
                    win_to=win_to,
                    reg_key=reg_key,
                )
                # helio.cmd is in win_to; helio.exe is in win_to\bin (NOT beside helio.cmd!).
                self.assertTrue((win_to / "helio.cmd").is_file())
                self.assertFalse((win_to / "helio.exe").exists())
                self.assertTrue((win_to / "bin" / "helio.exe").is_file())

                existed_p, reg_path_val, _ = self.win_mod.read_reg_path(reg_key)
                self.assertTrue(existed_p)
                base_env = dict(os.environ)
                base_env.pop("HELIO_STORE", None)
                # Append the registry Path value after system PATH (excluding any pre-existing production helio dir).
                clean_sys = [
                    d for d in base_env.get("PATH", "").split(";")
                    if d and not any((Path(d) / n).is_file() for n in ("helio", "helio.cmd", "helio.exe"))
                ]
                base_env["PATH"] = f"{';'.join(clean_sys)};{reg_path_val}"

                # Bare `helio` from cmd.exe outside win_to resolves to <win_to>\helio.cmd and reaches default_store.
                w_cmd = subprocess.run(["cmd.exe", "/c", "where", "helio"], cwd=str(outside_cwd), env=base_env, capture_output=True, text=True)
                self.assertEqual(w_cmd.returncode, 0, w_cmd.stderr)
                where_lines = [ln.strip().lower() for ln in w_cmd.stdout.strip().splitlines() if ln.strip()]
                self.assertIn(str((win_to / "helio.cmd").resolve()).lower(), where_lines)
                self.assertTrue(where_lines[0].startswith(str(win_to.resolve()).lower() + "\\"), where_lines)
                r_cmd = subprocess.run(
                    ["cmd.exe", "/c", "helio", "inbox", "--as", "demo/win"],
                    cwd=str(outside_cwd),
                    env=base_env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_cmd.returncode, 0, r_cmd.stdout + r_cmd.stderr)
                self.assertIn("msg-default", r_cmd.stdout)

                # Bare `helio` from PowerShell outside win_to resolves to <win_to>\helio.cmd and reaches default_store.
                ps = shutil.which("powershell.exe")
                self.assertIsNotNone(ps, "powershell.exe is required for Windows command discovery test")
                assert ps is not None
                w_ps = subprocess.run([ps, "-NoProfile", "-Command", "(Get-Command helio).Source"], cwd=str(outside_cwd), env=base_env, capture_output=True, text=True)
                self.assertEqual(w_ps.returncode, 0, w_ps.stderr)
                self.assertEqual(w_ps.stdout.strip().lower(), str((win_to / "helio.cmd").resolve()).lower())
                r_ps = subprocess.run(
                    [ps, "-NoProfile", "-Command", "helio inbox --as demo/win"],
                    cwd=str(outside_cwd),
                    env=base_env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_ps.returncode, 0, r_ps.stdout + r_ps.stderr)
                self.assertIn("msg-default", r_ps.stdout)

                # HELIO_STORE overrides default_store; --store overrides HELIO_STORE.
                env_override = dict(base_env)
                env_override["HELIO_STORE"] = str(env_store)
                r_env = subprocess.run(
                    ["cmd.exe", "/c", "helio", "inbox", "--as", "demo/win"],
                    cwd=str(outside_cwd),
                    env=env_override,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_env.returncode, 0, r_env.stderr)
                self.assertIn("msg-env", r_env.stdout)
                self.assertNotIn("msg-default", r_env.stdout)

                r_flag = subprocess.run(
                    ["cmd.exe", "/c", "helio", "inbox", "--as", "demo/win", "--store", str(flag_store)],
                    cwd=str(outside_cwd),
                    env=env_override,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_flag.returncode, 0, r_flag.stderr)
                self.assertIn("msg-flag", r_flag.stdout)
                self.assertNotIn("msg-env", r_flag.stdout)

                # Forward stdin body, literal special args (e.g. `--key --store`), and exact exit codes.
                r_tell = subprocess.run(
                    ["cmd.exe", "/c", "helio", "tell", "demo/stdin", "stdin-check", "--as", "demo/sender", "--key", "--store"],
                    input="forwarded stdin payload line\n",
                    cwd=str(outside_cwd),
                    env=base_env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_tell.returncode, 0, r_tell.stdout + r_tell.stderr)
                msg_id = r_tell.stdout.strip().splitlines()[-1].strip()
                r_show = subprocess.run(
                    ["cmd.exe", "/c", "helio", "show", msg_id, "--as", "demo/stdin"],
                    cwd=str(outside_cwd),
                    env=base_env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_show.returncode, 0, r_show.stderr)
                self.assertIn("forwarded stdin payload line", r_show.stdout)

                r_bad_direct = subprocess.run(
                    [str(self.helio_exe), "show", "nonexistent-id-123", "--store", str(default_store)],
                    capture_output=True,
                    text=True,
                )
                r_bad = subprocess.run(
                    ["cmd.exe", "/c", "helio", "show", "nonexistent-id-123"],
                    cwd=str(outside_cwd),
                    env=base_env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_bad.returncode, r_bad_direct.returncode)
                self.assertNotEqual(r_bad.returncode, 0)

                # Unreachable \\wsl.localhost distro gives exit 3 naming WSL + distro and creates no alternate store,
                # while a reachable --store or HELIO_STORE override STILL succeeds!
                bogus_distro = f"NoSuchDistro{uuid.uuid4().hex[:6]}"
                bogus_unc = f"\\\\wsl.localhost\\{bogus_distro}\\home\\nobody\\Heliograph-signals"
                self.win_mod.win_install(
                    source_exe=self.helio_exe,
                    source_sha256=exe_sha,
                    store_path=bogus_unc,
                    commit="b" * 40,
                    win_to=win_to,
                    reg_key=reg_key,
                )
                r_unr = subprocess.run(
                    ["cmd.exe", "/c", "helio", "check"],
                    cwd=str(outside_cwd),
                    env=base_env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_unr.returncode, 3, r_unr.stdout + r_unr.stderr)
                self.assertIn("WSL", r_unr.stderr)
                self.assertIn(bogus_distro, r_unr.stderr)

                # Reachable HELIO_STORE and --store overrides still succeed when default WSL store is unreachable.
                r_ov1 = subprocess.run(
                    ["cmd.exe", "/c", "helio", "check"],
                    cwd=str(outside_cwd),
                    env=env_override,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_ov1.returncode, 0, r_ov1.stderr)
                r_ov2 = subprocess.run(
                    ["cmd.exe", "/c", "helio", "check", "--store", str(flag_store)],
                    cwd=str(outside_cwd),
                    env=base_env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_ov2.returncode, 0, r_ov2.stderr)

                # Failing command with an explicit reachable --store (including empty quoted arg AND comma-containing
                # --about value that cmd.exe's shift would split!) returns helio.exe's exact exit code with NO WSL diagnostic!
                for extra_args in (["--about", ""], ["--about", "ref,--key"]):
                    r_fail_direct = subprocess.run(
                        [str(self.helio_exe), "show", "nonexistent-id-123", "--as", "demo/win", *extra_args, "--store", str(flag_store)],
                        capture_output=True,
                        text=True,
                    )
                    r_fail_ov = subprocess.run(
                        ["cmd.exe", "/c", "helio", "show", "nonexistent-id-123", "--as", "demo/win", *extra_args, "--store", str(flag_store)],
                        cwd=str(outside_cwd),
                        env=base_env,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(r_fail_ov.returncode, r_fail_direct.returncode, extra_args)
                    self.assertNotEqual(r_fail_ov.returncode, 0)
                    self.assertNotIn("WSL", r_fail_ov.stderr)
                    self.assertNotIn(bogus_distro, r_fail_ov.stderr)

                # Store-independent `helio help` STILL succeeds (exit 0) even when the default WSL distro is offline!
                r_help = subprocess.run(
                    ["cmd.exe", "/c", "helio", "help"],
                    cwd=str(outside_cwd),
                    env=base_env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_help.returncode, 0, r_help.stdout + r_help.stderr)
                self.assertIn("helio", r_help.stdout.lower())

                # Offline `helio init` against unreachable WSL distro ALSO reports WSL + distro (exit 3)!
                r_init_unr = subprocess.run(
                    ["cmd.exe", "/c", "helio", "init"],
                    cwd=str(outside_cwd),
                    env=base_env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_init_unr.returncode, 3, r_init_unr.stdout + r_init_unr.stderr)
                self.assertIn("WSL", r_init_unr.stderr)
                self.assertIn(bogus_distro, r_init_unr.stderr)

                # Explicit --store pointing to an unavailable UNC distro gets the standard store error
                # (NOT the installed-default offline-WSL diagnostic!), and malformed ESC bytes are sanitized!
                r_exp_unr = subprocess.run(
                    [str(self.helio_exe), "check", "--store", bogus_unc],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_exp_unr.returncode, 3)
                self.assertNotIn("cannot reach WSL store", r_exp_unr.stderr)

                ctrl_env = dict(base_env)
                ctrl_env["HELIO_DEFAULT_STORE"] = "\\\\wsl.localhost\\\x1b[31mEvil\\store"
                r_ctrl = subprocess.run(
                    [str(self.helio_exe), "check"],
                    env=ctrl_env,
                    capture_output=True,
                )
                self.assertEqual(r_ctrl.returncode, 3)
                self.assertNotIn(b"\x1b", r_ctrl.stderr)

                # Non-ASCII Unicode Windows store path works through helio.cmd via HELIO_DEFAULT_STORE_HEX!
                uni_store = root / "Donn\u00e9es_store"
                self.win_mod.win_install(
                    source_exe=self.helio_exe,
                    source_sha256=exe_sha,
                    store_path=str(uni_store),
                    commit="c" * 40,
                    win_to=win_to,
                    reg_key=reg_key,
                )
                self.assertIn("HELIO_DEFAULT_STORE_HEX=", (win_to / "helio.cmd").read_text(encoding="ascii"))
                r_uni = subprocess.run(
                    ["cmd.exe", "/c", "helio", "check"],
                    cwd=str(outside_cwd),
                    env=base_env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_uni.returncode, 0, r_uni.stdout + r_uni.stderr)

                # Semicolon in --to is refused with exit 2; non-empty unrelated --store is refused with exit 2;
                # PATHEXT missing .CMD fails win_status with exit 3!
                with self.assertRaises(self.win_mod.WinInstallError) as ctx:
                    self.win_mod.win_install(
                        source_exe=self.helio_exe,
                        source_sha256=exe_sha,
                        store_path=str(default_store),
                        commit="c" * 40,
                        win_to=root / "Apps;Team" / "Heliograph",
                        reg_key=reg_key,
                    )
                self.assertEqual(ctx.exception.code, 2)

                unrelated_dir = root / "unrelated_project"
                unrelated_dir.mkdir()
                (unrelated_dir / "README.txt").write_text("existing files\n", encoding="utf-8")
                with self.assertRaises(self.win_mod.WinInstallError) as ctx:
                    self.win_mod.win_install(
                        source_exe=self.helio_exe,
                        source_sha256=exe_sha,
                        store_path=str(unrelated_dir),
                        commit="c" * 40,
                        win_to=win_to,
                        reg_key=reg_key,
                    )
                self.assertEqual(ctx.exception.code, 2)

                with mock.patch.dict(os.environ, {"PATHEXT": ".COM;.EXE;.BAT"}):
                    code_pe, lines_pe = self.win_mod.win_status(win_to=win_to, reg_key=reg_key)
                    self.assertEqual(code_pe, 3, lines_pe)
                    self.assertTrue(any("PATHEXT" in ln for ln in lines_pe), lines_pe)

                # Deleting local store and repeating an identical win_install re-initializes the store so status is 0!
                shutil.rmtree(uni_store)
                self.assertFalse(uni_store.exists())
                self.win_mod.win_install(
                    source_exe=self.helio_exe,
                    source_sha256=exe_sha,
                    store_path=str(uni_store),
                    commit="c" * 40,
                    win_to=win_to,
                    reg_key=reg_key,
                )
                self.assertTrue((uni_store / "messages").is_dir())
                code_reinit, lines_reinit = self.win_mod.win_status(win_to=win_to, reg_key=reg_key)
                self.assertEqual(code_reinit, 0, lines_reinit)

                # An earlier extensionless `helio` script on HKCU\<reg_key>\Path shadows the Git Bash shim:
                # win_install reports `shadowed_by` naming it and win_status exits 3 naming it; removing it restores exit 0!
                import winreg

                sh_shadow_dir = root / "sh_shadow_earlier"
                sh_shadow_dir.mkdir()
                sh_shadow_file = sh_shadow_dir / "helio"
                sh_shadow_file.write_text("#!/bin/sh\necho SHADOW\n", encoding="utf-8")
                _, orig_rp, orig_rk = self.win_mod.read_reg_path(reg_key)
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_key, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.SetValueEx(k, "Path", 0, orig_rk, f"{sh_shadow_dir};{orig_rp}")
                try:
                    # Also delete uni_store so we exercise the store-restoring identical-reinstall return path!
                    shutil.rmtree(uni_store)
                    m_sh_shadow = self.win_mod.win_install(
                        source_exe=self.helio_exe,
                        source_sha256=exe_sha,
                        store_path=str(uni_store),
                        commit="c" * 40,
                        win_to=win_to,
                        reg_key=reg_key,
                    )
                    self.assertTrue((uni_store / "messages").is_dir())
                    sh_shadow_expected = str(sh_shadow_file.parent.resolve() / sh_shadow_file.name)
                    win_sh_expected = str(win_to.resolve() / "helio")
                    self.assertEqual(os.path.normcase(m_sh_shadow["shadowed_by"]), os.path.normcase(sh_shadow_expected))
                    self.assertEqual(os.path.normcase(m_sh_shadow["shadowed_target"]), os.path.normcase(win_sh_expected))
                    code_shs, lines_shs = self.win_mod.win_status(win_to=win_to, reg_key=reg_key)
                    self.assertEqual(code_shs, 3, lines_shs)
                    self.assertTrue(any(str(sh_shadow_file.resolve()).lower() in ln.lower() for ln in lines_shs), lines_shs)

                    # Even a symlink in an earlier PATH dir pointing at <win_to>\helio is detected as shadowing
                    # (without resolving the leaf) because `dirname "$0"` would look under the link's dir!
                    sh_shadow_file.unlink()
                    try:
                        sh_shadow_file.symlink_to(win_to / "helio")
                    except OSError:
                        # Non-admin Windows without Developer Mode may refuse file symlinks; fall back to regular file.
                        sh_shadow_file.write_text("#!/bin/sh\necho LINK\n", encoding="utf-8")
                    w_link = self.win_mod._find_winning_helio_sh(reg_key, win_to)
                    self.assertIsNotNone(w_link)
                    self.assertEqual(os.path.normcase(str(w_link)), os.path.normcase(sh_shadow_expected))
                    code_lnk, _ = self.win_mod.win_status(win_to=win_to, reg_key=reg_key)
                    self.assertEqual(code_lnk, 3)

                    # When an earlier directory supplies BOTH helio.cmd and extensionless helio (e.g., an older
                    # Heliograph install earlier on Path), install reports BOTH conflicts (cmd AND Git Bash shim)
                    # in `shadowings` and on CLI stderr!
                    cmd_shadow_file = sh_shadow_dir / "helio.cmd"
                    cmd_shadow_file.write_text("@echo off\necho OLD_CMD\n", encoding="utf-8")
                    sh_shadow_file.unlink()
                    sh_shadow_file.write_text("#!/bin/sh\necho OLD_SH\n", encoding="utf-8")
                    m_both = self.win_mod.win_install(
                        source_exe=self.helio_exe,
                        source_sha256=exe_sha,
                        store_path=str(uni_store),
                        commit="c" * 40,
                        win_to=win_to,
                        reg_key=reg_key,
                    )
                    pairs = {
                        (os.path.normcase(s["target"]), os.path.normcase(s["by"]))
                        for s in m_both.get("shadowings", [])
                    }
                    self.assertIn(
                        (os.path.normcase(str((win_to / "helio.cmd").resolve())), os.path.normcase(str(cmd_shadow_file.resolve()))),
                        pairs,
                        m_both,
                    )
                    self.assertIn(
                        (os.path.normcase(win_sh_expected), os.path.normcase(sh_shadow_expected)),
                        pairs,
                        m_both,
                    )
                    cli_both = subprocess.run(
                        [
                            sys.executable,
                            str(REPO / "tools" / "install.py"),
                            "--windows",
                            "--source-exe",
                            str(self.helio_exe),
                            "--source-sha256",
                            exe_sha,
                            "--store",
                            str(uni_store),
                            "--commit",
                            "c" * 40,
                            "--to",
                            str(win_to),
                            "--reg-key",
                            reg_key,
                        ],
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(cli_both.returncode, 0, cli_both.stdout + cli_both.stderr)
                    err_lines = [ln.strip().lower() for ln in cli_both.stderr.splitlines()]
                    expected_cmd_warn = (
                        f"warning: another helio earlier on path shadows "
                        f"{(win_to / 'helio.cmd').resolve()}: {cmd_shadow_file.resolve()}"
                    ).lower()
                    expected_sh_warn = (
                        f"warning: another helio earlier on path shadows "
                        f"{win_sh_expected}: {sh_shadow_expected}"
                    ).lower()
                    self.assertIn(expected_cmd_warn, err_lines, cli_both.stderr)
                    self.assertIn(expected_sh_warn, err_lines, cli_both.stderr)
                finally:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_key, 0, winreg.KEY_SET_VALUE) as k:
                        winreg.SetValueEx(k, "Path", 0, orig_rk, orig_rp)
                # Store-restoring identical reinstall when PATHEXT omits .CMD ALSO reports shadowed_by!
                shutil.rmtree(uni_store)
                with mock.patch.dict(os.environ, {"PATHEXT": ".COM;.EXE;.BAT"}):
                    m_pe_reinit = self.win_mod.win_install(
                        source_exe=self.helio_exe,
                        source_sha256=exe_sha,
                        store_path=str(uni_store),
                        commit="c" * 40,
                        win_to=win_to,
                        reg_key=reg_key,
                    )
                    self.assertIn("PATHEXT", m_pe_reinit.get("shadowed_by", ""))
                code_sh_ok, lines_sh_ok = self.win_mod.win_status(win_to=win_to, reg_key=reg_key)
                self.assertEqual(code_sh_ok, 0, lines_sh_ok)

                # A directory junction to win_to earlier on Path resolves its parent to win_to -> healthy status 0!
                junc_dir = root / "win_to_junction"
                mkj = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junc_dir), str(win_to)], capture_output=True, text=True)
                self.assertEqual(mkj.returncode, 0, mkj.stdout + mkj.stderr)
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_key, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.SetValueEx(k, "Path", 0, orig_rk, f"{junc_dir};{orig_rp}")
                try:
                    code_junc, lines_junc = self.win_mod.win_status(win_to=win_to, reg_key=reg_key)
                    self.assertEqual(code_junc, 0, lines_junc)
                finally:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_key, 0, winreg.KEY_SET_VALUE) as k:
                        winreg.SetValueEx(k, "Path", 0, orig_rk, orig_rp)
                    subprocess.run(["cmd.exe", "/c", "rmdir", str(junc_dir)], capture_output=True, text=True)
        finally:
            self._delete_reg_key(reg_key)

    def test_windows_busy_exe_rename_away_sweep_status_back_and_ownership(self) -> None:
        reg_key = self._new_reg_key()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                win_to = root / "Heliograph"
                s1, s2 = root / "s1", root / "s2"
                for sp in (s1, s2):
                    subprocess.run([str(self.helio_exe), "init", "--store", str(sp)], check=True, capture_output=True)
                exe_a = root / "helio_a.exe"
                exe_b = root / "helio_b.exe"
                make_distinct_binary(self.helio_exe, exe_a, b"WIN_A")
                make_distinct_binary(self.helio_exe, exe_b, b"WIN_B")
                sha_a = self.win_mod.sha256_file(exe_a)
                sha_b = self.win_mod.sha256_file(exe_b)

                self.win_mod.win_install(source_exe=exe_a, source_sha256=sha_a, store_path=str(s1), commit="a" * 40, win_to=win_to, reg_key=reg_key)

                # Hold live bin\helio.exe running via `tell` blocked on stdin (CREATE_NO_WINDOW).
                create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                holder = subprocess.Popen(
                    [str(win_to / "bin" / "helio.exe"), "tell", "demo/lane", "busy-upgrade", "--as", "demo/sender", "--store", str(s1)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=create_no_window,
                )
                try:
                    self.assertIsNone(holder.poll())
                    m2 = self.win_mod.win_install(source_exe=exe_b, source_sha256=sha_b, store_path=str(s2), commit="b" * 40, win_to=win_to, reg_key=reg_key)
                    self.assertEqual(self.win_mod.sha256_file(win_to / "bin" / "helio.exe"), sha_b)
                    self.assertGreaterEqual(len(m2["retired"]), 1, "expected busy live helio.exe to be renamed to .retired")
                    retired_name = next(iter(m2["retired"].keys()))
                    retired_file = win_to / "bin" / retired_name
                    self.assertTrue(retired_file.is_file())
                    _out, err = holder.communicate(input="body\n", timeout=30)
                    self.assertEqual(holder.returncode, 0, err)
                finally:
                    if holder.poll() is None:
                        holder.kill()
                        holder.communicate()

                # Tampering with a tracked .retired file causes install/remove to refuse with exit 2 and preserve bytes!
                orig_retired_bytes = retired_file.read_bytes()
                retired_file.write_bytes(b"externally-modified-retired")
                with self.assertRaises(self.win_mod.WinInstallError) as ctx:
                    self.win_mod.win_install(source_exe=exe_b, source_sha256=sha_b, store_path=str(s2), commit="b" * 40, win_to=win_to, reg_key=reg_key)
                self.assertEqual(ctx.exception.code, 2)
                self.assertEqual(retired_file.read_bytes(), b"externally-modified-retired")
                with self.assertRaises(self.win_mod.WinInstallError) as ctx:
                    self.win_mod.win_remove(win_to=win_to, reg_key=reg_key)
                self.assertEqual(ctx.exception.code, 2)
                self.assertEqual(retired_file.read_bytes(), b"externally-modified-retired")
                retired_file.write_bytes(orig_retired_bytes)

                # Next install (identical to B) sweeps retired file AND preserves distinct rollback generation A!
                m3 = self.win_mod.win_install(source_exe=exe_b, source_sha256=sha_b, store_path=str(s2), commit="b" * 40, win_to=win_to, reg_key=reg_key)
                self.assertEqual(m3["retired"], {})
                self.assertFalse(retired_file.exists())
                self.assertEqual(m3["previous"]["helio_exe_sha256"], sha_a)

                # Intact earlier-template helio.cmd passes win_status (hash match), and upgrade + win_back
                # restores the exact earlier helio.cmd bytes!
                exe_c = root / "helio_c.exe"
                make_distinct_binary(self.helio_exe, exe_c, b"WIN_C_UNKNOWN")
                mp = win_to / self.win_mod.WIN_MANIFEST_NAME
                earlier_cmd_bytes = b"@echo off\r\nREM Managed by Heliograph installer\r\nREM earlier template\r\n"
                (win_to / "helio.cmd").write_bytes(earlier_cmd_bytes)
                m_cur = json.loads(mp.read_text(encoding="utf-8"))
                m_cur["current"]["helio_cmd_sha256"] = self.win_mod.sha256_file(win_to / "helio.cmd")
                m_cur["current"].pop("helio_cmd_text", None)
                mp.write_text(json.dumps(m_cur), encoding="utf-8")
                code_earl, lines_earl = self.win_mod.win_status(win_to=win_to, reg_key=reg_key)
                self.assertEqual(code_earl, 0, lines_earl)
                self.win_mod.win_install(source_exe=exe_c, source_sha256=self.win_mod.sha256_file(exe_c), store_path=str(s2), commit="c" * 40, win_to=win_to, reg_key=reg_key)
                self.win_mod.win_back(win_to=win_to, reg_key=reg_key)
                self.assertEqual((win_to / "helio.cmd").read_bytes(), earlier_cmd_bytes)
                # Restore B over A cleanly for the remaining status/back checks below.
                self.win_mod.win_install(source_exe=exe_a, source_sha256=sha_a, store_path=str(s1), commit="a" * 40, win_to=win_to, reg_key=reg_key)
                self.win_mod.win_install(source_exe=exe_b, source_sha256=sha_b, store_path=str(s2), commit="b" * 40, win_to=win_to, reg_key=reg_key)

                # Status healthy -> 0; tamper checks (wrapper, empty store, top-level helio.exe shadow, wrong reg_key) -> 3.
                code, _ = self.win_mod.win_status(win_to=win_to, reg_key=reg_key)
                self.assertEqual(code, 0)

                # Empty store directory (missing messages/) must NOT report status: ok.
                empty_s = root / "empty_store_dir"
                empty_s.mkdir()
                self.win_mod.win_install(source_exe=exe_b, source_sha256=sha_b, store_path=str(empty_s), commit="b" * 40, win_to=win_to, reg_key=reg_key)
                self.assertTrue((empty_s / "messages").is_dir())
                shutil.rmtree(empty_s / "messages")
                code, lines = self.win_mod.win_status(win_to=win_to, reg_key=reg_key)
                self.assertEqual(code, 3, lines)
                self.win_mod.win_install(source_exe=exe_a, source_sha256=sha_a, store_path=str(s1), commit="a" * 40, win_to=win_to, reg_key=reg_key)
                self.win_mod.win_install(source_exe=exe_b, source_sha256=sha_b, store_path=str(s2), commit="b" * 40, win_to=win_to, reg_key=reg_key)

                # Top-level helio.exe shadowing helio.cmd fails status and refuses install.
                shadow_exe = win_to / "helio.exe"
                shadow_exe.write_bytes(b"shadow")
                code, lines = self.win_mod.win_status(win_to=win_to, reg_key=reg_key)
                self.assertEqual(code, 3)
                self.assertTrue(any("shadow" in l for l in lines), lines)
                with self.assertRaises(self.win_mod.WinInstallError) as ctx:
                    self.win_mod.win_install(source_exe=exe_b, source_sha256=sha_b, store_path=str(s2), commit="b" * 40, win_to=win_to, reg_key=reg_key)
                self.assertEqual(ctx.exception.code, 2)
                shadow_exe.unlink()

                # Wrong --reg-key on remove is refused without touching the wrong registry key!
                wrong_key = self._new_reg_key()
                with self.assertRaises(self.win_mod.WinInstallError) as ctx:
                    self.win_mod.win_remove(win_to=win_to, reg_key=wrong_key)
                self.assertEqual(ctx.exception.code, 2)

                orig_cmd = (win_to / "helio.cmd").read_bytes()
                (win_to / "helio.cmd").write_bytes(orig_cmd + b"REM edited\r\n")
                code, lines = self.win_mod.win_status(win_to=win_to, reg_key=reg_key)
                self.assertEqual(code, 3)
                self.assertTrue(any("helio.cmd" in l for l in lines), lines)
                (win_to / "helio.cmd").write_bytes(orig_cmd)

                # Test --back and corrupt-backup refusal.
                saved_old = (win_to / "bin" / "helio.exe.old").read_bytes()
                (win_to / "bin" / "helio.exe.old").write_bytes(b"corrupt")
                with self.assertRaises(self.win_mod.WinInstallError) as ctx:
                    self.win_mod.win_back(win_to=win_to, reg_key=reg_key)
                self.assertEqual(ctx.exception.code, 2)
                self.assertEqual(self.win_mod.sha256_file(win_to / "bin" / "helio.exe"), sha_b)
                (win_to / "bin" / "helio.exe.old").write_bytes(saved_old)

                m_back = self.win_mod.win_back(win_to=win_to, reg_key=reg_key)
                self.assertEqual(m_back["current"]["helio_exe_sha256"], sha_a)
                self.assertEqual(self.win_mod.sha256_file(win_to / "bin" / "helio.exe"), sha_a)
                self.assertEqual(m_back["current"]["store"], str(s1))

                # Unrelated user files (including a file named .install.lock!) are preserved and keep win_to alive on remove.
                keep = win_to / "user_notes.txt"
                keep.write_text("do not delete\n", encoding="utf-8")
                user_lock = win_to / ".install.lock"
                user_lock.write_text("user lock file\n", encoding="utf-8")
                self.assertTrue(self.win_mod.win_remove(win_to=win_to, reg_key=reg_key))
                self.assertTrue(keep.is_file())
                self.assertEqual(user_lock.read_text(encoding="utf-8"), "user lock file\n")
                self.assertFalse((win_to / "helio.cmd").exists())
                self.assertFalse((win_to / "bin").exists())

                # Unowned pre-existing helio.cmd with no manifest is refused with byte-for-byte preservation.
                foreign_dir = root / "foreign_dest"
                foreign_dir.mkdir()
                foreign_cmd = foreign_dir / "helio.cmd"
                foreign_cmd.write_bytes(b"@echo custom user script\r\n")
                with self.assertRaises(self.win_mod.WinInstallError) as ctx:
                    self.win_mod.win_install(source_exe=exe_a, source_sha256=sha_a, store_path=str(s1), commit="a" * 40, win_to=foreign_dir, reg_key=reg_key)
                self.assertEqual(ctx.exception.code, 2)
                self.assertEqual(foreign_cmd.read_bytes(), b"@echo custom user script\r\n")
        finally:
            self._delete_reg_key(reg_key)

    def test_windows_registry_path_kind_percent_intervening_edits_broadcast_and_remove(self) -> None:
        import winreg

        reg_key = self._new_reg_key()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                win_to = root / "HeliographReg"
                store = root / "reg_store"
                subprocess.run([str(self.helio_exe), "init", "--store", str(store)], check=True, capture_output=True)
                exe_sha = self.win_mod.sha256_file(self.helio_exe)

                # 1. Initially absent Path -> created as REG_EXPAND_SZ, broadcast called once; 2nd identical install is a true no-op; remove deletes Path.
                with mock.patch.object(self.win_mod, "broadcast_environment_change", wraps=self.win_mod.broadcast_environment_change) as bcast:
                    m1 = self.win_mod.win_install(source_exe=self.helio_exe, source_sha256=exe_sha, store_path=str(store), commit="a" * 40, win_to=win_to, reg_key=reg_key)
                    self.assertEqual(bcast.call_count, 1)
                    existed, val, kind = self.win_mod.read_reg_path(reg_key)
                    self.assertTrue(existed)
                    self.assertEqual(kind, winreg.REG_EXPAND_SZ)
                    self.assertEqual(val, str(win_to.resolve()))

                    # Second identical install is a true no-op (skips broadcast, does not bump installed_at or rewrite files).
                    cmd_mtime = (win_to / "helio.cmd").stat().st_mtime_ns
                    m2 = self.win_mod.win_install(source_exe=self.helio_exe, source_sha256=exe_sha, store_path=str(store), commit="a" * 40, win_to=win_to, reg_key=reg_key)
                    self.assertEqual(bcast.call_count, 1)
                    self.assertEqual(m2["current"]["installed_at"], m1["current"]["installed_at"])
                    self.assertEqual((win_to / "helio.cmd").stat().st_mtime_ns, cmd_mtime)

                    # Remove deletes Path value and folder completely.
                    self.assertTrue(self.win_mod.win_remove(win_to=win_to, reg_key=reg_key))
                    self.assertEqual(bcast.call_count, 2)
                    existed, _, _ = self.win_mod.read_reg_path(reg_key)
                    self.assertFalse(existed)
                    self.assertFalse(win_to.exists())

                # 1b. Trailing semicolon (e.g. C:\Tools;) roundtrips verbatim through install + remove!
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, reg_key, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, r"C:\Tools;")
                self.win_mod.win_install(source_exe=self.helio_exe, source_sha256=exe_sha, store_path=str(store), commit="a" * 40, win_to=win_to, reg_key=reg_key)
                self.assertTrue(self.win_mod.win_remove(win_to=win_to, reg_key=reg_key))
                existed_tr, val_tr, kind_tr = self.win_mod.read_reg_path(reg_key)
                self.assertEqual((existed_tr, val_tr, kind_tr), (True, r"C:\Tools;", winreg.REG_EXPAND_SZ))

                # 2. Existing REG_EXPAND_SZ with %USERPROFILE%\bin AND empty delimiters (;;) stays verbatim,
                #    and intervening 3rd-party entry survives remove!
                with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, reg_key, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, r"%USERPROFILE%\bin;;C:\ExistingTools")
                self.win_mod.win_install(source_exe=self.helio_exe, source_sha256=exe_sha, store_path=str(store), commit="a" * 40, win_to=win_to, reg_key=reg_key)
                existed, val, kind = self.win_mod.read_reg_path(reg_key)
                self.assertEqual(kind, winreg.REG_EXPAND_SZ)
                self.assertIn(r"%USERPROFILE%\bin;;C:\ExistingTools", val)

                # Simulate another installer appending an entry after Heliograph installed.
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_key, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, val + r";C:\ThirdParty\bin")

                self.assertTrue(self.win_mod.win_remove(win_to=win_to, reg_key=reg_key))
                existed, val_after, kind_after = self.win_mod.read_reg_path(reg_key)
                self.assertTrue(existed)
                self.assertEqual(kind_after, winreg.REG_EXPAND_SZ)
                self.assertEqual(val_after, r"%USERPROFILE%\bin;;C:\ExistingTools;C:\ThirdParty\bin")

                # 2b. Failed WM_SETTINGCHANGE broadcast raises exit 3.
                with mock.patch.object(self.win_mod, "broadcast_environment_change", return_value=False):
                    with self.assertRaises(self.win_mod.WinInstallError) as ctx:
                        self.win_mod.ensure_reg_path_entry(reg_key, win_to)
                    self.assertEqual(ctx.exception.code, 3)

                # 3. Pre-existing expandable entry (%HELIO_TEST_WIN_TO%) is recognized as equivalent -> path_added=False!
                os.environ["HELIO_TEST_WIN_TO"] = str(win_to.resolve())
                try:
                    pre_val = r"%HELIO_TEST_WIN_TO%"
                    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, reg_key, 0, winreg.KEY_SET_VALUE) as k:
                        winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, pre_val)
                    with mock.patch.object(self.win_mod, "broadcast_environment_change", return_value=True) as bcast:
                        m_pre = self.win_mod.win_install(source_exe=self.helio_exe, source_sha256=exe_sha, store_path=str(store), commit="a" * 40, win_to=win_to, reg_key=reg_key)
                        self.assertFalse(m_pre["path_added"])
                        self.assertEqual(bcast.call_count, 0)
                        self.assertTrue(self.win_mod.win_remove(win_to=win_to, reg_key=reg_key))
                        self.assertEqual(bcast.call_count, 0)
                    existed, val_pre_after, kind_pre_after = self.win_mod.read_reg_path(reg_key)
                    self.assertEqual((existed, val_pre_after, kind_pre_after), (True, pre_val, winreg.REG_EXPAND_SZ))

                    # 4. User prepends an expandable spelling (%HELIO_TEST_WIN_TO%) AFTER install -> remove (and recovery
                    #    retry after interrupting at after_path!) deletes ONLY the literal entry and preserves %HELIO_TEST_WIN_TO%!
                    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, reg_key, 0, winreg.KEY_SET_VALUE) as k:
                        winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, r"C:\Tools")
                    self.win_mod.win_install(source_exe=self.helio_exe, source_sha256=exe_sha, store_path=str(store), commit="a" * 40, win_to=win_to, reg_key=reg_key)
                    _, cur_p, _ = self.win_mod.read_reg_path(reg_key)
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_key, 0, winreg.KEY_SET_VALUE) as k:
                        winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, r"%HELIO_TEST_WIN_TO%;" + cur_p)
                    with self.assertRaises(SimulatedCrash):
                        self.win_mod.win_remove(
                            win_to=win_to,
                            reg_key=reg_key,
                            on_boundary=lambda st: (_ for _ in ()).throw(SimulatedCrash(st)) if st == "after_path" else None,
                        )
                    self.assertTrue(self.win_mod.recover_win_if_needed(win_to, reg_key))
                    _, val_after_exp, kind_after_exp = self.win_mod.read_reg_path(reg_key)
                    self.assertEqual((val_after_exp, kind_after_exp), (r"%HELIO_TEST_WIN_TO%;C:\Tools", winreg.REG_EXPAND_SZ))
                finally:
                    os.environ.pop("HELIO_TEST_WIN_TO", None)
        finally:
            self._delete_reg_key(reg_key)

    def test_windows_crash_recovery_every_boundary_and_concurrent_registry_lock(self) -> None:
        reg_key = self._new_reg_key()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = root / "crash_store"
                subprocess.run([str(self.helio_exe), "init", "--store", str(store)], check=True, capture_output=True)
                exe_sha = self.win_mod.sha256_file(self.helio_exe)

                for boundary in ("after_staging_journal", "after_journal", "after_exe", "after_cmd", "after_path", "after_manifest"):
                    win_to = root / f"crash_{boundary}"

                    def crash_at(stage: str, target: str = boundary) -> None:
                        if stage == target:
                            raise SimulatedCrash(stage)

                    with self.assertRaises(SimulatedCrash):
                        self.win_mod.win_install(
                            source_exe=self.helio_exe,
                            source_sha256=exe_sha,
                            store_path=str(store),
                            commit="a" * 40,
                            win_to=win_to,
                            reg_key=reg_key,
                            on_boundary=crash_at,
                        )
                    self.assertTrue((win_to / self.win_mod.WIN_JOURNAL_NAME).exists())
                    # Verify recover_win_if_needed restores clean state BEFORE any retry install!
                    self.assertTrue(self.win_mod.recover_win_if_needed(win_to, reg_key))
                    self.assertFalse((win_to / self.win_mod.WIN_JOURNAL_NAME).exists())
                    if boundary != "after_manifest":
                        self.assertFalse((win_to / "helio.cmd").exists())
                        _, raw_p, _ = self.win_mod.read_reg_path(reg_key)
                        self.assertNotIn(str(win_to.resolve()), raw_p)
                    else:
                        code, lines = self.win_mod.win_status(win_to=win_to, reg_key=reg_key)
                        self.assertEqual(code, 0, lines)
                        self.assertTrue(self.win_mod.win_remove(win_to=win_to, reg_key=reg_key))

                # Upgrade (after_backup), rollback (after_exe), remove (after_path) crash recovery,
                # plus external file protection during recovery and out-of-root journal key refusal!
                exe_a = root / "cr_a.exe"
                exe_b = root / "cr_b.exe"
                make_distinct_binary(self.helio_exe, exe_a, b"CRA")
                make_distinct_binary(self.helio_exe, exe_b, b"CRB")
                sha_a, sha_b = self.win_mod.sha256_file(exe_a), self.win_mod.sha256_file(exe_b)

                upg_to = root / "crash_upg"
                self.win_mod.win_install(source_exe=exe_a, source_sha256=sha_a, store_path=str(store), commit="a" * 40, win_to=upg_to, reg_key=reg_key)
                with self.assertRaises(SimulatedCrash):
                    self.win_mod.win_install(
                        source_exe=exe_b, source_sha256=sha_b, store_path=str(store), commit="b" * 40, win_to=upg_to, reg_key=reg_key,
                        on_boundary=lambda st: (_ for _ in ()).throw(SimulatedCrash(st)) if st == "after_backup" else None,
                    )
                self.assertTrue(self.win_mod.recover_win_if_needed(upg_to, reg_key))
                self.assertEqual(self.win_mod.sha256_file(upg_to / "bin" / "helio.exe"), sha_a)
                self.assertFalse((upg_to / "bin" / "helio.exe.old").exists())

                # Complete upgrade to B, then interrupt win_back at after_exe and recover back to B.
                self.win_mod.win_install(source_exe=exe_b, source_sha256=sha_b, store_path=str(store), commit="b" * 40, win_to=upg_to, reg_key=reg_key)
                with self.assertRaises(SimulatedCrash):
                    self.win_mod.win_back(
                        win_to=upg_to, reg_key=reg_key,
                        on_boundary=lambda st: (_ for _ in ()).throw(SimulatedCrash(st)) if st == "after_exe" else None,
                    )
                self.assertTrue(self.win_mod.recover_win_if_needed(upg_to, reg_key))
                self.assertEqual(self.win_mod.sha256_file(upg_to / "bin" / "helio.exe"), sha_b)

                # Interrupt win_remove at after_path and finish removal cleanly via retry.
                with self.assertRaises(SimulatedCrash):
                    self.win_mod.win_remove(
                        win_to=upg_to, reg_key=reg_key,
                        on_boundary=lambda st: (_ for _ in ()).throw(SimulatedCrash(st)) if st == "after_path" else None,
                    )
                self.assertTrue(self.win_mod.recover_win_if_needed(upg_to, reg_key))
                self.assertFalse(upg_to.exists())

                # Recovery refuses to delete a user-created helio.cmd planted after an interrupted first install.
                ext_to = root / "crash_ext_file"
                with self.assertRaises(SimulatedCrash):
                    self.win_mod.win_install(
                        source_exe=exe_a, source_sha256=sha_a, store_path=str(store), commit="a" * 40, win_to=ext_to, reg_key=reg_key,
                        on_boundary=lambda st: (_ for _ in ()).throw(SimulatedCrash(st)) if st == "after_journal" else None,
                    )
                (ext_to / "helio.cmd").write_bytes(b"@echo user restored script\r\n")
                with self.assertRaises(self.win_mod.WinInstallError) as ctx:
                    self.win_mod.recover_win_if_needed(ext_to, reg_key)
                self.assertEqual(ctx.exception.code, 2)
                self.assertEqual((ext_to / "helio.cmd").read_bytes(), b"@echo user restored script\r\n")

                # Interrupted recovery when restoring A over busy B (pre-recording B's digest in journal BEFORE rename,
                # and retrying recovery after writing restored manifest before journal unlink) sweeps B cleanly!
                leg_j_to = root / "crash_recovery_busy_b"
                self.win_mod.win_install(source_exe=exe_a, source_sha256=sha_a, store_path=str(store), commit="a" * 40, win_to=leg_j_to, reg_key=reg_key)
                with self.assertRaises(SimulatedCrash):
                    self.win_mod.win_install(
                        source_exe=exe_b, source_sha256=sha_b, store_path=str(store), commit="b" * 40, win_to=leg_j_to, reg_key=reg_key,
                        on_boundary=lambda st: (_ for _ in ()).throw(SimulatedCrash(st)) if st == "after_exe" else None,
                    )
                jp_leg = leg_j_to / self.win_mod.WIN_JOURNAL_NAME
                j_busy = json.loads(jp_leg.read_text(encoding="utf-8"))
                tx_busy = j_busy["tx_id"]
                ret_b_name = f".helio.exe.{tx_busy}.rec.retired"
                # Crash during recovery right after _replace_win_exe renamed B aside and restored A (BEFORE manifest write!).
                real_replace = self.win_mod._replace_win_exe
                def crash_after_restore_replace(staged, target, tid, **kwargs):
                    res = real_replace(staged, target, tid, **kwargs)
                    if target.name == "helio.exe":
                        shutil.copy2(exe_b, target.parent / ret_b_name)
                        raise SimulatedCrash("after_restore_replace")
                    return res
                with mock.patch.object(self.win_mod, "_replace_win_exe", side_effect=crash_after_restore_replace):
                    with self.assertRaises(SimulatedCrash):
                        self.win_mod.recover_win_if_needed(leg_j_to, reg_key)
                # Journal already recorded B's digest on disk before rename -> retry sweeps B cleanly!
                j_after_crash = json.loads(jp_leg.read_text(encoding="utf-8"))
                self.assertEqual(j_after_crash["retired"].get(ret_b_name), sha_b)
                self.assertTrue(self.win_mod.recover_win_if_needed(leg_j_to, reg_key))
                self.assertEqual(self.win_mod.sha256_file(leg_j_to / "bin" / "helio.exe"), sha_a)
                self.assertFalse((leg_j_to / "bin" / ret_b_name).exists())
                self.win_mod.win_remove(win_to=leg_j_to, reg_key=reg_key)

                # Out-of-root journal key is refused by _validate_win_journal.
                with self.assertRaises(self.win_mod.WinInstallError) as ctx:
                    self.win_mod._validate_win_journal(
                        {
                            "schema_version": 2,
                            "op": "remove",
                            "phase": "publishing",
                            "tx_id": "abcdef012345",
                            "win_to": str(ext_to.resolve()),
                            "reg_key": reg_key,
                            "temp_files": [],
                            "expected_hashes": {"../outside": "0" * 64},
                        },
                        ext_to,
                        reg_key,
                    )
                self.assertEqual(ctx.exception.code, 2)

                # Real held-A-during-upgrade crash before manifest: A is retired to .<tx>.retired, B is live;
                # releasing A and retrying recovery restores A, sweeps retired A, and clears journal without collision!
                held_to = root / "crash_held_a_recovery"
                self.win_mod.win_install(source_exe=exe_a, source_sha256=sha_a, store_path=str(store), commit="a" * 40, win_to=held_to, reg_key=reg_key)
                create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                holder_a = subprocess.Popen(
                    [str(held_to / "bin" / "helio.exe"), "tell", "demo/lane", "held-a-rec", "--as", "demo/sender", "--store", str(store)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=create_no_window,
                )
                try:
                    self.assertIsNone(holder_a.poll())
                    with self.assertRaises(SimulatedCrash):
                        self.win_mod.win_install(
                            source_exe=exe_b, source_sha256=sha_b, store_path=str(store), commit="b" * 40, win_to=held_to, reg_key=reg_key,
                            on_boundary=lambda st: (_ for _ in ()).throw(SimulatedCrash(st)) if st == "after_exe" else None,
                        )
                    _o, err_a = holder_a.communicate(input="body\n", timeout=30)
                    self.assertEqual(holder_a.returncode, 0, err_a)
                finally:
                    if holder_a.poll() is None:
                        holder_a.kill()
                        holder_a.communicate()
                self.assertTrue(self.win_mod.recover_win_if_needed(held_to, reg_key))
                self.assertEqual(self.win_mod.sha256_file(held_to / "bin" / "helio.exe"), sha_a)
                self.assertFalse((held_to / self.win_mod.WIN_JOURNAL_NAME).exists())
                self.assertEqual([p.name for p in (held_to / "bin").glob("*.retired")], [])
                self.win_mod.win_remove(win_to=held_to, reg_key=reg_key)

                # Two destinations with distinct stores sharing one registry key:
                # concurrent installs serialize cleanly, BOTH Path entries are preserved, and the shadowed
                # second destination reports `shadowed_by` on install and exit 3 on win_status naming the winner!
                d1, d2 = root / "conc1", root / "conc2"
                s_d1, s_d2 = root / "store_d1", root / "store_d2"
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    f1 = pool.submit(self.win_mod.win_install, source_exe=self.helio_exe, source_sha256=exe_sha, store_path=str(s_d1), commit="a" * 40, win_to=d1, reg_key=reg_key)
                    f2 = pool.submit(self.win_mod.win_install, source_exe=self.helio_exe, source_sha256=exe_sha, store_path=str(s_d2), commit="a" * 40, win_to=d2, reg_key=reg_key)
                    r1, r2 = f1.result(), f2.result()
                _, val, _ = self.win_mod.read_reg_path(reg_key)
                self.assertIn(str(d1.resolve()), val)
                self.assertIn(str(d2.resolve()), val)
                # Exactly one is first on Path (status 0); the second is shadowed (shadowed_by set + status 3 naming winner!).
                first_d, second_d, second_res = (d1, d2, r2) if val.index(str(d1.resolve())) < val.index(str(d2.resolve())) else (d2, d1, r1)
                self.assertEqual(Path(second_res["shadowed_by"]).resolve(), (first_d / "helio.cmd").resolve())
                c_first, l_first = self.win_mod.win_status(win_to=first_d, reg_key=reg_key)
                self.assertEqual(c_first, 0, l_first)
                c_sec, l_sec = self.win_mod.win_status(win_to=second_d, reg_key=reg_key)
                self.assertEqual(c_sec, 3, l_sec)
                self.assertTrue(any(str((first_d / "helio.cmd").resolve()).lower() in ln.lower() for ln in l_sec), l_sec)
                self.win_mod.win_remove(win_to=d1, reg_key=reg_key)
                self.win_mod.win_remove(win_to=d2, reg_key=reg_key)
        finally:
            self._delete_reg_key(reg_key)


@unittest.skipIf(os.name == "nt", "WSL phase installer tests run on Linux/WSL")
class TestPhaseInstaller(unittest.TestCase):
    """Tests for `tools/install.py --phase <n>` (D01 T01 §17)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = load_installer()
        cls.phase_mod = cls.mod._load_phase_install()
        cls.win_mod = cls.mod._load_win_install()
        cls.helio_bin, cls.helio_exe = find_built_binaries()
        if not cls.helio_bin.is_file() or not cls.helio_exe.is_file():
            raise unittest.SkipTest("built binaries not found in build/linux")
        cls.python_exe = shutil.which("python.exe")
        cls.wslpath = shutil.which("wslpath")
        if not cls.python_exe or not cls.wslpath:
            raise unittest.SkipTest("WSL-to-Windows interop (python.exe / wslpath) not available")

    def _new_reg_key(self) -> str:
        return f"Software\\HeliographPhaseTest\\{secrets.token_hex(6)}"

    def _delete_reg_key(self, reg_key: str) -> None:
        code = (
            "import sys, winreg\n"
            "key = sys.argv[1]\n"
            "try:\n"
            "    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)\n"
            "except OSError:\n"
            "    pass\n"
        )
        subprocess.run([self.python_exe, "-B", "-c", code, reg_key], stdin=subprocess.DEVNULL, capture_output=True)

    def _win_temp_dir(self, name: str) -> str:
        code = (
            "import os, pathlib, sys, tempfile\n"
            "d = pathlib.Path(tempfile.mkdtemp(prefix='helio_phase_')) / sys.argv[1]\n"
            "print(str(d))\n"
        )
        res = subprocess.run([self.python_exe, "-B", "-c", code, name], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True)
        return res.stdout.strip().splitlines()[-1]

    def _cleanup_win_parent(self, win_dir: str) -> None:
        code = (
            "import pathlib, shutil, sys\n"
            "p = pathlib.Path(sys.argv[1]).parent\n"
            "shutil.rmtree(p, ignore_errors=True)\n"
        )
        subprocess.run([self.python_exe, "-B", "-c", code, win_dir], stdin=subprocess.DEVNULL, capture_output=True)

    def _make_distinct_pair(self, out_dir: Path, tag: bytes) -> tuple[Path, Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        h = out_dir / "helio"
        e = out_dir / "helio.exe"
        h.write_bytes(self.helio_bin.read_bytes() + b"\x00" * 16 + tag)
        e.write_bytes(self.helio_exe.read_bytes() + b"\x00" * 16 + tag)
        os.chmod(h, 0o755)
        os.chmod(e, 0o755)
        return (h, e)

    def _make_fixture_workspace(
        self,
        root: Path,
        *,
        plan_json: dict,
        closeout_text: str | None = "# Phase 0\n\n## Closeout run=abc123\n\nAll done.\n",
        app_build_mode: str = "ok",
        prove_mode: str = "ok",
        bin_tag: bytes = b"CAND_V1",
    ) -> tuple[Path, Path, Path]:
        """Create an isolated workspace with fixture checkout, bare origin remote, and stub ImpossibleSDK."""
        ws = root / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        origin = ws / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)

        checkout = ws / "Heliograph"
        checkout.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Test"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(checkout), "config", "user.email", "test@example.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", str(origin)], check=True, capture_output=True)
        (checkout / ".gitignore").write_text("build/\n__pycache__/\n", encoding="utf-8")

        # scripts/todo-graph.py returning plan_json
        scripts_dir = checkout / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "todo-graph.py").write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "data = json.loads((pathlib.Path(__file__).resolve().parent / 'plan_fixture.json').read_text(encoding='utf-8'))\n"
            "print(json.dumps(data))\n",
            encoding="utf-8",
        )
        (scripts_dir / "plan_fixture.json").write_text(json.dumps(plan_json), encoding="utf-8")

        if closeout_text is not None:
            runs_dir = checkout / "docs" / "phase-runs"
            runs_dir.mkdir(parents=True)
            (runs_dir / "2026-11-24-phase-0.md").write_text(closeout_text, encoding="utf-8")

        # Nonrecursive test inside fixture checkout that exercises IMPOSSIBLE_BUILD\helio.exe on Windows.
        tests_dir = checkout / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_fixture.py").write_text(
            "import os, pathlib, subprocess, unittest\n"
            "class FixtureProof(unittest.TestCase):\n"
            "    def test_helio_exe_runs_from_impossible_build(self):\n"
            "        b = pathlib.Path(os.environ['IMPOSSIBLE_BUILD']) / 'helio.exe'\n"
            "        self.assertTrue(b.is_file(), str(b))\n"
            "        res = subprocess.run([str(b), 'help'], capture_output=True, text=True)\n"
            "        self.assertEqual(res.returncode, 0, res.stderr)\n"
            "        self.assertIn('helio passes messages', res.stdout)\n",
            encoding="utf-8",
        )

        # Stage source binaries for stub SDK app_build.py
        src_bins = ws / "src_bins"
        h_src, e_src = self._make_distinct_pair(src_bins, bin_tag)

        # Stub ImpossibleSDK beside checkout
        sdk_tools = ws / "ImpossibleSDK" / "tools"
        sdk_tools.mkdir(parents=True)
        (sdk_tools / "app_build.py").write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, shutil, sys\n"
            "repo = pathlib.Path(sys.argv[1]).resolve()\n"
            "mode = (pathlib.Path(__file__).resolve().parent / 'app_build.mode').read_text(encoding='utf-8').strip()\n"
            "src_dir = repo.parent / 'src_bins'\n"
            "out_win = repo / 'build' / 'linux' / 'helio.exe'\n"
            "out_wsl = repo / 'build' / 'linux' / 'linux' / 'helio'\n"
            "out_wsl.parent.mkdir(parents=True, exist_ok=True)\n"
            "if not out_wsl.exists() or out_wsl.read_bytes() != (src_dir / 'helio').read_bytes():\n"
            "    shutil.copy2(src_dir / 'helio', out_wsl)\n"
            "if not out_win.exists() or out_win.read_bytes() != (src_dir / 'helio.exe').read_bytes():\n"
            "    shutil.copy2(src_dir / 'helio.exe', out_win)\n"
            "if '--no-test' in sys.argv:\n"
            "    sys.exit(0)\n"
            "if mode == 'fail':\n"
            "    print('FAIL: simulated build failure')\n"
            "    sys.exit(1)\n"
            "if mode == 'skip_race':\n"
            "    print('test_windows_and_linux_take_each_job_once (test_helio.Jobs) ... skipped \\'no windows\\'')\n"
            "    sys.exit(0)\n"
            "print('test_windows_and_linux_take_each_job_once (test_helio.Jobs) ... ok')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        (sdk_tools / "app_build.mode").write_text(app_build_mode, encoding="utf-8")

        (sdk_tools / "prove.py").write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, subprocess, sys\n"
            "repo = pathlib.Path(sys.argv[1]).resolve()\n"
            "mode = (pathlib.Path(__file__).resolve().parent / 'prove.mode').read_text(encoding='utf-8').strip()\n"
            "if mode == 'fail':\n"
            "    print('NOT proved on Windows: Heliograph')\n"
            "    sys.exit(1)\n"
            "sha = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', '--short', 'HEAD'], text=True).strip()\n"
            "print(f'proved on Windows: Heliograph {sha}')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        (sdk_tools / "prove.mode").write_text(prove_mode, encoding="utf-8")

        subprocess.run(["git", "-C", str(checkout), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(checkout), "commit", "-m", "fixture: phase 0 closed"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(checkout), "push", "-u", "origin", "main"], check=True, capture_output=True)
        return (checkout, origin, src_bins)

    def test_phase_cli_validation_docs_and_refusals(self) -> None:
        # 1. Docs & owning closeout skills all name `tools/install.py --phase`
        for rel in (
            "AGENTS.md",
            "docs/unattended-prompt.md",
            ".agents/skills/process-phase/SKILL.md",
            ".claude/skills/process-phase/SKILL.md",
            ".codex/skills/process-phase/SKILL.md",
        ):
            text = (REPO / rel).read_text(encoding="utf-8")
            self.assertIn("tools/install.py --phase", text, rel)

        # 2. Incompatible CLI combinations refuse with exit 2
        self.assertEqual(self.mod.main(["--phase", "0", "--windows"]), 2)
        self.assertEqual(self.mod.main(["--win-to", "C:\\Helio"]), 2)
        self.assertEqual(self.mod.main(["--phase", "-1"]), 2)
        self.assertEqual(self.mod.main(["--phase", "0", "--win-to", "C:\\Bad;Dir"]), 2)

        # 3. Missing WSL-to-Windows interop refuses with exit 3 before touching destinations
        with mock.patch.object(shutil, "which", return_value=None):
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.phase_mod._check_interop(self.mod)
            self.assertEqual(ctx.exception.code, 3)

        # 4. Pre-flight refusal matrix on fixture checkout (installed program untouched, zero notices)
        base_plan = {
            "phases": {"0": "Phase 0", "99": "Manual"},
            "sections": [
                {"ref": "D01 T01 §15", "phase": 0, "status": "verified"},
                {"ref": "D01 T01 §16", "phase": 0, "status": "verified"},
            ],
        }
        reg_key = self._new_reg_key()
        win_to = self._win_temp_dir("Helio Refusal")
        try:
            with tempfile.TemporaryDirectory(prefix="helio-phase-refuse-") as tmp:
                root = Path(tmp)
                checkout, origin, src_bins = self._make_fixture_workspace(root, plan_json=base_plan)
                to_dir = root / "wsl_bin"
                store_dir = root / "store"
                wrapper = root / "local_bin" / "helio"

                def call_phase(phase_num: int = 0, **kw):
                    return self.phase_mod.run_phase_install(
                        repo=checkout,
                        phase=phase_num,
                        to_dir=to_dir,
                        store_dir=store_dir,
                        wrapper_path=wrapper,
                        win_to_raw=win_to,
                        reg_key=reg_key,
                        sdk_url="unused",
                        install_mod=self.mod,
                        **kw,
                    )

                # Unknown phase & empty phase 99
                for bad_phase in (77, 99):
                    with self.assertRaises(self.mod.InstallError) as ctx:
                        call_phase(bad_phase)
                    self.assertEqual(ctx.exception.code, 2)

                # One open section in phase 0
                open_plan = {
                    "phases": {"0": "Phase 0"},
                    "sections": [
                        {"ref": "D01 T01 §15", "phase": 0, "status": "verified"},
                        {"ref": "D01 T01 §16", "phase": 0, "status": "open"},
                    ],
                }
                (checkout / "scripts" / "plan_fixture.json").write_text(json.dumps(open_plan), encoding="utf-8")
                subprocess.run(["git", "-C", str(checkout), "commit", "-am", "open"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "push", "origin", "main"], check=True, capture_output=True)
                with self.assertRaises(self.mod.InstallError) as ctx:
                    call_phase(0)
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("§16", str(ctx.exception))

                # Restore verified plan, remove Closeout heading -> refused
                (checkout / "scripts" / "plan_fixture.json").write_text(json.dumps(base_plan), encoding="utf-8")
                (checkout / "docs" / "phase-runs" / "2026-11-24-phase-0.md").write_text("# Phase 0\n\nNo closeout yet.\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(checkout), "commit", "-am", "unclosed"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "push", "origin", "main"], check=True, capture_output=True)
                with self.assertRaises(self.mod.InstallError) as ctx:
                    call_phase(0)
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("Closeout", str(ctx.exception))

                # PARKED phase -> refused
                (checkout / "docs" / "phase-runs" / "2026-11-24-phase-0.md").write_text(
                    "# Phase 0\n\nPARKED 2026-11-24T00:00Z run=abc waiting\n", encoding="utf-8"
                )
                subprocess.run(["git", "-C", str(checkout), "commit", "-am", "parked"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "push", "origin", "main"], check=True, capture_output=True)
                with self.assertRaises(self.mod.InstallError) as ctx:
                    call_phase(0)
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("PARKED", str(ctx.exception))

                # Older Closeout overridden by a newer dated run file with PARKED -> refused!
                (checkout / "docs" / "phase-runs" / "2026-11-24-phase-0.md").write_text(
                    "# Phase 0\n\n## Closeout run=old\n\nOld closeout.\n", encoding="utf-8"
                )
                (checkout / "docs" / "phase-runs" / "2026-11-25-phase-0.md").write_text(
                    "# Phase 0\n\nPARKED 2026-11-25T00:00Z run=new reopened-and-parked\n", encoding="utf-8"
                )
                subprocess.run(["git", "-C", str(checkout), "add", "docs/phase-runs/2026-11-25-phase-0.md"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "commit", "-am", "newer-parked"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "push", "origin", "main"], check=True, capture_output=True)
                with self.assertRaises(self.mod.InstallError) as ctx:
                    call_phase(0)
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("PARKED", str(ctx.exception))
                (checkout / "docs" / "phase-runs" / "2026-11-25-phase-0.md").unlink()
                subprocess.run(["git", "-C", str(checkout), "rm", "docs/phase-runs/2026-11-25-phase-0.md"], check=True, capture_output=True)

                # Restore Closeout, plant unpushed commit (HEAD != origin/main) -> refused
                (checkout / "docs" / "phase-runs" / "2026-11-24-phase-0.md").write_text(
                    "# Phase 0\n\n## Closeout run=abc\n\nDone.\n", encoding="utf-8"
                )
                subprocess.run(["git", "-C", str(checkout), "commit", "-am", "closed-unpushed"], check=True, capture_output=True)
                with self.assertRaises(self.mod.InstallError) as ctx:
                    call_phase(0)
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("origin/main", str(ctx.exception))

                # Push it, then plant dirty working tree -> refused
                subprocess.run(["git", "-C", str(checkout), "push", "origin", "main"], check=True, capture_output=True)
                (checkout / "dirty.txt").write_text("untracked\n", encoding="utf-8")
                with self.assertRaises(self.mod.InstallError) as ctx:
                    call_phase(0)
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("uncommitted", str(ctx.exception))
                (checkout / "dirty.txt").unlink()

                # app_build.py fails -> refused
                mode_ab = checkout.parent / "ImpossibleSDK" / "tools" / "app_build.mode"
                mode_pr = checkout.parent / "ImpossibleSDK" / "tools" / "prove.mode"
                mode_ab.write_text("fail", encoding="utf-8")
                with self.assertRaises(self.mod.InstallError) as ctx:
                    call_phase(0)
                self.assertEqual(ctx.exception.code, 3)

                # app_build.py skips race test -> refused
                mode_ab.write_text("skip_race", encoding="utf-8")
                with self.assertRaises(self.mod.InstallError) as ctx:
                    call_phase(0)
                self.assertEqual(ctx.exception.code, 3)
                self.assertIn("skipped", str(ctx.exception))
                mode_ab.write_text("ok", encoding="utf-8")

                # prove.py fails -> refused
                mode_pr.write_text("fail", encoding="utf-8")
                with self.assertRaises(self.mod.InstallError) as ctx:
                    call_phase(0)
                self.assertEqual(ctx.exception.code, 3)
                mode_pr.write_text("ok", encoding="utf-8")

                # Binary rebuilt/modified after pre-proof hash -> refused
                def tamper_after_hash(wsl_b: Path, win_b: Path) -> None:
                    win_b.write_bytes(win_b.read_bytes() + b"\x00")

                with self.assertRaises(self.mod.InstallError) as ctx:
                    call_phase(0, on_after_pre_proof_hash=tamper_after_hash)
                self.assertEqual(ctx.exception.code, 3)
                self.assertIn("rebuilt", str(ctx.exception))

                # Nothing was installed and no store/notice was created by any refusal!
                self.assertFalse(to_dir.exists())
                self.assertFalse(wrapper.exists())
                self.assertFalse(store_dir.exists())
        finally:
            self._delete_reg_key(reg_key)
            self._cleanup_win_parent(win_to)

    def test_phase_end_to_end_retry_rollback_crash_recovery_and_tell(self) -> None:
        base_plan = {
            "phases": {"0": "Phase 0"},
            "sections": [{"ref": "D01 T01 §15", "phase": 0, "status": "verified"}],
        }
        reg_key = self._new_reg_key()
        win_to = self._win_temp_dir("Helio Phase Dir")
        try:
            with tempfile.TemporaryDirectory(prefix="helio-phase-e2e-") as tmp:
                root = Path(tmp)
                checkout, origin, src_bins = self._make_fixture_workspace(root, plan_json=base_plan, bin_tag=b"PHASE_V2")
                to_dir = root / "wsl bin"
                store_dir = root / "phase store"
                wrapper = root / "local bin" / "helio"

                # Seed a distinct V1 installation on BOTH WSL and Windows so we have a real previous generation.
                v1_dir = root / "v1_bins"
                v1_h, v1_e = self._make_distinct_pair(v1_dir, b"PRIOR_V1")
                v1_h_sha = self.mod.sha256_file(v1_h)
                v1_e_sha = self.mod.sha256_file(v1_e)
                v1_commit = "1" * 40
                self.mod.install(
                    repo=checkout,
                    to_dir=to_dir,
                    store_dir=store_dir,
                    wrapper_path=wrapper,
                    prebuilt=(v1_h, v1_e),
                    commit_override=v1_commit,
                    dirty_override=False,
                )
                install_py_win = self.win_mod.wsl_to_windows_path(REPO / "tools" / "install.py")
                win_store = self.win_mod.wsl_to_windows_path(store_dir)
                w1 = self.phase_mod._run_win_cli(
                    self.python_exe,
                    install_py_win,
                    [
                        "--source-exe",
                        self.win_mod.wsl_to_windows_path(v1_e),
                        "--source-sha256",
                        v1_e_sha,
                        "--commit",
                        v1_commit,
                        "--store",
                        win_store,
                        "--to",
                        win_to,
                        "--reg-key",
                        reg_key,
                    ],
                )
                self.assertEqual(w1.returncode, 0, w1.stdout + w1.stderr)

                # Run full `--phase 0` via CLI with distinct nondefault WSL --to and spaced Windows --win-to!
                # Put wrapper.parent on PATH so fresh-shell verification resolves bare `helio` without prepending.
                path_with_wrapper = f"{wrapper.parent}:{os.environ.get('PATH', '/usr/bin:/bin')}"
                env_cli = {**os.environ, "PATH": path_with_wrapper, "HELIO_REPO": str(checkout), "HELIO_AS": "impossible/runner-a"}
                cli_res = subprocess.run(
                    [
                        sys.executable,
                        str(REPO / "tools" / "install.py"),
                        "--phase",
                        "0",
                        "--to",
                        str(to_dir),
                        "--win-to",
                        win_to,
                        "--store",
                        str(store_dir),
                        "--wrapper",
                        str(wrapper),
                        "--reg-key",
                        reg_key,
                    ],
                    env=env_cli,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(cli_res.returncode, 0, cli_res.stdout + cli_res.stderr)

                head_sha = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
                v2_h_sha = self.mod.sha256_file(src_bins / "helio")
                v2_e_sha = self.mod.sha256_file(src_bins / "helio.exe")

                # Verify both WSL and Windows manifests & live binaries match proved V2 and keep V1 as previous.
                wsl_m = self.mod.read_manifest(to_dir)
                self.assertEqual(wsl_m["current"]["commit"], head_sha)
                self.assertFalse(wsl_m["current"]["dirty"])
                self.assertEqual(wsl_m["current"]["helio_sha256"], v2_h_sha)
                self.assertEqual(wsl_m["previous"]["commit"], v1_commit)
                self.assertEqual(wsl_m["previous"]["helio_sha256"], v1_h_sha)

                win_install_py_win = self.win_mod.wsl_to_windows_path(REPO / "tools" / "win_install.py")
                win_st = self.phase_mod._probe_win_state(self.python_exe, win_install_py_win, win_to, reg_key)
                self.assertEqual(win_st["status_code"], 0, win_st["status_lines"])
                self.assertEqual(win_st["exe_sha"], v2_e_sha)
                self.assertEqual(win_st["manifest"]["current"]["commit"], head_sha)
                self.assertEqual(win_st["manifest"]["previous"]["commit"], v1_commit)

                # Verify exactly ONE notice arrived in another lane's inbox naming phase 0 and head_sha.
                msg_files_before = sorted((store_dir / "messages").glob("*.post"))
                self.assertEqual(len(msg_files_before), 1, msg_files_before)
                inbox_other = subprocess.run(
                    [str(wrapper), "--store", str(store_dir), "--as", "impossible/other"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertIn("phase 0", inbox_other.stdout.lower())
                self.assertIn(head_sha[:12], inbox_other.stdout)

                # Idempotent retry under a DIFFERENT ambient HELIO_AS: neither host changes, previous stays V1, same notice!
                with mock.patch.dict(os.environ, {"PATH": path_with_wrapper, "HELIO_AS": "impossible/runner-b"}):
                    retry_res = self.phase_mod.run_phase_install(
                        repo=checkout,
                        phase=0,
                        to_dir=to_dir,
                        store_dir=store_dir,
                        wrapper_path=wrapper,
                        win_to_raw=win_to,
                        reg_key=reg_key,
                        sdk_url="unused",
                        install_mod=self.mod,
                    )
                self.assertFalse(retry_res["wsl_changed"])
                self.assertFalse(retry_res["win_changed"])
                self.assertEqual(self.mod.read_manifest(to_dir)["previous"]["commit"], v1_commit)
                self.assertEqual(
                    self.phase_mod._probe_win_state(self.python_exe, win_install_py_win, win_to, reg_key)["manifest"]["previous"]["commit"],
                    v1_commit,
                )
                self.assertEqual(len(list((store_dir / "messages").glob("*.post"))), 1)

                # Prepare a V3 commit and plant a verification fault -> BOTH WSL and Windows roll back to V2 (head_sha) and exit 3!
                self._make_distinct_pair(src_bins, b"PHASE_V3")
                (checkout / "v3.txt").write_text("v3\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(checkout), "add", "v3.txt"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "commit", "-m", "v3"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "push", "origin", "main"], check=True, capture_output=True)

                def fail_verify(stage: str) -> None:
                    if stage == "before_roundtrip":
                        raise self.mod.InstallError("planted post-install verification fault", code=3)

                with mock.patch.dict(os.environ, {"PATH": path_with_wrapper}):
                    with self.assertRaises(self.mod.InstallError) as ctx:
                        self.phase_mod.run_phase_install(
                            repo=checkout,
                            phase=0,
                            to_dir=to_dir,
                            store_dir=store_dir,
                            wrapper_path=wrapper,
                            win_to_raw=win_to,
                            reg_key=reg_key,
                            sdk_url="unused",
                            install_mod=self.mod,
                            on_verify=fail_verify,
                        )
                self.assertEqual(ctx.exception.code, 3)
                # Both hosts restored to V2 (head_sha), journal cleaned up, zero new notices!
                self.assertEqual(self.mod.read_manifest(to_dir)["current"]["commit"], head_sha)
                self.assertEqual(self.mod.sha256_file(to_dir / "helio"), v2_h_sha)
                win_after_rb = self.phase_mod._probe_win_state(self.python_exe, win_install_py_win, win_to, reg_key)
                self.assertEqual(win_after_rb["manifest"]["current"]["commit"], head_sha)
                self.assertEqual(win_after_rb["exe_sha"], v2_e_sha)
                self.assertFalse((to_dir / self.phase_mod.PHASE_JOURNAL_NAME).exists())
                self.assertEqual(len(list((store_dir / "messages").glob("*.post"))), 1)

                # Missing wrapper.parent on PATH (while keeping WSL-to-Windows interop tools on PATH!) ->
                # reaches post-install fresh-shell check, fails with missing-PATH diagnostic, rolls back both hosts to V2, exits 3, zero new notices!
                path_without_wrapper = os.pathsep.join(
                    p for p in os.environ.get("PATH", "").split(os.pathsep) if p and Path(p).resolve() != wrapper.parent.resolve()
                )
                with mock.patch.dict(os.environ, {"PATH": path_without_wrapper}):
                    with self.assertRaises(self.mod.InstallError) as ctx_nopath:
                        self.phase_mod.run_phase_install(
                            repo=checkout,
                            phase=0,
                            to_dir=to_dir,
                            store_dir=store_dir,
                            wrapper_path=wrapper,
                            win_to_raw=win_to,
                            reg_key=reg_key,
                            sdk_url="unused",
                            install_mod=self.mod,
                        )
                self.assertEqual(ctx_nopath.exception.code, 3)
                self.assertIn("PATH", str(ctx_nopath.exception))
                self.assertNotIn("interop", str(ctx_nopath.exception).lower())
                self.assertEqual(self.mod.read_manifest(to_dir)["current"]["commit"], head_sha)
                self.assertEqual(
                    self.phase_mod._probe_win_state(self.python_exe, win_install_py_win, win_to, reg_key)["manifest"]["current"]["commit"],
                    head_sha,
                )
                self.assertEqual(len(list((store_dir / "messages").glob("*.post"))), 1)

                # Pending-notification resume: fail once at `before_tell`, verify journal stays `notifying`,
                # then retry resumes ONLY notification without reinstalling!
                v3_sha = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()

                def fail_tell(boundary: str) -> None:
                    if boundary == "before_tell":
                        raise self.mod.InstallError("simulated tell failure", code=3)

                with mock.patch.dict(os.environ, {"PATH": path_with_wrapper}):
                    with self.assertRaises(self.mod.InstallError):
                        self.phase_mod.run_phase_install(
                            repo=checkout,
                            phase=0,
                            to_dir=to_dir,
                            store_dir=store_dir,
                            wrapper_path=wrapper,
                            win_to_raw=win_to,
                            reg_key=reg_key,
                            sdk_url="unused",
                            install_mod=self.mod,
                            on_boundary=fail_tell,
                        )
                jp = to_dir / self.phase_mod.PHASE_JOURNAL_NAME
                self.assertTrue(jp.is_file())
                self.assertEqual(json.loads(jp.read_text(encoding="utf-8"))["phase_state"], "notifying")

                with mock.patch.dict(os.environ, {"PATH": path_with_wrapper}):
                    resumed = self.phase_mod.run_phase_install(
                        repo=checkout,
                        phase=0,
                        to_dir=to_dir,
                        store_dir=store_dir,
                        wrapper_path=wrapper,
                        win_to_raw=win_to,
                        reg_key=reg_key,
                        sdk_url="unused",
                        install_mod=self.mod,
                    )
                self.assertTrue(resumed["resumed_notification"])
                self.assertEqual(resumed["commit"], v3_sha)
                self.assertFalse(jp.exists())
                self.assertEqual(len(list((store_dir / "messages").glob("*.post"))), 2)

                # Clean up Windows installation cleanly
                self.phase_mod._run_win_cli(self.python_exe, install_py_win, ["--remove", "--to", win_to, "--reg-key", reg_key])
        finally:
            self._delete_reg_key(reg_key)
            self._cleanup_win_parent(win_to)


    def test_previous_program_reads_populated_store_written_by_candidate_and_preserves_keyed_sends(self) -> None:
        with tempfile.TemporaryDirectory(prefix="helio-compat-") as tmp:
            root = Path(tmp)
            to_dir = root / "bin"
            store_dir = root / "store"
            wrapper = root / "local" / "helio"
            prev_dir = root / "prev"
            prev_dir.mkdir()

            # Build GENUINE previous WSL (`helio`) AND Windows (`helio.exe`) binaries from git commit `c4de2fe`
            # (pre-Phase-0 baseline) using the pinned ImpossibleSDK build so BOTH hosts exercise a truly older reader!
            prev_ws = root / "prev_ws"
            prev_ws.mkdir()
            real_sdk = REPO.parent / "ImpossibleSDK"
            self.assertTrue((real_sdk / "tools" / "app_build.py").is_file(), "ImpossibleSDK required to build c4de2fe")
            os.symlink(real_sdk, prev_ws / "ImpossibleSDK")
            prev_src = prev_ws / "Heliograph"
            prev_src.mkdir()
            arch = subprocess.run(
                ["git", "-C", str(REPO), "archive", "c4de2fe", "CMakeLists.txt", "app.json", "sdk.json", "engine", "src", "tests/racer.c", "tests/racer_linux.c", "tests/racer_windows.c"],
                capture_output=True,
                check=True,
            )
            subprocess.run(["tar", "-x", "-C", str(prev_src)], input=arch.stdout, check=True)
            (prev_src / "tests" / "test_ok.py").write_text(
                "import unittest\nclass Ok(unittest.TestCase):\n    def test_ok(self): pass\n",
                encoding="utf-8",
            )
            subprocess.run(
                [sys.executable, str(prev_ws / "ImpossibleSDK" / "tools" / "app_build.py"), str(prev_src)],
                check=True,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            )
            prev_h = prev_dir / "helio"
            prev_e = prev_dir / "helio.exe"
            shutil.copy2(prev_src / "build" / "linux" / "linux" / "helio", prev_h)
            shutil.copy2(prev_src / "build" / "linux" / "helio.exe", prev_e)

            cand_h, cand_e = self.helio_bin, self.helio_exe
            self.assertNotEqual(self.mod.sha256_file(prev_h), self.mod.sha256_file(cand_h))
            self.assertNotEqual(self.mod.sha256_file(prev_e), self.mod.sha256_file(cand_e))

            # Clean env with HELIO_STORE stripped so ambient operator stores are NEVER touched!
            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("HELIO_STORE", "HELIO_DEFAULT_STORE", "HELIO_DEFAULT_STORE_HEX", "HELIO_AS")
            }

            # Install genuine previous engine (c4de2fe) on BOTH WSL and Windows, then replace both with candidate.
            install_py_win = self.win_mod.wsl_to_windows_path(REPO / "tools" / "install.py")
            win_to = self._win_temp_dir("Helio Compat Win")
            reg_key = self._new_reg_key()
            win_store = self.win_mod.wsl_to_windows_path(store_dir)
            try:
                self.mod.install(
                    repo=REPO,
                    to_dir=to_dir,
                    store_dir=store_dir,
                    wrapper_path=wrapper,
                    prebuilt=(prev_h, prev_e),
                    commit_override="c4de2fe" + "0" * 33,
                    dirty_override=False,
                )
                self.mod.install(
                    repo=REPO,
                    to_dir=to_dir,
                    store_dir=store_dir,
                    wrapper_path=wrapper,
                    prebuilt=(cand_h, cand_e),
                    commit_override="b" * 40,
                    dirty_override=False,
                )
                for exe_p, cmt in ((prev_e, "c4de2fe" + "0" * 33), (cand_e, "b" * 40)):
                    rw = self.phase_mod._run_win_cli(
                        self.python_exe,
                        install_py_win,
                        [
                            "--source-exe",
                            self.win_mod.wsl_to_windows_path(exe_p),
                            "--source-sha256",
                            self.mod.sha256_file(exe_p),
                            "--commit",
                            cmt,
                            "--store",
                            win_store,
                            "--to",
                            win_to,
                            "--reg-key",
                            reg_key,
                        ],
                    )
                    self.assertEqual(rw.returncode, 0, rw.stdout + rw.stderr)

                # Candidate populates the store: a heads-up, a taken+done job, and a keyed message.
                tell_id = subprocess.run(
                    [str(wrapper), "tell", "impossible/all", "compat heads-up", "--as", "impossible/a"],
                    env=clean_env,
                    input="hello from candidate\n",
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip().splitlines()[-1]
                job_id = subprocess.run(
                    [str(wrapper), "ask", "impossible/all", "compat job", "--as", "impossible/a"],
                    env=clean_env,
                    input="job body from candidate\n",
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip().splitlines()[-1]
                subprocess.run([str(wrapper), "take", job_id, "--as", "impossible/b"], env=clean_env, check=True, capture_output=True, text=True)
                subprocess.run([str(wrapper), "done", job_id, "finished", "--as", "impossible/b"], env=clean_env, check=True, capture_output=True, text=True)
                keyed_id = subprocess.run(
                    [str(wrapper), "tell", "impossible/all", "compat keyed", "--as", "impossible/a", "--key", "compat-k1"],
                    env=clean_env,
                    input="keyed body\n",
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip().splitlines()[-1]
                msg_count_before = len(list((store_dir / "messages").glob("*.post")))
                self.assertEqual(msg_count_before, 3)

                # Roll back BOTH WSL and Windows to genuine previous engine (c4de2fe)!
                self.mod.go_back(to_dir=to_dir, wrapper_path=wrapper)
                self.assertEqual(self.mod.sha256_file(to_dir / "helio"), self.mod.sha256_file(prev_h))
                rb_win = self.phase_mod._run_win_cli(
                    self.python_exe, install_py_win, ["--back", "--to", win_to, "--reg-key", reg_key]
                )
                self.assertEqual(rb_win.returncode, 0, rb_win.stdout + rb_win.stderr)
                win_install_py_win = self.win_mod.wsl_to_windows_path(REPO / "tools" / "win_install.py")
                st_win = self.phase_mod._probe_win_state(self.python_exe, win_install_py_win, win_to, reg_key)
                self.assertEqual(st_win["exe_sha"], self.mod.sha256_file(prev_e))

                # Verify restored WSL c4de2fe reader: check, show, job state/taker, and keyed-send idempotence!
                chk = subprocess.run([str(wrapper), "check"], cwd=str(store_dir.parent), env=clean_env, capture_output=True, text=True)
                self.assertEqual(chk.returncode, 0, chk.stdout + chk.stderr)
                show_tell = subprocess.run([str(wrapper), "show", tell_id, "--as", "impossible/b"], cwd=str(store_dir.parent), env=clean_env, capture_output=True, text=True, check=True).stdout
                self.assertIn("hello from candidate", show_tell)
                show_job = subprocess.run([str(wrapper), "show", job_id, "--as", "impossible/b"], cwd=str(store_dir.parent), env=clean_env, capture_output=True, text=True, check=True).stdout
                self.assertIn("job body from candidate", show_job)
                self.assertIn("impossible/b", show_job)
                self.assertIn("done", show_job.lower())
                retry_keyed_id = subprocess.run(
                    [str(wrapper), "tell", "impossible/all", "compat keyed", "--as", "impossible/a", "--key", "compat-k1"],
                    cwd=str(store_dir.parent),
                    env=clean_env,
                    input="keyed body\n",
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip().splitlines()[-1]
                self.assertEqual(retry_keyed_id, keyed_id)
                self.assertEqual(len(list((store_dir / "messages").glob("*.post"))), msg_count_before)

                # Verify restored Windows c4de2fe reader via Windows `helio.cmd` (check, show, job state, keyed-send dedup)!
                # Note: pre-§16 baseline `c4de2fe` predates `HELIO_DEFAULT_STORE`, so pass `--store <win_store>` via `helio.cmd`.
                w_chk = self.phase_mod._run_clean_win_helio(self.python_exe, reg_key, ["check", "--store", win_store])
                self.assertEqual(w_chk.returncode, 0, w_chk.stdout + w_chk.stderr)
                w_tell = self.phase_mod._run_clean_win_helio(self.python_exe, reg_key, ["show", tell_id, "--as", "impossible/b", "--store", win_store])
                self.assertEqual(w_tell.returncode, 0, w_tell.stdout + w_tell.stderr)
                self.assertIn("hello from candidate", w_tell.stdout)
                w_job = self.phase_mod._run_clean_win_helio(self.python_exe, reg_key, ["show", job_id, "--as", "impossible/b", "--store", win_store])
                self.assertEqual(w_job.returncode, 0, w_job.stdout + w_job.stderr)
                self.assertIn("job body from candidate", w_job.stdout)
                self.assertIn("impossible/b", w_job.stdout)
                self.assertIn("done", w_job.stdout.lower())
                w_keyed = self.phase_mod._run_clean_win_helio(
                    self.python_exe,
                    reg_key,
                    ["tell", "impossible/all", "compat keyed", "--as", "impossible/a", "--key", "compat-k1", "--store", win_store],
                    input_text="keyed body\n",
                )
                self.assertEqual(w_keyed.returncode, 0, w_keyed.stdout + w_keyed.stderr)
                self.assertEqual(w_keyed.stdout.strip().splitlines()[-1], keyed_id)
                self.assertEqual(len(list((store_dir / "messages").glob("*.post"))), msg_count_before)

                self.phase_mod._run_win_cli(self.python_exe, install_py_win, ["--remove", "--to", win_to, "--reg-key", reg_key])
            finally:
                self._delete_reg_key(reg_key)
                self._cleanup_win_parent(win_to)

    def test_phase_crash_recovery_between_hosts_mid_rollback_and_mixed_host(self) -> None:
        base_plan = {
            "phases": {"0": "Phase 0"},
            "sections": [{"ref": "D01 T01 §15", "phase": 0, "status": "verified"}],
        }
        reg_key = self._new_reg_key()
        win_to = self._win_temp_dir("Helio Crash")
        try:
            with tempfile.TemporaryDirectory(prefix="helio-phase-crash-") as tmp:
                root = Path(tmp)
                checkout, origin, src_bins = self._make_fixture_workspace(root, plan_json=base_plan, bin_tag=b"CRASH_V2")
                to_dir = root / "wsl_bin"
                store_dir = root / "store"
                wrapper = root / "local" / "helio"
                install_py_win = self.win_mod.wsl_to_windows_path(REPO / "tools" / "install.py")
                win_install_py_win = self.win_mod.wsl_to_windows_path(REPO / "tools" / "win_install.py")
                win_store = self.win_mod.wsl_to_windows_path(store_dir)

                # Seed V1 on both WSL and Windows.
                v1_h, v1_e = self._make_distinct_pair(root / "v1", b"CRASH_V1")
                v1_h_sha = self.mod.sha256_file(v1_h)
                v1_e_sha = self.mod.sha256_file(v1_e)
                v1_commit = "1" * 40
                self.mod.install(
                    repo=checkout,
                    to_dir=to_dir,
                    store_dir=store_dir,
                    wrapper_path=wrapper,
                    prebuilt=(v1_h, v1_e),
                    commit_override=v1_commit,
                    dirty_override=False,
                )
                w1 = self.phase_mod._run_win_cli(
                    self.python_exe,
                    install_py_win,
                    [
                        "--source-exe",
                        self.win_mod.wsl_to_windows_path(v1_e),
                        "--source-sha256",
                        v1_e_sha,
                        "--commit",
                        v1_commit,
                        "--store",
                        win_store,
                        "--to",
                        win_to,
                        "--reg-key",
                        reg_key,
                    ],
                )
                self.assertEqual(w1.returncode, 0, w1.stdout + w1.stderr)

                # 1. Remote drift during proof (origin/main advances) -> refused, both hosts stay on V1!
                def drift_origin(wsl_b: Path, win_b: Path) -> None:
                    other_clone = root / "other_clone"
                    subprocess.run(["git", "clone", str(origin), str(other_clone)], check=True, capture_output=True)
                    subprocess.run(["git", "-C", str(other_clone), "config", "user.name", "Other"], check=True, capture_output=True)
                    subprocess.run(["git", "-C", str(other_clone), "config", "user.email", "o@example.com"], check=True, capture_output=True)
                    (other_clone / "drift.txt").write_text("drift\n", encoding="utf-8")
                    subprocess.run(["git", "-C", str(other_clone), "add", "drift.txt"], check=True, capture_output=True)
                    subprocess.run(["git", "-C", str(other_clone), "commit", "-m", "drift"], check=True, capture_output=True)
                    subprocess.run(["git", "-C", str(other_clone), "push", "origin", "main"], check=True, capture_output=True)

                with self.assertRaises(self.mod.InstallError) as ctx:
                    self.phase_mod.run_phase_install(
                        repo=checkout,
                        phase=0,
                        to_dir=to_dir,
                        store_dir=store_dir,
                        wrapper_path=wrapper,
                        win_to_raw=win_to,
                        reg_key=reg_key,
                        sdk_url="unused",
                        install_mod=self.mod,
                        on_after_pre_proof_hash=drift_origin,
                    )
                self.assertEqual(ctx.exception.code, 2)
                self.assertEqual(self.mod.sha256_file(to_dir / "helio"), v1_h_sha)

                # Pull drift so checkout == origin/main again.
                subprocess.run(["git", "-C", str(checkout), "pull", "--rebase", "origin", "main"], check=True, capture_output=True)
                head_sha = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()

                # 2. Simulate crash BETWEEN WSL install and Windows install using a valid schema-2 journal
                # -> WSL rolls back to V1 and journal is removed!
                v2_h, v2_e = src_bins / "helio", src_bins / "helio.exe"
                self.mod.install(
                    repo=checkout,
                    to_dir=to_dir,
                    store_dir=store_dir,
                    wrapper_path=wrapper,
                    prebuilt=(v2_h, v2_e),
                    commit_override=head_sha,
                    dirty_override=False,
                )
                crashed_journal = {
                    "schema_version": 2,
                    "managed_by": "heliograph-phase-installer",
                    "tx_id": "aabbccddeeff",
                    "phase": 0,
                    "commit": head_sha,
                    "phase_state": "wsl_done",
                    "wsl_to": str(to_dir.resolve()),
                    "wrapper": str(wrapper.resolve()),
                    "store": str(store_dir.resolve()),
                    "win_store": win_store,
                    "win_to_arg": win_to,
                    "win_dest": win_to,
                    "reg_key": reg_key,
                    "proved_wsl_sha": self.mod.sha256_file(v2_h),
                    "proved_win_sha": self.mod.sha256_file(v2_e),
                    "wsl_had_install": True,
                    "wsl_pre_helio_sha": v1_h_sha,
                    "wsl_pre_commit": v1_commit,
                    "wsl_pre_store": str(store_dir.resolve()),
                    "wsl_pre_wrapper": str(wrapper.resolve()),
                    "win_had_install": True,
                    "win_pre_exe_sha": v1_e_sha,
                    "win_pre_commit": v1_commit,
                    "win_pre_store": win_store,
                    "wsl_changed": True,
                    "win_changed": False,
                }
                self.mod.write_json_atomic(to_dir / self.phase_mod.PHASE_JOURNAL_NAME, crashed_journal)
                self.phase_mod._recover_pending_phase_journal(
                    to_dir=to_dir,
                    wrapper_path=wrapper,
                    store_dir=store_dir,
                    win_to_raw=win_to,
                    reg_key=reg_key,
                    python_exe=self.python_exe,
                    install_py_win=install_py_win,
                    win_install_py_win=win_install_py_win,
                    install_mod=self.mod,
                )
                self.assertEqual(self.mod.sha256_file(to_dir / "helio"), v1_h_sha)
                self.assertEqual(
                    self.phase_mod._probe_win_state(self.python_exe, win_install_py_win, win_to, reg_key)["exe_sha"],
                    v1_e_sha,
                )
                self.assertFalse((to_dir / self.phase_mod.PHASE_JOURNAL_NAME).exists())

                # 3. Mixed-host update (WSL already on V2, Windows still on V1) + planted verification fault:
                # ONLY Windows rolls back to V1; WSL stays on V2 with its `previous` (V1) preserved!
                self.mod.install(
                    repo=checkout,
                    to_dir=to_dir,
                    store_dir=store_dir,
                    wrapper_path=wrapper,
                    prebuilt=(v2_h, v2_e),
                    commit_override=head_sha,
                    dirty_override=False,
                )

                def fail_after_win(stage: str) -> None:
                    if stage == "before_roundtrip":
                        raise self.mod.InstallError("planted mixed-host fault", code=3)

                with self.assertRaises(self.mod.InstallError) as ctx:
                    self.phase_mod.run_phase_install(
                        repo=checkout,
                        phase=0,
                        to_dir=to_dir,
                        store_dir=store_dir,
                        wrapper_path=wrapper,
                        win_to_raw=win_to,
                        reg_key=reg_key,
                        sdk_url="unused",
                        install_mod=self.mod,
                        on_verify=fail_after_win,
                    )
                self.assertEqual(ctx.exception.code, 3)
                # WSL was untouched (still V2 with previous=V1), Windows rolled back to V1!
                wsl_after = self.mod.read_manifest(to_dir)
                self.assertEqual(wsl_after["current"]["commit"], head_sha)
                self.assertEqual(wsl_after["previous"]["commit"], v1_commit)
                win_after = self.phase_mod._probe_win_state(self.python_exe, win_install_py_win, win_to, reg_key)
                self.assertEqual(win_after["exe_sha"], v1_e_sha)
                self.assertEqual(win_after["manifest"]["current"]["commit"], v1_commit)

                # 4. Ordinary --back / --remove refused with exit 3 while phase.journal.json exists!
                self.mod.write_json_atomic(to_dir / self.phase_mod.PHASE_JOURNAL_NAME, crashed_journal)
                with self.assertRaises(self.mod.InstallError) as ctx_b:
                    self.mod.go_back(to_dir=to_dir, wrapper_path=wrapper)
                self.assertEqual(ctx_b.exception.code, 3)
                with self.assertRaises(self.mod.InstallError) as ctx_r:
                    self.mod.remove(to_dir=to_dir, wrapper_path=wrapper)
                self.assertEqual(ctx_r.exception.code, 3)
                (to_dir / self.phase_mod.PHASE_JOURNAL_NAME).unlink()

                # 5. Legacy WSL pair without manifest: if phase install fails on Windows, rollback preserves
                # the legacy binary instead of deleting it!
                leg_to = root / "legacy_bin"
                leg_to.mkdir()
                leg_wrap = root / "leg_local" / "helio"
                leg_store = root / "leg_store"
                shutil.copy2(v1_h, leg_to / "helio")
                shutil.copy2(v1_e, leg_to / "helio.exe")
                with self.assertRaises(self.mod.InstallError) as ctx_leg:
                    self.phase_mod.run_phase_install(
                        repo=checkout,
                        phase=0,
                        to_dir=leg_to,
                        store_dir=leg_store,
                        wrapper_path=leg_wrap,
                        win_to_raw=win_to,
                        reg_key=reg_key,
                        sdk_url="unused",
                        install_mod=self.mod,
                        on_verify=fail_after_win,
                    )
                self.assertEqual(ctx_leg.exception.code, 3)
                self.assertTrue((leg_to / "helio").is_file())
                self.assertEqual(self.mod.sha256_file(leg_to / "helio"), v1_h_sha)

                # 6. Ambient WSL PATH shadowing triggers fresh-shell rollback!
                shadow_dir = root / "earlier_wsl_shadow"
                shadow_dir.mkdir()
                shadow_helio = shadow_dir / "helio"
                shadow_helio.write_text("#!/bin/sh\necho SHADOW\n", encoding="utf-8")
                os.chmod(shadow_helio, 0o755)
                ambient_path = f"{shadow_dir}:{wrapper.parent}:{os.environ.get('PATH', '/usr/bin:/bin')}"
                with mock.patch.dict(os.environ, {"PATH": ambient_path}):
                    with self.assertRaises(self.mod.InstallError) as ctx_sh:
                        self.phase_mod._run_clean_wsl_helio(
                            wrapper,
                            ["check"],
                            require_on_path=True,
                            install_mod=self.mod,
                        )
                    self.assertEqual(ctx_sh.exception.code, 3)
                    self.assertIn("shadows", str(ctx_sh.exception))

                # 7. Foreign/symlink/unbound-wrapper-or-store phase.journal.json refused with exit 2 BEFORE any side effects!
                foreign_to = root / "foreign_to"
                foreign_to.mkdir()
                foreign_jp = foreign_to / self.phase_mod.PHASE_JOURNAL_NAME
                foreign_jp.write_text(json.dumps({"schema_version": 1, "tx_id": "112233445566", "wsl_to": str(foreign_to)}), encoding="utf-8")
                with self.assertRaises(self.mod.InstallError) as ctx_fj:
                    self.phase_mod._recover_pending_phase_journal(
                        to_dir=foreign_to,
                        wrapper_path=wrapper,
                        store_dir=store_dir,
                        win_to_raw=win_to,
                        reg_key=reg_key,
                        python_exe=self.python_exe,
                        install_py_win=install_py_win,
                        win_install_py_win=win_install_py_win,
                        install_mod=self.mod,
                    )
                self.assertEqual(ctx_fj.exception.code, 2)
                self.assertTrue(foreign_jp.is_file())
                # Schema-2 journal pointing at a DIFFERENT wrapper/store is ALSO refused with exit 2!
                foreign_jp.write_text(
                    json.dumps(
                        {
                            **crashed_journal,
                            "schema_version": 2,
                            "managed_by": "heliograph-phase-installer",
                            "wsl_to": str(foreign_to.resolve()),
                            "wrapper": str(root / "evil_wrapper"),
                            "phase_state": "notifying",
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(self.mod.InstallError) as ctx_unbound:
                    self.phase_mod._recover_pending_phase_journal(
                        to_dir=foreign_to,
                        wrapper_path=wrapper,
                        store_dir=store_dir,
                        win_to_raw=win_to,
                        reg_key=reg_key,
                        python_exe=self.python_exe,
                        install_py_win=install_py_win,
                        win_install_py_win=win_install_py_win,
                        install_mod=self.mod,
                    )
                self.assertEqual(ctx_unbound.exception.code, 2)
                # Non-UTF-8 phase.journal.json -> refused cleanly with exit 2 (never unhandled UnicodeDecodeError)!
                foreign_jp.write_bytes(b"\xff\xfe\xfdnot-utf8")
                with self.assertRaises(self.mod.InstallError) as ctx_bin:
                    self.phase_mod._recover_pending_phase_journal(
                        to_dir=foreign_to,
                        wrapper_path=wrapper,
                        store_dir=store_dir,
                        win_to_raw=win_to,
                        reg_key=reg_key,
                        python_exe=self.python_exe,
                        install_py_win=install_py_win,
                        win_install_py_win=win_install_py_win,
                        install_mod=self.mod,
                    )
                self.assertEqual(ctx_bin.exception.code, 2)
                # Incomplete schema-2 journal (matching paths & managed_by but missing wsl_had_install) -> refused with exit 2!
                foreign_jp.write_text(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "managed_by": "heliograph-phase-installer",
                            "tx_id": "112233445566",
                            "phase_state": "wsl_done",
                            "wsl_to": str(foreign_to.resolve()),
                            "wrapper": str(wrapper.resolve()),
                            "store": str(store_dir.resolve()),
                            "win_to_arg": win_to,
                            "reg_key": reg_key,
                            "wsl_changed": True,
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(self.mod.InstallError) as ctx_inc2:
                    self.phase_mod._recover_pending_phase_journal(
                        to_dir=foreign_to,
                        wrapper_path=wrapper,
                        store_dir=store_dir,
                        win_to_raw=win_to,
                        reg_key=reg_key,
                        python_exe=self.python_exe,
                        install_py_win=install_py_win,
                        win_install_py_win=win_install_py_win,
                        install_mod=self.mod,
                    )
                self.assertEqual(ctx_inc2.exception.code, 2)
                foreign_jp.unlink()
                other_file = root / "other_target.json"
                other_file.write_text("{}", encoding="utf-8")
                foreign_jp.symlink_to(other_file)
                with self.assertRaises(self.mod.InstallError) as ctx_sym:
                    self.phase_mod._recover_pending_phase_journal(
                        to_dir=foreign_to,
                        wrapper_path=wrapper,
                        store_dir=store_dir,
                        win_to_raw=win_to,
                        reg_key=reg_key,
                        python_exe=self.python_exe,
                        install_py_win=install_py_win,
                        win_install_py_win=win_install_py_win,
                        install_mod=self.mod,
                    )
                self.assertEqual(ctx_sym.exception.code, 2)
                self.assertTrue(foreign_jp.is_symlink())

                # 8. Mid-transaction publication fault (foreign file at wrapper -> inner code 2) rolls back & exits 3!
                bad_wrap_dir = root / "bad_wrap_dir"
                bad_wrap_dir.mkdir()
                bad_wrap = bad_wrap_dir / "helio"
                bad_wrap.write_text("#!/bin/sh\necho FOREIGN\n", encoding="utf-8")
                os.chmod(bad_wrap, 0o755)
                bad_to = root / "bad_to"
                with self.assertRaises(self.mod.InstallError) as ctx_fw:
                    self.phase_mod.run_phase_install(
                        repo=checkout,
                        phase=0,
                        to_dir=bad_to,
                        store_dir=store_dir,
                        wrapper_path=bad_wrap,
                        win_to_raw=win_to,
                        reg_key=reg_key,
                        sdk_url="unused",
                        install_mod=self.mod,
                    )
                self.assertEqual(ctx_fw.exception.code, 3)

                # 9. Windows phase marker blocks concurrent `--windows --remove` with exit 3, AND recovering
                # an `acquiring` crash journal releases the marker cleanly!
                self.phase_mod._win_phase_marker_call(
                    self.python_exe, win_install_py_win, "begin", win_to, reg_key, "abcdef012345", phase=0, commit=head_sha, install_mod=self.mod
                )
                blk = self.phase_mod._run_win_cli(self.python_exe, install_py_win, ["--remove", "--to", win_to, "--reg-key", reg_key])
                self.assertEqual(blk.returncode, 3, blk.stdout + blk.stderr)
                acq_j = {
                    **crashed_journal,
                    "schema_version": 2,
                    "managed_by": "heliograph-phase-installer",
                    "tx_id": "abcdef012345",
                    "phase_state": "acquiring",
                    "wsl_changed": False,
                    "win_changed": False,
                }
                self.mod.write_json_atomic(to_dir / self.phase_mod.PHASE_JOURNAL_NAME, acq_j)
                self.phase_mod._recover_pending_phase_journal(
                    to_dir=to_dir,
                    wrapper_path=wrapper,
                    store_dir=store_dir,
                    win_to_raw=win_to,
                    reg_key=reg_key,
                    python_exe=self.python_exe,
                    install_py_win=install_py_win,
                    win_install_py_win=win_install_py_win,
                    install_mod=self.mod,
                )
                self.assertFalse((to_dir / self.phase_mod.PHASE_JOURNAL_NAME).exists())

                # 10. Narrowed remote.origin.fetch where origin/main is stale while FETCH_HEAD advanced -> refused (exit 2)!
                subprocess.run(
                    ["git", "-C", str(checkout), "config", "remote.origin.fetch", "+refs/heads/other:refs/remotes/origin/other"],
                    check=True,
                    capture_output=True,
                )
                other_clone2 = root / "other_clone2"
                subprocess.run(["git", "clone", str(origin), str(other_clone2)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(other_clone2), "config", "user.name", "Other"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(other_clone2), "config", "user.email", "o@example.com"], check=True, capture_output=True)
                (other_clone2 / "narrow.txt").write_text("narrow\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(other_clone2), "add", "narrow.txt"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(other_clone2), "commit", "-m", "narrow"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(other_clone2), "push", "origin", "main"], check=True, capture_output=True)
                with self.assertRaises(self.mod.InstallError) as ctx_nf:
                    self.phase_mod._check_clean_and_origin_main(checkout, self.mod)
                self.assertEqual(ctx_nf.exception.code, 2)
                self.assertIn("origin/main", str(ctx_nf.exception))

                # 11. Interrupted inner Windows publication of C over B (with predecessor A) on BOTH sides of live-file
                # replacement: phase rollback calls `win_recover` first and restores exact B with ZERO pending
                # `install.win.journal.json` (never double-rolling back to A)!
                v0_h, v0_e = self._make_distinct_pair(root / "v0", b"CRASH_V0_A")
                v0_e_sha = self.mod.sha256_file(v0_e)
                v3_h, v3_e = self._make_distinct_pair(root / "v3", b"CRASH_V3_C")
                v3_e_sha = self.mod.sha256_file(v3_e)
                win_c_to = self._win_temp_dir("Helio Win Crash B")
                reg_key_c = self._new_reg_key()
                try:
                    # Install A then B on Windows so B is live and A is `previous`.
                    for src_p, sha_p, cmt_p in ((v0_e, v0_e_sha, "0" * 40), (v1_e, v1_e_sha, v1_commit)):
                        r_ab = self.phase_mod._run_win_cli(
                            self.python_exe,
                            install_py_win,
                            ["--source-exe", self.win_mod.wsl_to_windows_path(src_p), "--source-sha256", sha_p, "--commit", cmt_p, "--store", win_store, "--to", win_c_to, "--reg-key", reg_key_c],
                        )
                        self.assertEqual(r_ab.returncode, 0, r_ab.stdout + r_ab.stderr)
                    for boundary_name in ("after_journal", "after_exe"):
                        # Acquire Windows phase marker as production does before interrupting inner win_install of C!
                        self.phase_mod._win_phase_marker_call(
                            self.python_exe, win_install_py_win, "begin", win_c_to, reg_key_c, "123456789abc", phase=0, commit="3" * 40, install_mod=self.mod
                        )
                        # Interrupt inner win_install of C at `boundary_name` leaving install.win.journal.json on disk!
                        crash_helper = (
                            "import importlib.util, pathlib, sys\n"
                            "spec = importlib.util.spec_from_file_location('wi', sys.argv[1])\n"
                            "mod = importlib.util.module_from_spec(spec)\n"
                            "spec.loader.exec_module(mod)\n"
                            "class Crash(BaseException):\n"
                            "    _leave_journal_for_test = True\n"
                            "def on_b(b):\n"
                            "    if b == sys.argv[6]:\n"
                            "        raise Crash('boom')\n"
                            "try:\n"
                            "    mod.win_install(source_exe=pathlib.Path(sys.argv[2]), source_sha256=sys.argv[3], "
                            "store_path=sys.argv[4], commit='3'*40, win_to=pathlib.Path(sys.argv[5]), reg_key=sys.argv[7], "
                            "phase_tx_id='123456789abc', on_boundary=on_b)\n"
                            "except Crash:\n"
                            "    pass\n"
                        )
                        subprocess.run(
                            [self.python_exe, "-B", "-c", crash_helper, win_install_py_win, self.win_mod.wsl_to_windows_path(v3_e), v3_e_sha, win_store, win_c_to, boundary_name, reg_key_c],
                            check=True,
                            stdin=subprocess.DEVNULL,
                            capture_output=True,
                        )
                        win_crash_j = {
                            "schema_version": 2,
                            "managed_by": "heliograph-phase-installer",
                            "tx_id": "123456789abc",
                            "phase": 0,
                            "commit": "3" * 40,
                            "phase_state": "both_done",
                            "wsl_to": str(to_dir.resolve()),
                            "wrapper": str(wrapper.resolve()),
                            "store": str(store_dir.resolve()),
                            "win_store": win_store,
                            "win_to_arg": win_c_to,
                            "win_dest": win_c_to,
                            "reg_key": reg_key_c,
                            "proved_wsl_sha": self.mod.sha256_file(to_dir / "helio"),
                            "proved_win_sha": v3_e_sha,
                            "wsl_had_install": True,
                            "wsl_pre_helio_sha": self.mod.sha256_file(to_dir / "helio"),
                            "wsl_pre_commit": head_sha,
                            "wsl_pre_store": str(store_dir.resolve()),
                            "wsl_pre_wrapper": str(wrapper.resolve()),
                            "win_had_install": True,
                            "win_pre_exe_sha": v1_e_sha,
                            "win_pre_commit": v1_commit,
                            "win_pre_store": win_store,
                            "wsl_changed": False,
                            "win_changed": True,
                        }
                        self.mod.write_json_atomic(to_dir / self.phase_mod.PHASE_JOURNAL_NAME, win_crash_j)
                        self.phase_mod._recover_pending_phase_journal(
                            to_dir=to_dir,
                            wrapper_path=wrapper,
                            store_dir=store_dir,
                            win_to_raw=win_c_to,
                            reg_key=reg_key_c,
                            python_exe=self.python_exe,
                            install_py_win=install_py_win,
                            win_install_py_win=win_install_py_win,
                            install_mod=self.mod,
                        )
                        st_after = self.phase_mod._probe_win_state(self.python_exe, win_install_py_win, win_c_to, reg_key_c)
                        self.assertEqual(st_after["status_code"], 0, f"{boundary_name}: {st_after['status_lines']}")
                        self.assertEqual(st_after["exe_sha"], v1_e_sha, boundary_name)
                        self.assertEqual(st_after["manifest"]["current"]["commit"], v1_commit, boundary_name)
                        self.assertEqual(st_after["manifest"]["previous"]["commit"], "0" * 40, boundary_name)
                    self.phase_mod._run_win_cli(self.python_exe, install_py_win, ["--remove", "--to", win_c_to, "--reg-key", reg_key_c])

                    # 12. Non-UTF-8 Windows `.phase-active.json` marker returns exit 2 cleanly (never UnicodeDecodeError).
                    bad_marker_helper = (
                        "import pathlib, sys\n"
                        "d = pathlib.Path(sys.argv[1]); d.mkdir(parents=True, exist_ok=True)\n"
                        "(d / '.phase-active.json').write_bytes(b'\\xff\\xfeinvalid-utf8')\n"
                    )
                    subprocess.run([self.python_exe, "-B", "-c", bad_marker_helper, win_c_to], check=True, stdin=subprocess.DEVNULL)
                    r_bad_m = self.phase_mod._run_win_cli(
                        self.python_exe,
                        install_py_win,
                        ["--source-exe", self.win_mod.wsl_to_windows_path(v1_e), "--source-sha256", v1_e_sha, "--commit", v1_commit, "--store", win_store, "--to", win_c_to, "--reg-key", reg_key_c],
                    )
                    self.assertEqual(r_bad_m.returncode, 2, r_bad_m.stdout + r_bad_m.stderr)
                    # Clean bad marker for next sub-test.
                    subprocess.run(
                        [self.python_exe, "-B", "-c", "import pathlib, sys; (pathlib.Path(sys.argv[1]) / '.phase-active.json').unlink(missing_ok=True)", win_c_to],
                        check=True,
                        stdin=subprocess.DEVNULL,
                    )

                    # 13. Interrupt production `_rollback_phase_journal` right after Windows marker release (`after_rollback_marker_end`),
                    # verify `phase_state == "rolled_back"` was persisted by production code, allow an ordinary Windows installer
                    # to publish a new installation with a different store, then run recovery and verify the later install is PRESERVED!
                    other_store = root / "later_writer_store"
                    other_store.mkdir()
                    other_win_store = self.win_mod.wsl_to_windows_path(other_store)
                    # First install v3 as a first-install on win_c_to, then interrupt rollback right after marker release.
                    r_first = self.phase_mod._run_win_cli(
                        self.python_exe,
                        install_py_win,
                        ["--source-exe", self.win_mod.wsl_to_windows_path(v3_e), "--source-sha256", v3_e_sha, "--commit", "3" * 40, "--store", win_store, "--to", win_c_to, "--reg-key", reg_key_c],
                    )
                    self.assertEqual(r_first.returncode, 0, r_first.stdout + r_first.stderr)
                    self.phase_mod._win_phase_marker_call(
                        self.python_exe, win_install_py_win, "begin", win_c_to, reg_key_c, "fedcba987654", phase=0, commit="3" * 40, install_mod=self.mod
                    )
                    rb_j = {
                        "schema_version": 2,
                        "managed_by": "heliograph-phase-installer",
                        "tx_id": "fedcba987654",
                        "phase": 0,
                        "commit": "3" * 40,
                        "phase_state": "both_done",
                        "wsl_to": str(to_dir.resolve()),
                        "wrapper": str(wrapper.resolve()),
                        "store": str(store_dir.resolve()),
                        "win_store": win_store,
                        "win_to_arg": win_c_to,
                        "win_dest": win_c_to,
                        "reg_key": reg_key_c,
                        "proved_wsl_sha": self.mod.sha256_file(to_dir / "helio"),
                        "proved_win_sha": v3_e_sha,
                        "wsl_had_install": True,
                        "wsl_pre_helio_sha": self.mod.sha256_file(to_dir / "helio"),
                        "wsl_pre_commit": head_sha,
                        "wsl_pre_store": str(store_dir.resolve()),
                        "wsl_pre_wrapper": str(wrapper.resolve()),
                        "win_had_install": False,
                        "win_pre_exe_sha": "",
                        "win_pre_commit": "",
                        "win_pre_store": "",
                        "wsl_changed": False,
                        "win_changed": True,
                    }
                    self.mod.write_json_atomic(to_dir / self.phase_mod.PHASE_JOURNAL_NAME, rb_j)

                    def stop_after_marker(b: str) -> None:
                        if b == "after_rollback_marker_end":
                            raise RuntimeError("simulated crash before journal unlink")

                    with self.assertRaises(RuntimeError):
                        self.phase_mod._rollback_phase_journal(
                            rb_j,
                            python_exe=self.python_exe,
                            install_py_win=install_py_win,
                            win_install_py_win=win_install_py_win,
                            install_mod=self.mod,
                            on_boundary=stop_after_marker,
                        )
                    persisted = json.loads((to_dir / self.phase_mod.PHASE_JOURNAL_NAME).read_text(encoding="utf-8"))
                    self.assertEqual(persisted["phase_state"], "rolled_back")
                    # Ordinary Windows installer now publishes same release with a DIFFERENT store!
                    r_later = self.phase_mod._run_win_cli(
                        self.python_exe,
                        install_py_win,
                        ["--source-exe", self.win_mod.wsl_to_windows_path(v3_e), "--source-sha256", v3_e_sha, "--commit", "3" * 40, "--store", other_win_store, "--to", win_c_to, "--reg-key", reg_key_c],
                    )
                    self.assertEqual(r_later.returncode, 0, r_later.stdout + r_later.stderr)
                    # Phase retry recovers `rolled_back` without re-entering rollback -> later installation is PRESERVED!
                    self.phase_mod._recover_pending_phase_journal(
                        to_dir=to_dir,
                        wrapper_path=wrapper,
                        store_dir=store_dir,
                        win_to_raw=win_c_to,
                        reg_key=reg_key_c,
                        python_exe=self.python_exe,
                        install_py_win=install_py_win,
                        win_install_py_win=win_install_py_win,
                        install_mod=self.mod,
                    )
                    self.assertFalse((to_dir / self.phase_mod.PHASE_JOURNAL_NAME).exists())
                    st_later = self.phase_mod._probe_win_state(self.python_exe, win_install_py_win, win_c_to, reg_key_c)
                    self.assertEqual(st_later["status_code"], 0, st_later["status_lines"])
                    self.assertEqual(st_later["manifest"]["current"]["store"], other_win_store)

                    # 14. Strict schema-2 lock-token ownership, deterministic repeated missing-marker refusal,
                    # missing-win_store refusal, and schema-1 refusal tests!
                    # (a) Repeated recovery against a missing Windows marker (for BOTH different store AND exact same
                    # release/digest/store) MUST deterministically refuse with exit 3 on TWO consecutive retries
                    # AND preserve `phase.journal.json` and the later Windows installation untouched!
                    for expected_store in (other_win_store, win_store):
                        if expected_store == win_store:
                            r_same = self.phase_mod._run_win_cli(
                                self.python_exe,
                                install_py_win,
                                ["--source-exe", self.win_mod.wsl_to_windows_path(v3_e), "--source-sha256", v3_e_sha, "--commit", "3" * 40, "--store", win_store, "--to", win_c_to, "--reg-key", reg_key_c],
                            )
                            self.assertEqual(r_same.returncode, 0, r_same.stdout + r_same.stderr)
                        missing_m_j = {**rb_j, "phase_state": "both_done"}
                        self.mod.write_json_atomic(to_dir / self.phase_mod.PHASE_JOURNAL_NAME, missing_m_j)
                        for attempt in (1, 2):
                            with self.assertRaises(self.mod.InstallError) as ctx_mm:
                                self.phase_mod._recover_pending_phase_journal(
                                    to_dir=to_dir,
                                    wrapper_path=wrapper,
                                    store_dir=store_dir,
                                    win_to_raw=win_c_to,
                                    reg_key=reg_key_c,
                                    python_exe=self.python_exe,
                                    install_py_win=install_py_win,
                                    win_install_py_win=win_install_py_win,
                                    install_mod=self.mod,
                                )
                            self.assertEqual(ctx_mm.exception.code, 3, f"attempt {attempt}")
                            self.assertIn("active phase marker", str(ctx_mm.exception))
                            self.assertTrue((to_dir / self.phase_mod.PHASE_JOURNAL_NAME).is_file(), f"attempt {attempt}")
                        # Direct `--remove --phase-tx` without an active marker is also refused inside Windows lock!
                        r_no_marker = self.phase_mod._run_win_cli(
                            self.python_exe,
                            install_py_win,
                            ["--remove", "--to", win_c_to, "--reg-key", reg_key_c, "--phase-tx", "fedcba987654"],
                        )
                        self.assertEqual(r_no_marker.returncode, 3, r_no_marker.stdout + r_no_marker.stderr)
                        st_still = self.phase_mod._probe_win_state(self.python_exe, win_install_py_win, win_c_to, reg_key_c)
                        self.assertEqual(st_still["status_code"], 0, st_still["status_lines"])
                        self.assertEqual(st_still["manifest"]["current"]["store"], expected_store)
                        (to_dir / self.phase_mod.PHASE_JOURNAL_NAME).unlink()

                    # (b) Schema-2 journal missing required `win_store` OR with non-integer `phase` -> refused with exit 2 before any mutation!
                    no_ws_j = {k: v for k, v in rb_j.items() if k != "win_store"}
                    for bad_j in (no_ws_j, {**rb_j, "phase_state": "notifying", "phase": "zero"}):
                        self.mod.write_json_atomic(to_dir / self.phase_mod.PHASE_JOURNAL_NAME, bad_j)
                        with self.assertRaises(self.mod.InstallError) as ctx_nows:
                            self.phase_mod._recover_pending_phase_journal(
                                to_dir=to_dir,
                                wrapper_path=wrapper,
                                store_dir=store_dir,
                                win_to_raw=win_c_to,
                                reg_key=reg_key_c,
                                python_exe=self.python_exe,
                                install_py_win=install_py_win,
                                win_install_py_win=win_install_py_win,
                                install_mod=self.mod,
                            )
                        self.assertEqual(ctx_nows.exception.code, 2)
                        self.assertTrue((to_dir / self.phase_mod.PHASE_JOURNAL_NAME).is_file())
                        (to_dir / self.phase_mod.PHASE_JOURNAL_NAME).unlink()

                    # (c) Unshipped pre-stamp draft `schema_version: 1` journals are refused with exit 2
                    # leaving journal, Windows marker, and both installations completely untouched!
                    self.phase_mod._win_phase_marker_call(
                        self.python_exe, win_install_py_win, "begin", win_c_to, reg_key_c, "111122223333", phase=0, commit="3" * 40, install_mod=self.mod
                    )
                    s1_refuse_j = {k: v for k, v in rb_j.items() if k != "managed_by"}
                    s1_refuse_j["schema_version"] = 1
                    self.mod.write_json_atomic(to_dir / self.phase_mod.PHASE_JOURNAL_NAME, s1_refuse_j)
                    wsl_before_s1 = self.mod.sha256_file(to_dir / "helio")
                    with self.assertRaises(self.mod.InstallError) as ctx_s1:
                        self.phase_mod._recover_pending_phase_journal(
                            to_dir=to_dir,
                            wrapper_path=wrapper,
                            store_dir=store_dir,
                            win_to_raw=win_c_to,
                            reg_key=reg_key_c,
                            python_exe=self.python_exe,
                            install_py_win=install_py_win,
                            win_install_py_win=win_install_py_win,
                            install_mod=self.mod,
                        )
                    self.assertEqual(ctx_s1.exception.code, 2)
                    self.assertTrue((to_dir / self.phase_mod.PHASE_JOURNAL_NAME).is_file())
                    self.assertEqual(self.mod.sha256_file(to_dir / "helio"), wsl_before_s1)
                    st_s1_untouched = self.phase_mod._probe_win_state(self.python_exe, win_install_py_win, win_c_to, reg_key_c)
                    self.assertEqual(st_s1_untouched["phase_marker_tx"], "111122223333")
                    self.assertEqual(st_s1_untouched["exe_sha"], v3_e_sha)
                    (to_dir / self.phase_mod.PHASE_JOURNAL_NAME).unlink()
                    self.phase_mod._win_phase_marker_call(
                        self.python_exe, win_install_py_win, "end", win_c_to, reg_key_c, "111122223333", install_mod=self.mod
                    )
                    self.phase_mod._run_win_cli(self.python_exe, install_py_win, ["--remove", "--to", win_c_to, "--reg-key", reg_key_c])
                finally:
                    self._delete_reg_key(reg_key_c)
                    self._cleanup_win_parent(win_c_to)

                # Clean up Windows installation cleanly
                self.phase_mod._run_win_cli(self.python_exe, install_py_win, ["--remove", "--to", win_to, "--reg-key", reg_key])
        finally:
            self._delete_reg_key(reg_key)
            self._cleanup_win_parent(win_to)

    def test_failed_phase_restores_both_current_and_previous_generations_on_both_hosts(self) -> None:
        """A -> B -> failed C restores current=B AND previous=A on both WSL and Windows so ordinary `--back` still restores A."""
        base_plan = {
            "phases": {"0": "Phase 0"},
            "sections": [{"ref": "D01 T01 §15", "phase": 0, "status": "verified"}],
        }
        reg_key = self._new_reg_key()
        win_to = self._win_temp_dir("Helio Prev Gen")
        try:
            with tempfile.TemporaryDirectory(prefix="helio-phase-prevgen-") as tmp:
                root = Path(tmp)
                checkout, _, _ = self._make_fixture_workspace(root, plan_json=base_plan, bin_tag=b"PREVGEN_C")
                to_dir = root / "wsl_bin"
                store_dir = root / "store"
                wrapper = root / "local" / "helio"
                install_py_win = self.win_mod.wsl_to_windows_path(REPO / "tools" / "install.py")
                win_install_py_win = self.win_mod.wsl_to_windows_path(REPO / "tools" / "win_install.py")
                win_store = self.win_mod.wsl_to_windows_path(store_dir)

                a_h, a_e = self._make_distinct_pair(root / "gen_a", b"GEN_A")
                b_h, b_e = self._make_distinct_pair(root / "gen_b", b"GEN_B")
                a_h_sha, a_e_sha = self.mod.sha256_file(a_h), self.mod.sha256_file(a_e)
                b_h_sha, b_e_sha = self.mod.sha256_file(b_h), self.mod.sha256_file(b_e)
                a_commit, b_commit = "a" * 40, "b" * 40

                # Install A then B on BOTH WSL and Windows.
                for hp, ep, cmt in ((a_h, a_e, a_commit), (b_h, b_e, b_commit)):
                    self.mod.install(
                        repo=checkout,
                        to_dir=to_dir,
                        store_dir=store_dir,
                        wrapper_path=wrapper,
                        prebuilt=(hp, ep),
                        commit_override=cmt,
                        dirty_override=False,
                    )
                    rw = self.phase_mod._run_win_cli(
                        self.python_exe,
                        install_py_win,
                        [
                            "--source-exe",
                            self.win_mod.wsl_to_windows_path(ep),
                            "--source-sha256",
                            self.mod.sha256_file(ep),
                            "--commit",
                            cmt,
                            "--store",
                            win_store,
                            "--to",
                            win_to,
                            "--reg-key",
                            reg_key,
                        ],
                    )
                    self.assertEqual(rw.returncode, 0, rw.stdout + rw.stderr)

                # Plant foreign backup-shaped files, operator diagnostic copies (including a genuine
                # interrupted temp file copied from a DIFFERENT transaction `other_tx` on BOTH WSL and Windows!),
                # AND real pre-replace atomic-writer temp files for THIS transaction `fixed_tx` produced by
                # each host's OWN writer (`write_bytes_atomic` / `_write_bytes_atomic`) BEFORE running the
                # failed-C upgrade + rollback (items 2, 9, 23, and 27).
                fixed_tx = "deadbeef1234"
                other_tx = "112233445566"
                wsl_foreign_copy = to_dir / f".phase-prev-{fixed_tx}.helio.old"
                wsl_foreign_empty = to_dir / f".phase-prev-{fixed_tx}.win.helio.exe.old"
                wsl_op_saved = to_dir / ".phase.journal.json.tmp.saved"
                wsl_foreign_copy.write_bytes(a_h.read_bytes())
                wsl_foreign_empty.write_bytes(b"")
                wsl_op_saved.write_bytes(b"operator diagnostic journal copy\n")
                wsl_real_tmps: list[Path] = []
                wsl_other_tx_tmps: list[Path] = []
                with mock.patch.object(os, "replace", side_effect=lambda src, _dst: wsl_real_tmps.append(Path(src))):
                    self.mod.write_bytes_atomic(to_dir / "phase.journal.json", b'{"partial": true}', tx_id=fixed_tx)
                    self.mod.write_bytes_atomic(to_dir / "helio.old", b"partial-helio-old", tx_id=fixed_tx)
                    self.mod.write_bytes_atomic(to_dir / "helio.exe.old", b"partial-helio-exe-old", tx_id=fixed_tx)
                with mock.patch.object(os, "replace", side_effect=lambda src, _dst: wsl_other_tx_tmps.append(Path(src))):
                    self.mod.write_bytes_atomic(to_dir / "helio.old", b"other-tx-wsl-copy", tx_id=other_tx)
                self.assertEqual(len(wsl_real_tmps), 3)
                self.assertEqual(len(wsl_other_tx_tmps), 1)
                for tp in wsl_real_tmps + wsl_other_tx_tmps:
                    self.assertTrue(tp.is_file(), f"real WSL writer temp {tp} must exist before rollback")
                plant_win_foreign = (
                    "import importlib.util, json, os, pathlib, sys\n"
                    "from unittest import mock\n"
                    "spec = importlib.util.spec_from_file_location('wi', sys.argv[4])\n"
                    "mod = importlib.util.module_from_spec(spec)\n"
                    "spec.loader.exec_module(mod)\n"
                    "b = pathlib.Path(sys.argv[1]) / 'bin'\n"
                    "tx, other_tx = sys.argv[3], sys.argv[5]\n"
                    "(b / f'.phase-prev-{tx}.helio.exe.old').write_bytes(pathlib.Path(sys.argv[2]).read_bytes())\n"
                    "(b / '.phase-prev-foreignempty.helio.exe.old').write_bytes(b'')\n"
                    "(b / '.helio.exe.old.tmp.saved').write_bytes(b'operator diagnostic win copy')\n"
                    "(b / '.helio.exe.old.tmp.99999.deadbeef').write_bytes(b'wsl-format foreign copy on windows')\n"
                    "captured, other_captured = [], []\n"
                    "with mock.patch.object(os, 'replace', side_effect=lambda src, _dst: captured.append(pathlib.Path(src))):\n"
                    "    mod._write_bytes_atomic(b / 'helio.exe.old', b'partial-win-old', tx_id=tx)\n"
                    "with mock.patch.object(os, 'replace', side_effect=lambda src, _dst: other_captured.append(pathlib.Path(src))):\n"
                    "    mod._write_bytes_atomic(b / 'helio.exe.old', b'other-tx-win-copy', tx_id=other_tx)\n"
                    "assert len(captured) == 1 and captured[0].is_file()\n"
                    "assert len(other_captured) == 1 and other_captured[0].is_file()\n"
                    "print(json.dumps({'own': captured[0].name, 'other': other_captured[0].name}))\n"
                )
                win_tmps = json.loads(
                    subprocess.check_output(
                        [self.python_exe, "-B", "-c", plant_win_foreign, win_to, self.win_mod.wsl_to_windows_path(a_e), fixed_tx, win_install_py_win, other_tx],
                        text=True,
                        stdin=subprocess.DEVNULL,
                    ).strip()
                )
                win_tmp_name = win_tmps["own"]
                win_other_tmp_name = win_tmps["other"]
                self.assertIn(f".tx-{fixed_tx}.", win_tmp_name)
                self.assertIn(f".tx-{other_tx}.", win_other_tmp_name)

                # Run phase C with a planted verification fault -> must publish C on BOTH hosts first,
                # reach the planted fault, and then roll back BOTH hosts to current=B, previous=A!
                seen_boundaries: list[str] = []
                verify_called: list[str] = []

                def record_boundary(name: str) -> None:
                    seen_boundaries.append(name)

                def fail_verify(stage: str) -> None:
                    verify_called.append(stage)
                    raise self.mod.InstallError("planted live verification fault", code=3)

                with mock.patch.object(self.phase_mod.secrets, "token_hex", return_value=fixed_tx):
                    with self.assertRaises(self.mod.InstallError) as ctx:
                        self.phase_mod.run_phase_install(
                            repo=checkout,
                            phase=0,
                            to_dir=to_dir,
                            store_dir=store_dir,
                            wrapper_path=wrapper,
                            win_to_raw=win_to,
                            reg_key=reg_key,
                            sdk_url="unused",
                            install_mod=self.mod,
                            on_boundary=record_boundary,
                            on_verify=fail_verify,
                        )
                self.assertEqual(ctx.exception.code, 3)
                self.assertIn("after_win_install", seen_boundaries, "must publish C on Windows before rolling back")
                self.assertTrue(verify_called, "must reach planted live verification fault after publication")
                self.assertIn("planted live verification fault", str(ctx.exception))

                # Verify BOTH WSL and Windows foreign files AND other-tx temps survived byte-for-byte
                # while ALL real pre-replace atomic-writer temp files for THIS transaction `fixed_tx` were swept!
                self.assertEqual(wsl_foreign_copy.read_bytes(), a_h.read_bytes())
                self.assertEqual(wsl_foreign_empty.read_bytes(), b"")
                self.assertEqual(wsl_op_saved.read_bytes(), b"operator diagnostic journal copy\n")
                self.assertEqual(wsl_other_tx_tmps[0].read_bytes(), b"other-tx-wsl-copy")
                for tp in wsl_real_tmps:
                    self.assertFalse(tp.exists(), f"real WSL writer temp {tp} must be swept")
                self.assertEqual(
                    sorted(p.name for p in to_dir.iterdir() if "phase-prev" in p.name),
                    sorted([wsl_foreign_copy.name, wsl_foreign_empty.name]),
                )
                wsl_foreign_copy.unlink()
                wsl_foreign_empty.unlink()
                wsl_op_saved.unlink()
                wsl_other_tx_tmps[0].unlink()
                check_win_foreign = (
                    "import pathlib, sys\n"
                    "b = pathlib.Path(sys.argv[1]) / 'bin'\n"
                    "expected = pathlib.Path(sys.argv[2]).read_bytes()\n"
                    "tx, real_tmp, other_tmp = sys.argv[3], sys.argv[4], sys.argv[5]\n"
                    "p1 = b / f'.phase-prev-{tx}.helio.exe.old'\n"
                    "p2 = b / '.phase-prev-foreignempty.helio.exe.old'\n"
                    "p3 = b / '.helio.exe.old.tmp.saved'\n"
                    "p4 = b / '.helio.exe.old.tmp.99999.deadbeef'\n"
                    "p5 = b / real_tmp\n"
                    "p6 = b / other_tmp\n"
                    "assert p1.is_file() and p1.read_bytes() == expected, 'Windows foreign copy must survive rollback verbatim'\n"
                    "assert p2.is_file() and p2.read_bytes() == b'', 'Windows foreign empty file must survive rollback verbatim'\n"
                    "assert p3.is_file() and p3.read_bytes() == b'operator diagnostic win copy', 'Windows .tmp.saved must survive verbatim'\n"
                    "assert p4.is_file() and p4.read_bytes() == b'wsl-format foreign copy on windows', 'WSL-format foreign copy on Windows must survive verbatim'\n"
                    "assert not p5.exists(), f'real Windows writer temp {p5} must be swept'\n"
                    "assert p6.is_file() and p6.read_bytes() == b'other-tx-win-copy', 'other-tx Windows temp copy must survive verbatim'\n"
                    "assert sorted(p.name for p in b.iterdir() if 'phase-prev' in p.name) == sorted([p1.name, p2.name])\n"
                    "p1.unlink(); p2.unlink(); p3.unlink(); p4.unlink(); p6.unlink()\n"
                )
                subprocess.run(
                    [self.python_exe, "-B", "-c", check_win_foreign, win_to, self.win_mod.wsl_to_windows_path(a_e), fixed_tx, win_tmp_name, win_other_tmp_name],
                    check=True,
                    stdin=subprocess.DEVNULL,
                )

                # Verify WSL has current=B, previous=A, and ordinary `--back` restores A!
                wsl_m = self.mod.read_manifest(to_dir)
                self.assertEqual(wsl_m["current"]["commit"], b_commit)
                self.assertEqual(wsl_m["previous"]["commit"], a_commit)
                self.assertEqual(self.mod.sha256_file(to_dir / "helio"), b_h_sha)
                self.assertEqual(self.mod.sha256_file(to_dir / "helio.old"), a_h_sha)
                self.mod.go_back(to_dir=to_dir, wrapper_path=wrapper)
                self.assertEqual(self.mod.sha256_file(to_dir / "helio"), a_h_sha)
                self.assertEqual(self.mod.read_manifest(to_dir)["current"]["commit"], a_commit)

                # Verify Windows has current=B, previous=A, and ordinary `--back` restores A!
                st_win = self.phase_mod._probe_win_state(self.python_exe, win_install_py_win, win_to, reg_key)
                self.assertEqual(st_win["status_code"], 0, st_win["status_lines"])
                self.assertEqual(st_win["exe_sha"], b_e_sha)
                self.assertEqual(st_win["manifest"]["current"]["commit"], b_commit)
                self.assertEqual(st_win["manifest"]["previous"]["commit"], a_commit)
                rb_win = self.phase_mod._run_win_cli(
                    self.python_exe, install_py_win, ["--back", "--to", win_to, "--reg-key", reg_key]
                )
                self.assertEqual(rb_win.returncode, 0, rb_win.stdout + rb_win.stderr)
                st_win_a = self.phase_mod._probe_win_state(self.python_exe, win_install_py_win, win_to, reg_key)
                self.assertEqual(st_win_a["status_code"], 0, st_win_a["status_lines"])
                self.assertEqual(st_win_a["exe_sha"], a_e_sha)
                self.assertEqual(st_win_a["manifest"]["current"]["commit"], a_commit)
                # Also test FIRST upgrade (B sole generation, previous=None -> failed C): rollback must clear
                # `previous` on BOTH hosts so ordinary `--back` refuses and never activates failed C!
                sole_to = root / "sole_wsl_bin"
                sole_wrap = root / "sole_local" / "helio"
                sole_win_to = self._win_temp_dir("Helio Sole Gen")
                sole_reg_key = self._new_reg_key()
                try:
                    self.mod.install(
                        repo=checkout,
                        to_dir=sole_to,
                        store_dir=store_dir,
                        wrapper_path=sole_wrap,
                        prebuilt=(b_h, b_e),
                        commit_override=b_commit,
                        dirty_override=False,
                    )
                    rw_sole = self.phase_mod._run_win_cli(
                        self.python_exe,
                        install_py_win,
                        [
                            "--source-exe",
                            self.win_mod.wsl_to_windows_path(b_e),
                            "--source-sha256",
                            b_e_sha,
                            "--commit",
                            b_commit,
                            "--store",
                            win_store,
                            "--to",
                            sole_win_to,
                            "--reg-key",
                            sole_reg_key,
                        ],
                    )
                    self.assertEqual(rw_sole.returncode, 0, rw_sole.stdout + rw_sole.stderr)
                    with self.assertRaises(self.mod.InstallError) as ctx_sole:
                        self.phase_mod.run_phase_install(
                            repo=checkout,
                            phase=0,
                            to_dir=sole_to,
                            store_dir=store_dir,
                            wrapper_path=sole_wrap,
                            win_to_raw=sole_win_to,
                            reg_key=sole_reg_key,
                            sdk_url="unused",
                            install_mod=self.mod,
                            on_verify=fail_verify,
                        )
                    self.assertEqual(ctx_sole.exception.code, 3)
                    # WSL: current=B, previous=None, helio.old absent, ordinary `--back` refuses!
                    self.assertIsNone(self.mod.read_manifest(sole_to).get("previous"))
                    self.assertFalse((sole_to / "helio.old").exists())
                    with self.assertRaises(self.mod.InstallError):
                        self.mod.go_back(to_dir=sole_to, wrapper_path=sole_wrap)
                    # Windows: current=B, previous=None, ordinary `--back` refuses!
                    st_sole_w = self.phase_mod._probe_win_state(self.python_exe, win_install_py_win, sole_win_to, sole_reg_key)
                    self.assertEqual(st_sole_w["status_code"], 0, st_sole_w["status_lines"])
                    self.assertEqual(st_sole_w["manifest"]["current"]["commit"], b_commit)
                    self.assertIsNone(st_sole_w["manifest"].get("previous"))
                    rb_sole_w = self.phase_mod._run_win_cli(
                        self.python_exe, install_py_win, ["--back", "--to", sole_win_to, "--reg-key", sole_reg_key]
                    )
                    self.assertNotEqual(rb_sole_w.returncode, 0)
                    self.phase_mod._run_win_cli(self.python_exe, install_py_win, ["--remove", "--to", sole_win_to, "--reg-key", sole_reg_key])
                finally:
                    self._delete_reg_key(sole_reg_key)
                    self._cleanup_win_parent(sole_win_to)

                # Verify initial journal publication via O_TMPFILE + /proc/self/fd link (items 2, 9, & 32):
                # 1) Interrupting `_create_initial_phase_journal` right before `os.link` leaves ZERO directory
                #    entries on disk (no temp file, no empty/partial journal);
                # 2) Any foreign file in `to_dir` (empty 0-byte marker OR copied acquiring journal at
                #    `.phase.journal.init.json`) survives a full `run_phase_install` retry byte-for-byte
                #    because the installer never creates or unlinks any initial staging filename!
                init_dir = root / "init_otmpfile_check"
                init_dir.mkdir(parents=True, exist_ok=True)
                pre_link_entries: list[list[str]] = []
                link_calls: list[tuple[str, str, dict[str, Any]]] = []

                def abort_before_link(src: str, dst: str, **kw: Any) -> None:
                    pre_link_entries.append([p.name for p in init_dir.iterdir()])
                    link_calls.append((src, dst, kw))
                    raise RuntimeError("killed before os.link")

                with mock.patch.object(os, "link", side_effect=abort_before_link):
                    with self.assertRaisesRegex(RuntimeError, "killed before os.link"):
                        self.phase_mod._create_initial_phase_journal(
                            init_dir / self.phase_mod.PHASE_JOURNAL_NAME,
                            {"commit": a_commit, "managed_by": "heliograph-phase-installer", "phase_state": "acquiring", "schema_version": 2, "tx_id": "aabbccddeeff"},
                            self.mod,
                        )
                self.assertEqual(pre_link_entries, [[]], "O_TMPFILE must leave zero directory entries before os.link")
                self.assertEqual(len(link_calls), 1)
                self.assertEqual(link_calls[0][1], self.phase_mod.PHASE_JOURNAL_NAME)
                self.assertIsInstance(link_calls[0][2].get("dst_dir_fd"), int)
                self.assertIs(link_calls[0][2].get("follow_symlinks"), True)
                self.assertEqual(list(init_dir.iterdir()), [], "aborted O_TMPFILE write must leave zero files on disk")
                copied_acquiring = (
                    json.dumps(
                        {
                            "commit": a_commit,
                            "managed_by": "heliograph-phase-installer",
                            "phase_state": "acquiring",
                            "schema_version": 2,
                            "tx_id": "aabbccddeeff",
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8")
                for foreign_tag, foreign_bytes in (("empty", b""), ("copied_journal", copied_acquiring)):
                    foreign_init = to_dir / ".phase.journal.init.json"
                    foreign_init.write_bytes(foreign_bytes)
                    retry_bounds: list[str] = []
                    retry_verify: list[str] = []

                    def retry_rec_bound(name: str, _b: list[str] = retry_bounds) -> None:
                        _b.append(name)

                    def retry_fail_verify(stage: str, _v: list[str] = retry_verify) -> None:
                        _v.append(stage)
                        raise self.mod.InstallError("planted live verification fault", code=3)

                    with self.assertRaises(self.mod.InstallError) as retry_ctx:
                        self.phase_mod.run_phase_install(
                            repo=checkout,
                            phase=0,
                            to_dir=to_dir,
                            store_dir=store_dir,
                            wrapper_path=wrapper,
                            win_to_raw=win_to,
                            reg_key=reg_key,
                            sdk_url="unused",
                            install_mod=self.mod,
                            on_boundary=retry_rec_bound,
                            on_verify=retry_fail_verify,
                        )
                    self.assertEqual(retry_ctx.exception.code, 3)
                    self.assertIn("after_win_install", retry_bounds, f"{foreign_tag}: retry must publish on Windows")
                    self.assertTrue(retry_verify, f"{foreign_tag}: retry must reach planted live verification fault")
                    self.assertIn("planted live verification fault", str(retry_ctx.exception))
                    self.assertEqual(
                        foreign_init.read_bytes(),
                        foreign_bytes,
                        f"{foreign_tag}: foreign .phase.journal.init.json must survive untouched verbatim",
                    )
                    self.assertFalse((to_dir / self.phase_mod.PHASE_JOURNAL_NAME).exists())
                    foreign_init.unlink()

                # Also verify `_store_needs_init` uses `os.stat` (preserving OS errors even on Python 3.14!)
                # and propagates PermissionError for local Windows stores, reachable WSL shares, AND WSL
                # distribution-root ACL denials (items 24, 25, and 28), returning False ONLY on genuine
                # unreachable-share WinErrors (e.g. WinError 64)!
                local_store_err_code = (
                    "import importlib.util, os, stat, sys, types\n"
                    "from unittest import mock\n"
                    "spec = importlib.util.spec_from_file_location('wi', sys.argv[1])\n"
                    "mod = importlib.util.module_from_spec(spec)\n"
                    "spec.loader.exec_module(mod)\n"
                    "with mock.patch.object(os, 'stat', side_effect=OSError(0, 'network name gone', None, 64)):\n"
                    "    assert mod._store_needs_init(r'\\\\wsl.localhost\\NoSuchDistro\\store') is False\n"
                    "    try:\n"
                    "        mod._store_needs_init(r'C:\\LockedLocalStore')\n"
                    "        raise AssertionError('local store OSError must not be swallowed')\n"
                    "    except OSError:\n"
                    "        pass\n"
                    "with mock.patch.object(os, 'stat', side_effect=PermissionError(13, 'ACL denied at root')):\n"
                    "    try:\n"
                    "        mod._store_needs_init(r'\\\\wsl.localhost\\Ubuntu\\home\\u\\store')\n"
                    "        raise AssertionError('WSL root PermissionError must not be swallowed')\n"
                    "    except PermissionError:\n"
                    "        pass\n"
                    "def _reachable_root_denied_messages(p, *a, **kw):\n"
                    "    if str(p).endswith('messages'):\n"
                    "        raise PermissionError(13, 'ACL denied on messages')\n"
                    "    return types.SimpleNamespace(st_mode=stat.S_IFDIR)\n"
                    "with mock.patch.object(os, 'stat', _reachable_root_denied_messages):\n"
                    "    try:\n"
                    "        mod._store_needs_init(r'\\\\wsl.localhost\\Ubuntu\\home\\u\\store')\n"
                    "        raise AssertionError('reachable WSL share PermissionError on messages must not be swallowed')\n"
                    "    except PermissionError:\n"
                    "        pass\n"
                )
                subprocess.run(
                    [self.python_exe, "-B", "-c", local_store_err_code, win_install_py_win],
                    check=True,
                    stdin=subprocess.DEVNULL,
                )

                self.phase_mod._run_win_cli(self.python_exe, install_py_win, ["--remove", "--to", win_to, "--reg-key", reg_key])
        finally:
            self._delete_reg_key(reg_key)
            self._cleanup_win_parent(win_to)

    def test_pending_phases_and_restart_after_closeout_or_install(self) -> None:
        """Process-plan skills check --pending-phases before stopping in Step 1 and Step 2; committed receipts only; failed plan query exits 3."""
        for rel in (
            ".agents/skills/process-plan/SKILL.md",
            ".claude/skills/process-plan/SKILL.md",
            ".codex/skills/process-plan/SKILL.md",
        ):
            txt = (REPO / rel).read_text(encoding="utf-8")
            self.assertIn("--pending-phases", txt, rel)
            # Step 1 must check --pending-phases BEFORE "If no phase has a ready row" so restart never stops early!
            idx_pending = txt.find("--pending-phases")
            idx_no_ready = txt.find("If no phase has a ready row")
            self.assertTrue(0 <= idx_pending < idx_no_ready, f"{rel}: --pending-phases must precede Step 1 halt")
            # Both Step 1 branch (c) and Step 2 must unconditionally synchronize HEAD with origin/main,
            # re-check --pending-phases, and ONLY THEN run --phase <n> (items 39, 40, and 41)!
            step2_idx = txt.find("## 2. After a phase closeout or park")
            self.assertGreater(step2_idx, 0, f"{rel}: missing Step 2 heading")
            step1_txt = txt[:step2_idx]
            step2_txt = txt[step2_idx:]
            c_start = step1_txt.find("(c) otherwise")
            c_end = step1_txt.find("If no phase has a ready row", c_start)
            self.assertTrue(0 <= c_start < c_end, f"{rel}: missing Step 1 branch (c)")
            a_start = step1_txt.find("(a) if")
            b_start = step1_txt.find("(b) if", max(a_start, 0))
            self.assertTrue(0 <= a_start < b_start < c_start, f"{rel}: missing Step 1 branches (a)/(b)/(c)")
            branch_a = step1_txt[a_start:b_start]
            branch_b = step1_txt[b_start:c_start]
            for br_label, br_chunk in (("branch (a)", branch_a), ("branch (b)", branch_b)):
                self.assertIn("git pull --rebase origin main", br_chunk, f"{rel} {br_label}: must pull --rebase before push")
                self.assertIn("verified manifests", br_chunk, f"{rel} {br_label}: must keep manifest-verified SHA")
                self.assertIn("GIT_EDITOR=true git rebase --continue", br_chunk, f"{rel} {br_label}: must continue rebase noninteractively")
                self.assertIn("git push origin main", br_chunk, f"{rel} {br_label}: must push after rebase")
            branch_c = step1_txt[c_start:c_end]
            self.assertNotIn("if `HEAD` has unpushed", branch_c, f"{rel}: branch (c) sync must be unconditional")
            for label, chunk in (("Step 1 branch (c)", branch_c), ("Step 2", step2_txt)):
                i_sync = chunk.find("git pull --rebase origin main && git push origin main")
                i_recheck = chunk.find("re-run `python3 tools/install.py --pending-phases`", max(i_sync, 0))
                i_phase = chunk.find("tools/install.py --phase <n>", max(i_recheck, 0))
                self.assertTrue(
                    0 <= i_sync < i_recheck < i_phase,
                    f"{rel} {label}: must sync -> re-run --pending-phases -> --phase <n> (got {i_sync}, {i_recheck}, {i_phase})",
                )
        prompt_txt = (REPO / "docs/unattended-prompt.md").read_text(encoding="utf-8")
        pa = prompt_txt.find("(a)")
        pb = prompt_txt.find("(b)", max(pa, 0))
        pc = prompt_txt.find("(c)", max(pb, 0))
        self.assertTrue(0 <= pa < pb < pc, "unattended-prompt.md must define branches (a), (b), (c)")
        for br_label, br_chunk in (("unattended (a)", prompt_txt[pa:pb]), ("unattended (b)", prompt_txt[pb:pc])):
            self.assertIn("git pull --rebase origin main", br_chunk, f"{br_label}: must pull --rebase")
            self.assertIn("verified manifests", br_chunk, f"{br_label}: must keep manifest-verified SHA")
            self.assertIn("GIT_EDITOR=true git rebase --continue", br_chunk, f"{br_label}: must use noninteractive rebase --continue (items 8 & 42)")
            self.assertIn("git push origin main", br_chunk, f"{br_label}: must push")
        p_sync = prompt_txt.find("synchronize `HEAD` with `git pull --rebase origin main && git push origin main`", pc)
        p_recheck = prompt_txt.find("re-run `python3 tools/install.py --pending-phases`", max(p_sync, 0))
        p_phase = prompt_txt.find("run `--phase <n>`", max(p_recheck, 0))
        self.assertTrue(0 <= p_sync < p_recheck < p_phase, f"unattended-prompt.md ordering: {p_sync}, {p_recheck}, {p_phase}")

        # Broken/non-repo HELIO_REPO must exit 3 (never silently return [] / exit 0)!
        r_bad = subprocess.run(
            [sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"],
            env={**os.environ, "HELIO_REPO": "/usr"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(r_bad.returncode, 3, r_bad.stdout + r_bad.stderr)

        base_plan = {
            "phases": {"0": "Phase 0"},
            "sections": [{"ref": "D01 T01 §15", "phase": 0, "status": "verified"}],
        }
        reg_key = self._new_reg_key()
        win_to = self._win_temp_dir("Helio Pending")
        try:
            with tempfile.TemporaryDirectory(prefix="helio-phase-pending-") as tmp:
                root = Path(tmp)
                checkout, _, _ = self._make_fixture_workspace(root, plan_json=base_plan, bin_tag=b"PENDING_V1")
                to_dir = root / "wsl_bin"
                store_dir = root / "store"
                wrapper = root / "local" / "helio"
                install_py_win = self.win_mod.wsl_to_windows_path(REPO / "tools" / "install.py")
                win_install_py_win = self.win_mod.wsl_to_windows_path(REPO / "tools" / "win_install.py")
                head_sha = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()

                # Restart BEFORE install: closed phase lacks committed `Installed commit:` -> --pending-phases returns [0] (exit 1)!
                env = {**os.environ, "HELIO_REPO": str(checkout)}
                r1 = subprocess.run([sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"], env=env, capture_output=True, text=True)
                self.assertEqual(r1.returncode, 1, r1.stdout + r1.stderr)
                self.assertEqual(json.loads(r1.stdout), [0])

                # Run installation (now BOTH manifests match HEAD), simulate crash BEFORE committing `Installed commit:`!
                path_with_wrapper = f"{wrapper.parent}:{os.environ.get('PATH', '')}"
                with mock.patch.dict(os.environ, {"PATH": path_with_wrapper}):
                    res = self.phase_mod.run_phase_install(
                        repo=checkout,
                        phase=0,
                        to_dir=to_dir,
                        store_dir=store_dir,
                        wrapper_path=wrapper,
                        win_to_raw=win_to,
                        reg_key=reg_key,
                        sdk_url="unused",
                        install_mod=self.mod,
                    )
                self.assertEqual(res["commit"], head_sha)
                # Uncommitted `Installed commit:` in working tree MUST STILL report [0] (exit 1)!
                run_file = checkout / "docs" / "phase-runs" / "2026-11-24-phase-0.md"
                run_file.write_text(run_file.read_text(encoding="utf-8") + f"\nInstalled commit: {head_sha}\n", encoding="utf-8")
                r2 = subprocess.run([sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"], env=env, capture_output=True, text=True)
                self.assertEqual(r2.returncode, 1, r2.stdout + r2.stderr)
                self.assertEqual(json.loads(r2.stdout), [0])
                # Verify BOTH manifests carry the installed commit!
                self.assertEqual(self.mod.read_manifest(to_dir)["current"]["commit"], head_sha)
                st_win = self.phase_mod._probe_win_state(self.python_exe, win_install_py_win, win_to, reg_key)
                self.assertEqual(st_win["manifest"]["current"]["commit"], head_sha)
                # Commit `Installed commit: <sha>` locally WITHOUT pushing: MUST STILL report [0] (exit 1)!
                subprocess.run(["git", "-C", str(checkout), "add", str(run_file)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "commit", "-m", "record installed commit"], check=True, capture_output=True)
                r_unpushed = subprocess.run([sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"], env=env, capture_output=True, text=True)
                self.assertEqual(r_unpushed.returncode, 1, r_unpushed.stdout + r_unpushed.stderr)
                self.assertEqual(json.loads(r_unpushed.stdout), [0])
                # Push the receipt commit to origin/main (zero reinstallations!) -> --pending-phases now returns [] (exit 0)!
                subprocess.run(["git", "-C", str(checkout), "push", "origin", "main"], check=True, capture_output=True)
                r3 = subprocess.run([sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"], env=env, capture_output=True, text=True)
                self.assertEqual(r3.returncode, 0, r3.stdout + r3.stderr)
                self.assertEqual(json.loads(r3.stdout), [])
                # Appending required post-receipt CI notes (even quoting an earlier deployment's
                # `Installed commit: <sha>` in a CI bullet or blockquote) MUST NOT reopen an installed phase
                # BOTH before pushing (local commit on restart) AND after pushing (item 21)!
                prior_sha = "b" * 40
                run_file.write_text(
                    run_file.read_text(encoding="utf-8")
                    + f"\n- CI: build.yml green after closeout (replaced prior Installed commit: {prior_sha})\n"
                    + f"> Installed commit: {prior_sha}\n",
                    encoding="utf-8",
                )
                subprocess.run(["git", "-C", str(checkout), "add", str(run_file)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "commit", "-m", "append post-receipt CI note"], check=True, capture_output=True)
                r_ci_note_local = subprocess.run([sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"], env=env, capture_output=True, text=True)
                self.assertEqual(r_ci_note_local.returncode, 0, r_ci_note_local.stdout + r_ci_note_local.stderr)
                self.assertEqual(json.loads(r_ci_note_local.stdout), [])
                subprocess.run(["git", "-C", str(checkout), "push", "origin", "main"], check=True, capture_output=True)
                r_ci_note = subprocess.run([sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"], env=env, capture_output=True, text=True)
                self.assertEqual(r_ci_note.returncode, 0, r_ci_note.stdout + r_ci_note.stderr)
                self.assertEqual(json.loads(r_ci_note.stdout), [])
                # An unrelated local commit does NOT reopen an already-published phase!
                (checkout / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(checkout), "add", "unrelated.txt"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "commit", "-m", "unrelated local commit"], check=True, capture_output=True)
                r_unrel = subprocess.run([sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"], env=env, capture_output=True, text=True)
                self.assertEqual(r_unrel.returncode, 0, r_unrel.stdout + r_unrel.stderr)
                self.assertEqual(json.loads(r_unrel.stdout), [])
                # Stale local checkout (HEAD lacks receipt, but origin/main ALREADY has the published receipt) -> [] (exit 0)!
                stale_clone = root / "stale_clone"
                subprocess.run(["git", "clone", str(checkout), str(stale_clone)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(stale_clone), "reset", "--hard", head_sha], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(stale_clone), "remote", "set-url", "origin", str(checkout.parent / "origin.git")], check=True, capture_output=True)
                r_stale = subprocess.run(
                    [sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"],
                    env={**os.environ, "HELIO_REPO": str(stale_clone)},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_stale.returncode, 0, r_stale.stdout + r_stale.stderr)
                self.assertEqual(json.loads(r_stale.stdout), [])
                # Competing local receipt SHA (item 8) AND renewed closeout binding (item 18):
                # 1. Push a renewed closeout `## Closeout run=competing` at `comp_closeout_sha` and genuinely install
                #    `comp_closeout_sha` so BOTH WSL and Windows manifests carry `comp_closeout_sha`.
                # 2. First verify item 18: publishing the PRECEDING closeout's SHA (`head_sha`) under the renewed
                #    closeout MUST keep phase 0 pending ([0], exit 1)!
                # 3. Then have `comp_clone` commit the manifest-verified `comp_closeout_sha`, force a genuine rebase
                #    conflict against the competing remote receipt (`head_sha`), resolve by keeping `comp_closeout_sha`,
                #    push, and assert --pending-phases == [] AND `comp_closeout_sha` on origin/main!
                run_file.write_text(run_file.read_text(encoding="utf-8") + "\n## Closeout run=competing\n\nReady.\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(checkout), "add", str(run_file)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "commit", "-m", "closeout before competing receipts"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "push", "origin", "main"], check=True, capture_output=True)
                comp_closeout_sha = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
                with mock.patch.dict(os.environ, {"PATH": path_with_wrapper}):
                    res_comp = self.phase_mod.run_phase_install(
                        repo=checkout,
                        phase=0,
                        to_dir=to_dir,
                        store_dir=store_dir,
                        wrapper_path=wrapper,
                        win_to_raw=win_to,
                        reg_key=reg_key,
                        sdk_url="unused",
                        install_mod=self.mod,
                    )
                self.assertEqual(res_comp["commit"], comp_closeout_sha)
                self.assertEqual(self.mod.read_manifest(to_dir)["current"]["commit"], comp_closeout_sha)
                st_win2 = self.phase_mod._probe_win_state(self.python_exe, win_install_py_win, win_to, reg_key)
                self.assertEqual(st_win2["manifest"]["current"]["commit"], comp_closeout_sha)
                comp_clone = root / "comp_clone"
                subprocess.run(["git", "clone", str(checkout), str(comp_clone)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(comp_clone), "remote", "set-url", "origin", str(checkout.parent / "origin.git")], check=True, capture_output=True)
                comp_rf = comp_clone / "docs" / "phase-runs" / "2026-11-24-phase-0.md"
                comp_base_text = comp_rf.read_text(encoding="utf-8")
                # Remote advances with a stale/competing receipt carrying the preceding closeout's SHA (`head_sha`) -> item 18 MUST stay pending [0]!
                run_file.write_text(comp_base_text + f"\nInstalled commit: {head_sha}\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(checkout), "add", str(run_file)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "commit", "-m", "stale preceding receipt under renewed closeout"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "push", "origin", "main"], check=True, capture_output=True)
                r_item18 = subprocess.run(
                    [sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_item18.returncode, 1, r_item18.stdout + r_item18.stderr)
                self.assertEqual(json.loads(r_item18.stdout), [0])
                # Local checkout commits the manifest-verified `comp_closeout_sha`
                comp_rf.write_text(comp_base_text + f"\nInstalled commit: {comp_closeout_sha}\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(comp_clone), "config", "user.name", "Test"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(comp_clone), "config", "user.email", "t@example.com"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(comp_clone), "add", str(comp_rf)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(comp_clone), "commit", "-m", "verified local receipt"], check=True, capture_output=True)
                r_comp = subprocess.run(
                    [sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"],
                    env={**os.environ, "HELIO_REPO": str(comp_clone)},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_comp.returncode, 1, r_comp.stdout + r_comp.stderr)
                self.assertEqual(json.loads(r_comp.stdout), [0])
                rb_res = subprocess.run(["git", "-C", str(comp_clone), "pull", "--rebase", "origin", "main"], capture_output=True, text=True)
                self.assertNotEqual(rb_res.returncode, 0, "competing receipt lines must trigger a genuine rebase conflict")
                # Keep the manifest-verified `comp_closeout_sha` and continue rebase!
                comp_rf.write_text(comp_base_text + f"\nInstalled commit: {comp_closeout_sha}\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(comp_clone), "add", str(comp_rf)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(comp_clone), "rebase", "--continue"], env={**os.environ, "GIT_EDITOR": "true"}, check=True, capture_output=True)
                subprocess.run(["git", "-C", str(comp_clone), "push", "origin", "main"], check=True, capture_output=True)
                r_comp_ok = subprocess.run(
                    [sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"],
                    env={**os.environ, "HELIO_REPO": str(comp_clone)},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_comp_ok.returncode, 0, r_comp_ok.stdout + r_comp_ok.stderr)
                self.assertEqual(json.loads(r_comp_ok.stdout), [])
                published_rf = subprocess.check_output(
                    ["git", "-C", str(comp_clone), "show", "origin/main:docs/phase-runs/2026-11-24-phase-0.md"],
                    text=True,
                )
                self.assertIn(f"Installed commit: {comp_closeout_sha}", published_rf.splitlines()[-1])
                # Amended local closeout with the EXACT SAME heading (`## Closeout run=remoteA`) while origin/main
                # has a genuine receipt for `remote_a_sha` -> MUST report [0] (exit 1)!
                # AND a stale checkout strictly behind that newer receipted remote closeout -> MUST report [] (exit 0)!
                subprocess.run(["git", "-C", str(checkout), "pull", "--rebase", "origin", "main"], check=True, capture_output=True)
                div_clone = root / "div_clone"
                subprocess.run(["git", "clone", str(checkout), str(div_clone)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(div_clone), "remote", "set-url", "origin", str(checkout.parent / "origin.git")], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(div_clone), "config", "user.name", "Test"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(div_clone), "config", "user.email", "t@example.com"], check=True, capture_output=True)
                # Remote publishes renewed `## Closeout run=remoteA` at `remote_a_sha`, then commits its receipt `Installed commit: remote_a_sha`!
                run_file.write_text(run_file.read_text(encoding="utf-8") + "\n## Closeout run=remoteA\n\nRemote body.\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(checkout), "add", str(run_file)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "commit", "-m", "remote closeout A"], check=True, capture_output=True)
                remote_a_sha = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
                run_file.write_text(run_file.read_text(encoding="utf-8") + f"\nInstalled commit: {remote_a_sha}\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(checkout), "add", str(run_file)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "commit", "-m", "remote closeout A receipt"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "push", "origin", "main"], check=True, capture_output=True)
                # (1) Stale checkout at `older_co_sha` (strictly behind newer receipted `## Closeout run=remoteA`) -> [] (exit 0)!
                r_behind = subprocess.run(
                    [sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"],
                    env={**os.environ, "HELIO_REPO": str(div_clone)},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_behind.returncode, 0, r_behind.stdout + r_behind.stderr)
                self.assertEqual(json.loads(r_behind.stdout), [])
                # (2) Amended local closeout with the EXACT SAME heading (`## Closeout run=remoteA`) and NO receipt -> [0] (exit 1)!
                div_rf = div_clone / "docs" / "phase-runs" / "2026-11-24-phase-0.md"
                div_rf.write_text(div_rf.read_text(encoding="utf-8") + "\n## Closeout run=remoteA\n\nAmended body.\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(div_clone), "add", str(div_rf)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(div_clone), "commit", "-m", "amended local closeout same heading"], check=True, capture_output=True)
                r_div = subprocess.run(
                    [sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"],
                    env={**os.environ, "HELIO_REPO": str(div_clone)},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_div.returncode, 1, r_div.stdout + r_div.stderr)
                self.assertEqual(json.loads(r_div.stdout), [0])
                # A repeated closeout with the EXACT SAME heading AND identical summary reusing `remote_a_sha` MUST stay pending [0] (item 18)!
                run_file.write_text(
                    run_file.read_text(encoding="utf-8") + f"\n## Closeout run=remoteA\n\nRemote body.\n\nInstalled commit: {remote_a_sha}\n",
                    encoding="utf-8",
                )
                subprocess.run(["git", "-C", str(checkout), "add", str(run_file)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "commit", "-m", "repeated identical closeout reusing earlier sha"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "push", "origin", "main"], check=True, capture_output=True)
                r_repeat_ident = subprocess.run([sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"], env=env, capture_output=True, text=True)
                self.assertEqual(r_repeat_ident.returncode, 1, r_repeat_ident.stdout + r_repeat_ident.stderr)
                self.assertEqual(json.loads(r_repeat_ident.stdout), [0])
                # Locally deleting a newer dated phase-run file while an older closeout file survives MUST keep
                # phase 0 pending ([0], exit 1) (item 38)!
                run_file_2 = checkout / "docs" / "phase-runs" / "2026-11-25-phase-0.md"
                run_file_2.write_text("# Phase 0 run 2\n\n## Closeout run=run2\n\nDone 2.\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(checkout), "add", str(run_file_2)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "commit", "-m", "second dated phase-0 closeout"], check=True, capture_output=True)
                rf2_sha = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
                run_file_2.write_text(run_file_2.read_text(encoding="utf-8") + f"\nInstalled commit: {rf2_sha}\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(checkout), "add", str(run_file_2)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "commit", "-m", "second dated phase-0 receipt"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "push", "origin", "main"], check=True, capture_output=True)
                r_rf2_ok = subprocess.run([sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"], env=env, capture_output=True, text=True)
                self.assertEqual(r_rf2_ok.returncode, 0, r_rf2_ok.stdout + r_rf2_ok.stderr)
                self.assertEqual(json.loads(r_rf2_ok.stdout), [])
                subprocess.run(["git", "-C", str(checkout), "rm", str(run_file_2)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "commit", "-m", "locally delete newer phase-0 run file"], check=True, capture_output=True)
                r_rf2_del = subprocess.run([sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"], env=env, capture_output=True, text=True)
                self.assertEqual(r_rf2_del.returncode, 1, r_rf2_del.stdout + r_rf2_del.stderr)
                self.assertEqual(json.loads(r_rf2_del.stdout), [0])
                # Recover the unpublished deletion commit via branch (c): sync with origin/main (preserving the deletion),
                # re-check `--pending-phases` (still [0]), run `run_phase_install`, record `Installed commit:`, and push -> [] (items 39 & 41)!
                subprocess.run(["git", "-C", str(checkout), "pull", "--rebase", "origin", "main"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "push", "origin", "main"], check=True, capture_output=True)
                r_rf2_recheck = subprocess.run([sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"], env=env, capture_output=True, text=True)
                self.assertEqual(r_rf2_recheck.returncode, 1, r_rf2_recheck.stdout + r_rf2_recheck.stderr)
                self.assertEqual(json.loads(r_rf2_recheck.stdout), [0])
                del_sha = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
                with mock.patch.dict(os.environ, {"PATH": path_with_wrapper}):
                    res_del = self.phase_mod.run_phase_install(
                        repo=checkout,
                        phase=0,
                        to_dir=to_dir,
                        store_dir=store_dir,
                        wrapper_path=wrapper,
                        win_to_raw=win_to,
                        reg_key=reg_key,
                        sdk_url="unused",
                        install_mod=self.mod,
                    )
                self.assertEqual(res_del["commit"], del_sha)
                run_file.write_text(run_file.read_text(encoding="utf-8") + f"\nInstalled commit: {del_sha}\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(checkout), "add", str(run_file)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "commit", "-m", "record installed commit after run-file deletion"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "push", "origin", "main"], check=True, capture_output=True)
                r_rf2_recovered = subprocess.run([sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"], env=env, capture_output=True, text=True)
                self.assertEqual(r_rf2_recovered.returncode, 0, r_rf2_recovered.stdout + r_rf2_recovered.stderr)
                self.assertEqual(json.loads(r_rf2_recovered.stdout), [])
                check_del_preserved = subprocess.run(
                    ["git", "-C", str(checkout), "cat-file", "-e", "origin/main:docs/phase-runs/2026-11-25-phase-0.md"],
                    capture_output=True,
                )
                self.assertNotEqual(check_del_preserved.returncode, 0, "operator deletion of newer run file must remain preserved on origin/main")
                # A newer local `## Closeout` committed WITHOUT pushing MUST STILL report [0] (never inherit older remote receipt)!
                run_file.write_text(run_file.read_text(encoding="utf-8") + "\n## Closeout run=reopened\n\nRe-closed.\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(checkout), "add", str(run_file)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "commit", "-m", "new closeout"], check=True, capture_output=True)
                r4_local = subprocess.run([sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"], env=env, capture_output=True, text=True)
                self.assertEqual(r4_local.returncode, 1, r4_local.stdout + r4_local.stderr)
                self.assertEqual(json.loads(r4_local.stdout), [0])
                subprocess.run(["git", "-C", str(checkout), "push", "origin", "main"], check=True, capture_output=True)
                r4 = subprocess.run([sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"], env=env, capture_output=True, text=True)
                self.assertEqual(r4.returncode, 1, r4.stdout + r4.stderr)
                self.assertEqual(json.loads(r4.stdout), [0])
                # Removing `origin` remote must fail with exit 3 when a closed phase needs publication check!
                subprocess.run(["git", "-C", str(checkout), "remote", "remove", "origin"], check=True, capture_output=True)
                r_norem = subprocess.run([sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"], env=env, capture_output=True, text=True)
                self.assertEqual(r_norem.returncode, 3, r_norem.stdout + r_norem.stderr)
                # Offline/no-origin checkout with NO locally closed verified phase (e.g. PARKED phase) MUST return [] (exit 0)
                # without querying `origin` (item 37)!
                run_file.write_text(run_file.read_text(encoding="utf-8") + "\nPARKED waiting on operator\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(checkout), "add", str(run_file)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "commit", "-m", "park phase 0"], check=True, capture_output=True)
                r_offline_open = subprocess.run(
                    [sys.executable, "-B", str(REPO / "tools" / "install.py"), "--pending-phases"],
                    env={**env, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.allow", "GIT_CONFIG_VALUE_0": "never"},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(r_offline_open.returncode, 0, r_offline_open.stdout + r_offline_open.stderr)
                self.assertEqual(json.loads(r_offline_open.stdout), [])
                self.phase_mod._run_win_cli(self.python_exe, install_py_win, ["--remove", "--to", win_to, "--reg-key", reg_key])
        finally:
            self._delete_reg_key(reg_key)
            self._cleanup_win_parent(win_to)

    def test_phase_real_pinned_sdk_nonrecursive_end_to_end(self) -> None:
        """Exercise one nonrecursive fixture checkout through the REAL pinned ImpossibleSDK app_build.py and prove.py."""
        if os.environ.get("HELIO_IN_SDK_PHASE_TEST") == "1":
            self.skipTest("prevent recursion inside nested SDK phase test")
        real_sdk = REPO.parent / "ImpossibleSDK"
        if not (real_sdk / "tools" / "app_build.py").is_file() or not (real_sdk / "tools" / "prove.py").is_file():
            self.skipTest("ImpossibleSDK checkout beside Heliograph is required")
        reg_key = self._new_reg_key()
        win_to = self._win_temp_dir("Helio Real SDK")
        try:
            with tempfile.TemporaryDirectory(prefix="helio-real-sdk-phase-") as tmp:
                root = Path(tmp)
                os.symlink(real_sdk, root / "ImpossibleSDK")
                checkout = root / "Heliograph"
                checkout.mkdir()
                for name in ("CMakeLists.txt", "app.json", "sdk.json", ".gitignore"):
                    shutil.copy2(REPO / name, checkout / name)
                for folder in ("engine", "src", "brand"):
                    if (REPO / folder).is_dir():
                        shutil.copytree(REPO / folder, checkout / folder)
                # Minimal nonrecursive test suite with the required cross-host race test name so REAL app_build.py + prove.py run!
                (checkout / "tests").mkdir()
                for racer_f in ("racer.c", "racer_linux.c", "racer_windows.c"):
                    shutil.copy2(REPO / "tests" / racer_f, checkout / "tests" / racer_f)
                (checkout / "tests" / "test_smoke.py").write_text(
                    "import os, pathlib, subprocess, tempfile, time, unittest\n"
                    "class Smoke(unittest.TestCase):\n"
                    "    def test_windows_and_linux_take_each_job_once(self):\n"
                    "        b = pathlib.Path(os.environ['IMPOSSIBLE_BUILD'])\n"
                    "        if os.name == 'nt':\n"
                    "            r = subprocess.run([str(b / 'helio.exe'), 'help'], capture_output=True, text=True)\n"
                    "            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)\n"
                    "            return\n"
                    "        linux_h = b / 'linux' / 'helio'\n"
                    "        linux_r = b / 'linux' / 'racer'\n"
                    "        win_r = b / 'racer.exe'\n"
                    "        with tempfile.TemporaryDirectory(prefix='sdk-race-') as td:\n"
                    "            st = pathlib.Path(td) / 'store'\n"
                    "            subprocess.run([str(linux_h), '--store', str(st), 'init'], check=True, capture_output=True)\n"
                    "            ids = []\n"
                    "            for i in range(120):\n"
                    "                out = subprocess.run(\n"
                    "                    [str(linux_h), '--store', str(st), 'ask', 'impossible/all', f'job {i}', '--as', 'impossible/a'],\n"
                    "                    input='body\\n', text=True, capture_output=True, check=True,\n"
                    "                ).stdout.strip().splitlines()[-1]\n"
                    "                ids.append(out)\n"
                    "            win_st = subprocess.check_output(['wslpath', '-w', str(st)], text=True).strip()\n"
                    "            procs = []\n"
                    "            try:\n"
                    "                procs.append(subprocess.Popen([str(linux_r), str(st), 'impossible/lin', *ids], stdin=subprocess.DEVNULL))\n"
                    "                procs.append(subprocess.Popen([str(win_r), win_st, 'impossible/win1', *ids[::-1]], stdin=subprocess.DEVNULL))\n"
                    "                procs.append(subprocess.Popen([str(win_r), win_st, 'impossible/win2', *ids[::-1]], stdin=subprocess.DEVNULL))\n"
                    "                for _ in range(300):\n"
                    "                    if len(list(st.glob('ready.*'))) == len(procs):\n"
                    "                        break\n"
                    "                    time.sleep(0.1)\n"
                    "                self.assertEqual(len(list(st.glob('ready.*'))), len(procs), 'all racers must reach barrier')\n"
                    "                (st / 'go').write_text('x', encoding='utf-8')\n"
                    "                won = [p.wait(60) for p in procs]\n"
                    "            finally:\n"
                    "                for p in procs:\n"
                    "                    if p.poll() is None:\n"
                    "                        p.kill()\n"
                    "            self.assertEqual(sum(won), len(ids), won)\n"
                    "            self.assertGreater(won[0], 0, f'Linux must win jobs: {won}')\n"
                    "            self.assertGreater(won[1] + won[2], 0, f'Windows must win jobs: {won}')\n"
                    "            log_out = subprocess.run([str(linux_h), '--store', str(st), '--as', 'impossible/main', 'log'], capture_output=True, text=True, check=True).stdout\n"
                    "            self.assertEqual(log_out.count('request taken by'), len(ids))\n",
                    encoding="utf-8",
                )
                (checkout / "scripts").mkdir()
                (checkout / "scripts" / "todo-graph.py").write_text(
                    "import json\nprint(json.dumps({'phases': {'0': 'Phase 0'}, 'sections': [{'ref': 'D01 T01 §15', 'phase': 0, 'status': 'verified'}]}))\n",
                    encoding="utf-8",
                )
                (checkout / "docs" / "phase-runs").mkdir(parents=True)
                (checkout / "docs" / "phase-runs" / "2026-11-24-phase-0.md").write_text(
                    "# Phase run: Phase 0\n\n## Closeout run=realsdk\n\nAll verified.\n",
                    encoding="utf-8",
                )
                origin = root / "origin.git"
                subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(origin)], check=True, capture_output=True)
                subprocess.run(["git", "init", "--initial-branch=main", str(checkout)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Test"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "config", "user.email", "t@example.com"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "commit", "-m", "real sdk phase fixture"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", str(origin)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(checkout), "push", "-u", "origin", "main"], check=True, capture_output=True)
                head_sha = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()

                to_dir = root / "wsl_bin"
                store_dir = root / "store"
                wrapper = root / "local" / "helio"
                install_py_win = self.win_mod.wsl_to_windows_path(REPO / "tools" / "install.py")
                win_install_py_win = self.win_mod.wsl_to_windows_path(REPO / "tools" / "win_install.py")
                path_with_wrapper = f"{wrapper.parent}:{os.environ.get('PATH', '')}"
                with mock.patch.dict(os.environ, {"PATH": path_with_wrapper, "HELIO_IN_SDK_PHASE_TEST": "1"}):
                    res = self.phase_mod.run_phase_install(
                        repo=checkout,
                        phase=0,
                        to_dir=to_dir,
                        store_dir=store_dir,
                        wrapper_path=wrapper,
                        win_to_raw=win_to,
                        reg_key=reg_key,
                        sdk_url="unused",
                        install_mod=self.mod,
                    )
                self.assertEqual(res["commit"], head_sha)
                built_wsl_sha = self.mod.sha256_file(checkout / "build" / "linux" / "linux" / "helio")
                built_win_sha = self.mod.sha256_file(checkout / "build" / "linux" / "helio.exe")
                self.assertEqual(self.mod.sha256_file(to_dir / "helio"), built_wsl_sha)
                st_win = self.phase_mod._probe_win_state(self.python_exe, win_install_py_win, win_to, reg_key)
                self.assertEqual(st_win["status_code"], 0, st_win["status_lines"])
                self.assertEqual(st_win["exe_sha"], built_win_sha)
                self.phase_mod._run_win_cli(self.python_exe, install_py_win, ["--remove", "--to", win_to, "--reg-key", reg_key])
        finally:
            self._delete_reg_key(reg_key)
            self._cleanup_win_parent(win_to)


if __name__ == "__main__":
    unittest.main()
