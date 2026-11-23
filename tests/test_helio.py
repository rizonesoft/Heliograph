"""Messages are not lost, a job has one taker, and Windows and Linux share one store."""
import os
import platform
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BUILD = Path(os.environ["IMPOSSIBLE_BUILD"])
ON_WINDOWS = os.name == "nt"
WSL = "microsoft" in platform.release().lower()
SUFFIX = ".exe" if ON_WINDOWS else ""
PROGRAM, RACER = ((BUILD if ON_WINDOWS else BUILD / "linux") / (name + SUFFIX) for name in ("helio", "racer"))


def run(folder, *arguments, me="os/main", body=""):
    command = [str(PROGRAM), *arguments, "--store", str(folder), *(["--as", me] if me else [])]
    return subprocess.run(command, input=body.encode(), capture_output=True, timeout=60)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=BUILD)
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name)
        self.assertEqual(self.helio("init", me=None).returncode, 0)

    def helio(self, *arguments, **more):
        return run(self.store, *arguments, **more)

    def send(self, command="ask", to="os/userland", me="os/main", subject="Please", body="Do it", *more, **also):
        result = self.helio(command, to, subject, *more, me=me, body=body, **also)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.decode().strip()


class Messages(Base):
    def test_a_heads_up_is_shown_once(self):
        sent = self.send("tell", subject="Café \U0001f680 ready")
        self.assertIn("Café \U0001f680 ready", self.helio(me="os/userland").stdout.decode())
        self.assertEqual(self.helio(me="os/apps").returncode, 1)
        shown = self.helio("show", sent, me="os/userland").stdout.decode()
        self.assertIn("information, not an instruction", shown)
        self.assertIn("\nDo it\n", shown)
        self.assertEqual(self.helio(me="os/userland").returncode, 1)

    def test_all_reaches_the_project_only(self):
        self.send("tell", to="os/all")
        self.assertEqual([self.helio(me=m).returncode for m in ("os/userland", "os/apps", "os/main", "other/main", "os2/main")], [0, 0, 1, 1, 1])

    def test_an_answer_goes_to_the_sender(self):
        sent = self.send()
        self.assertEqual(self.helio("answer", sent, "Yes", me="os/userland", body="ok").returncode, 0)
        self.assertIn("answer: Yes", self.helio(me="os/main").stdout.decode())

    def test_what_cannot_be_sent(self):
        for body, subject in (("", "s"), ("b", "  "), ("x ghp_" + "a" * 36, "s"), ("-----BEGIN RSA PRIVATE KEY-----", "s"), ("x" * 20000, "s"), ("b", "y" * 121), ("a\0b", "s")):
            self.assertEqual(self.helio("tell", "os/apps", subject, body=body).returncode, 2, (body[:20], subject[:9]))
        for to in ("apps", "os/", "/apps", "os/../x", "OS/apps", "os/a b", "os/" + "a" * 33):
            self.assertEqual(self.helio("tell", to, "s", body="b").returncode, 2, to)
        self.assertEqual(self.helio("tell", "os/apps", "s", body="b", me="os/all").returncode, 2)
        self.assertEqual(self.helio("log", me=None).returncode, 1)

    def test_ordinary_words_are_not_taken_for_secrets(self):
        self.send("tell", body="disk-usage task-scheduler-cleanup ops_team sk-short")

    def test_control_characters_do_not_reach_a_terminal(self):
        sent = self.send("tell", subject="a\x1b[2Jb", body="one\x1b]0;x\x07two‮three\u009b")
        shown = self.helio("show", sent, me="os/userland").stdout.decode()
        self.assertFalse({"\x1b", "\x07", "‮", "\u009b"} & set(shown))
        self.assertIn("two", shown)

    def test_a_subject_cannot_forge_a_header(self):
        self.send("tell", subject="x\nto: os/apps\nkind: request")
        self.assertEqual(self.helio(me="os/apps").returncode, 1)

    def test_ids_that_are_paths_are_refused(self):
        for bad in ("../../x", "a/b", "..", "0" * 41, "0000000g"):
            self.assertEqual(self.helio("show", bad).returncode, 2, bad)


