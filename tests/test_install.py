import concurrent.futures
import contextlib
import errno
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
                    self.assertEqual(os.path.normcase(m_sh_shadow["shadowed_by"]), os.path.normcase(os.path.abspath(str(sh_shadow_file))))
                    self.assertEqual(os.path.normcase(m_sh_shadow["shadowed_target"]), os.path.normcase(os.path.abspath(str(win_to / "helio"))))
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
                    self.assertEqual(os.path.normcase(str(w_link)), os.path.normcase(os.path.abspath(str(sh_shadow_file))))
                    code_lnk, _ = self.win_mod.win_status(win_to=win_to, reg_key=reg_key)
                    self.assertEqual(code_lnk, 3)
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


if __name__ == "__main__":
    unittest.main()
