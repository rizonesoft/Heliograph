import concurrent.futures
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

            # If another destination took wrap_a, --back refuses with exit 2 without clobbering it.
            other_dir = root / "other_bin"
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
                    "schema_version": 1,
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
            self.assertEqual(unrelated_helio.read_text(encoding="utf-8"), "unrelated user script\n")

            # Foreign install journal trying to unlink unrelated_helio when saved_files has existed=False.
            (frem_dir / self.mod.JOURNAL_NAME).write_text(
                json.dumps({
                    "schema_version": 1,
                    "op": "install",
                    "phase": "publishing",
                    "tx_id": "abcdef012345",
                    "to_dir": str(frem_dir.resolve()),
                    "wrapper_path": str((root / "frw" / "helio").resolve()),
                    "had_manifest": False,
                    "previous_manifest": None,
                    "previous_wrapper_existed": False,
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
            fman_dir = root / "foreign_manifest_pub"
            fman_dir.mkdir()
            make_distinct_binary(self.helio_bin, fman_dir / "helio", b"gen_legacy")
            make_distinct_binary(self.helio_exe, fman_dir / "helio.exe", b"gen_legacy")
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
                            "helio_sha256": self.mod.sha256_file(fman_dir / "helio"),
                            "helio_exe_sha256": self.mod.sha256_file(fman_dir / "helio.exe"),
                            "store": str(store),
                            "wrapper": str(existing_ext_dir / "helio"),
                        },
                        "previous": None,
                    },
                    "pre_wrappers": {str(existing_ext_dir / "helio"): str(store)},
                    "saved_files": {},
                    "temp_files": [],
                    "desired_manifest": None,
                }),
                encoding="utf-8",
            )
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=repo, to_dir=fman_dir, store_dir=store, wrapper_path=root / "fmw" / "helio", prebuilt=(a_h, a_e))
            self.assertEqual(ctx.exception.code, 2)
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

            # 7d. Interrupted --back with a pre-existing third wrapper C (and per-unlink boundary after_unlink_old_wrapper_0).
            w3_dir = root / "back_preexist_c"
            w3a, w3b, w3c = root / "w3a2" / "helio", root / "w3b2" / "helio", root / "w3c2" / "helio"
            s3a, s3b, s3c = root / "s3a2", root / "s3b2", root / "s3c2"
            self.mod.install(repo=repo, to_dir=w3_dir, store_dir=s3c, wrapper_path=w3c, prebuilt=(a_h, a_e))
            self.mod.install(repo=repo, to_dir=w3_dir, store_dir=s3a, wrapper_path=w3a, prebuilt=(a_h, a_e))
            self.mod.install(repo=repo, to_dir=w3_dir, store_dir=s3b, wrapper_path=w3b, prebuilt=(b_h, b_e))

            def crash_at_unlink_0(stage: str) -> None:
                if stage == "after_unlink_old_wrapper_0":
                    raise SimulatedCrash(stage)

            with self.assertRaises(SimulatedCrash):
                self.mod.go_back(to_dir=w3_dir, wrapper_path=w3c, on_boundary=crash_at_unlink_0)
            self.assertTrue(self.mod.recover_if_needed(w3_dir, w3c))
            self.assertEqual(w3a.read_text(encoding="utf-8"), self.mod.render_wrapper(w3_dir, s3a))
            self.assertEqual(w3b.read_text(encoding="utf-8"), self.mod.render_wrapper(w3_dir, s3b))
            self.assertEqual(w3c.read_text(encoding="utf-8"), self.mod.render_wrapper(w3_dir, s3c))

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
                    "schema_version": 1,
                    "op": "install",
                    "phase": "publishing",
                    "tx_id": "abcdef012345",
                    "to_dir": str(frec_dir.resolve()),
                    "wrapper_path": str((root / "frec_w" / "helio").resolve()),
                    "alt_wrapper_path": str(fake_ext_dir / "helio"),
                    "had_manifest": False,
                    "previous_manifest": None,
                    "previous_wrapper_existed": False,
                    "saved_files": {"helio": {"existed": True, "rec_name": fake_rec.name, "sha256": self.mod.sha256_file(fake_rec)}},
                    "temp_files": [fake_rec.name],
                    "desired_manifest": None,
                }),
                encoding="utf-8",
            )
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.install(repo=repo, to_dir=frec_dir, store_dir=store, wrapper_path=root / "frec_w" / "helio", prebuilt=(a_h, a_e))
            self.assertEqual(ctx.exception.code, 2)
            self.assertFalse((frec_dir / "helio").exists())
            self.assertFalse(fake_ext_dir.exists())

            # Remove alt_wrapper_path so journal passes validation and hits the independent .rec identity check.
            j_only_rec = json.loads((frec_dir / self.mod.JOURNAL_NAME).read_text(encoding="utf-8"))
            j_only_rec.pop("alt_wrapper_path")
            (frec_dir / self.mod.JOURNAL_NAME).write_text(json.dumps(j_only_rec), encoding="utf-8")
            with self.assertRaises(self.mod.InstallError) as ctx:
                self.mod.recover_if_needed(frec_dir, root / "frec_w" / "helio")
            self.assertEqual(ctx.exception.code, 2)
            self.assertFalse((frec_dir / "helio").exists())

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
            self.mod.install(repo=repo, to_dir=dir_b, store_dir=store_dir, wrapper_path=wrapper, prebuilt=(b_h, b_e))
            self.mod.remove(to_dir=dir_a, wrapper_path=wrapper)
            self.assertTrue(wrapper.is_file())
            self.assertTrue(self.mod.wrapper_belongs_to(wrapper, dir_b))

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(self.mod.install, repo=repo, to_dir=dir_a, store_dir=store_dir, wrapper_path=wrapper, prebuilt=(a_h, a_e))
                f2 = pool.submit(self.mod.install, repo=repo, to_dir=dir_b, store_dir=store_dir, wrapper_path=wrapper, prebuilt=(b_h, b_e))
                f1.result()
                f2.result()
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
        if not (shutil.which("python.exe") and shutil.which("wslpath")):
            self.skipTest("WSL Windows interop (python.exe/wslpath) not available")
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
            in_pub_window = threading.Event()
            overlap_proved = threading.Event()
            win_failures: list[str] = []
            overlap_calls = 0

            def win_caller():
                nonlocal overlap_calls
                while not win_stop.is_set():
                    during = in_pub_window.is_set()
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
                        elif during:
                            overlap_calls += 1
                            overlap_proved.set()
                    except (OSError, subprocess.SubprocessError) as exc:
                        win_failures.append(f"exc={exc}")

            def wait_for_overlap(stage: str) -> None:
                if stage == "after_helio":
                    in_pub_window.set()
                    self.assertTrue(overlap_proved.wait(timeout=15), "no Windows caller completed during publication window")
                elif stage == "after_helio_exe":
                    in_pub_window.clear()

            wt = threading.Thread(target=win_caller)
            try:
                assert holder.stdout is not None
                self.assertEqual(holder.stdout.readline().strip(), "HELD")
                wt.start()
                try:
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
            finally:
                in_pub_window.clear()
                win_stop.set()
                wt.join(timeout=35)
                holder.terminate()
                holder.wait(timeout=10)
                if holder.stdout:
                    holder.stdout.close()
                if holder.stderr:
                    holder.stderr.close()
            self.assertEqual(win_failures, [])
            self.assertGreater(overlap_calls, 0)
            code, _ = self.mod.status(to_dir=to_dir, wrapper_path=wrapper)
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