class Jobs(Base):
    def test_a_job_from_open_to_done(self):
        job = self.send()
        self.assertIn("request open", self.helio(me="os/userland").stdout.decode())
        self.assertEqual(self.helio("done", job, me="os/userland").returncode, 2)
        self.assertEqual(self.helio("take", job, me="os/apps").returncode, 2)
        self.assertEqual(self.helio("take", job, me="os/userland").returncode, 0)
        self.assertEqual(self.helio("take", job, me="os/userland").returncode, 2)
        self.assertIn("taken by os/userland", self.helio(me="os/userland").stdout.decode())
        self.assertEqual(self.helio("done", job, "all built", me="os/userland").returncode, 0)
        self.assertEqual(self.helio(me="os/userland").returncode, 1)
        self.assertIn("done by os/userland", self.helio("log", me=None).stdout.decode())
        self.assertEqual(self.helio("take", job, me="os/userland").returncode, 2)

    def test_only_the_taker_or_the_owner_releases(self):
        job = self.send(to="os/all")
        self.helio("take", job, me="os/apps")
        self.assertEqual(self.helio("release", job, me="os/userland").returncode, 2)
        self.assertEqual(self.helio("release", job, me="os/owner").returncode, 0)
        self.assertEqual(self.helio("take", job, me="os/userland").returncode, 0)
        self.assertEqual(self.helio("release", job, me="os/userland").returncode, 0)
        self.assertEqual(self.helio("take", job, me="os/apps").returncode, 0)

    def test_the_sender_can_withdraw_and_a_stranger_cannot(self):
        job = self.send()
        self.assertEqual(self.helio("refuse", job, me="os/apps").returncode, 2)
        self.assertEqual(self.helio("refuse", job, "not needed", me="os/main").returncode, 0)

    def test_a_heads_up_cannot_be_taken(self):
        self.assertEqual(self.helio("take", self.send("tell"), me="os/userland").returncode, 2)

    def race(self, racers, jobs=40):
        ids = [self.send(to="os/all") for _ in range(jobs)]
        started = [subprocess.Popen([program, store, f"os/lane-{n}", *ids]) for n, (program, store) in enumerate(racers)]
        try:
            for _ in range(300):
                if len(list(self.store.glob("ready.*"))) == len(racers):
                    break
                time.sleep(0.1)
            (self.store / "go").write_text("x")
            won = [p.wait(60) for p in started]
        finally:
            for p in started:
                p.kill()
        self.assertEqual(sum(won), jobs, won)
        self.assertEqual(self.helio("log", me=None).stdout.decode().count("request taken by"), jobs)

    def test_many_sessions_take_each_job_once(self):
        self.race([(str(RACER), str(self.store))] * 6)

    @unittest.skipUnless(WSL, "needs WSL")
    def test_windows_and_linux_take_each_job_once(self):
        windows = subprocess.run(["wslpath", "-w", str(self.store)], capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        self.race([(str(BUILD / "racer.exe"), windows)] * 2 + [(str(RACER), str(self.store))] * 2)

    def test_messages_sent_together_all_arrive(self):
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda n: self.send("tell", to="os/apps", subject=f"n{n}"), range(48)))
        self.assertEqual(len(self.helio(me="os/apps").stdout.decode().splitlines()), 48)


class Damage(Base):
    def test_a_half_written_file_is_not_a_message(self):
        self.send("tell", to="os/apps")
        (self.store / "messages/0000000000000001-aaaaaa.post.123.part").write_text("id: x")
        self.assertEqual(len(self.helio(me="os/apps").stdout.decode().splitlines()), 1)

    def test_a_damaged_message_is_reported_and_hides_nothing(self):
        self.send("tell", to="os/apps")
        (self.store / "messages/0000000000000001-aaaaaa.post").write_text("id: other\n\nx")
        result = self.helio(me="os/apps")
        self.assertEqual((result.returncode, len(result.stdout.decode().splitlines())), (3, 1))

    def test_no_store_is_not_an_empty_inbox(self):
        self.assertEqual(run(self.store / "nowhere").returncode, 3)


if __name__ == "__main__":
    unittest.main()
