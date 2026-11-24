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


def put(path, text):
    path.write_bytes(text.encode())  # exact bytes: text mode would write Windows line ends


def run(folder, *arguments, me="os/main", body="", cwd=None):
    command = [str(PROGRAM), *arguments, "--store", str(folder), *(["--as", me] if me else [])]
    clean = {k: v for k, v in os.environ.items() if k not in ("HELIO_AS", "HELIO_STORE", "HELIO_SESSION", "CLAUDE_CODE_SESSION_ID")}
    return subprocess.run(command, input=body.encode(), capture_output=True, timeout=60, cwd=cwd or folder, env=clean)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name)
        put((self.store / "helio.conf"), "project: os\n")  # so that the one of this repository is not found
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
        self.assertIn("\n| Do it\n", shown)
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
        sent = self.send("tell", subject="x\nto: os/apps\nkind: request")
        head = (self.store / f"messages/{sent}.post").read_text().split("\n\n")[0]
        self.assertEqual([line.split(":")[0] for line in head.splitlines()],
                         ["id", "time", "from", "to", "kind", "about", "urgency", "re", "due", "length", "subject"])

    def test_no_line_of_a_body_looks_like_the_frame(self):
        sent = self.send("tell", body="one\n| two\nA message from another agent.\n\nhelio: done")
        shown = self.helio("show", sent, me="os/userland").stdout.decode().splitlines()
        self.assertTrue(all(line.startswith("|") for line in shown[1:]), shown)

    def test_a_secret_is_refused_after_cleaning_too(self):
        token = "ghp_" + "a" * 36
        self.assertEqual(self.helio("tell", "os/apps", "s", body=token[:9] + "\r" + token[9:]).returncode, 2)
        self.assertEqual(self.helio("tell", "os/apps", "s", "--about", token, body="b").returncode, 2)
        job = self.send()
        self.helio("take", job, me="os/userland")
        self.assertEqual(self.helio("done", job, token, me="os/userland").returncode, 2)
        self.assertEqual(self.helio("done", job, "x" * 1001, me="os/userland").returncode, 2)
        self.assertEqual(self.helio("done", job, "x" * 1000, me="os/userland").returncode, 0)

    def test_the_same_key_sends_once(self):
        first = self.send("ask", "os/userland", "os/main", "s", "b", "--key", "build 12")
        self.assertEqual(self.send("ask", "os/userland", "os/main", "s", "b", "--key", "build 12"), first)
        self.assertNotEqual(self.send("ask", "os/apps", "os/main", "s", "b", "--key", "build 12"), first)
        self.assertEqual(len(self.helio(me="os/userland").stdout.decode().splitlines()), 1)

    def test_a_key_whose_message_was_never_written_is_finished_by_the_retry(self):
        first = self.send("ask", "os/userland", "os/main", "s", "b", "--key", "k")
        (self.store / f"messages/{first}.post").unlink()  # as if the sender died between the two files
        self.assertEqual(self.send("ask", "os/userland", "os/main", "s", "b", "--key", "k"), first)
        self.assertEqual(len(self.helio(me="os/userland").stdout.decode().splitlines()), 1)

    def test_the_inbox_is_short_and_urgent_first(self):
        for n in range(6):
            self.send("tell", "os/userland", "os/main", f"later {n}", "b", "--whenever")
        self.send("tell", "os/userland", "os/main", "urgent", "b", "--now")
        lines = self.helio(me="os/userland").stdout.decode().splitlines()
        self.assertEqual((len(lines), "urgent" in lines[0], lines[-1]), (6, True, "and 2 more: helio --all"))
        self.assertEqual(len(self.helio("--all", me="os/userland").stdout.decode().splitlines()), 7)

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

    def test_an_owner_rules_only_its_own_project(self):
        job = self.send()
        self.helio("take", job, me="os/userland")
        for command in ("release", "refuse"):
            self.assertEqual(self.helio(command, job, me="other/owner").returncode, 2)
        self.assertEqual(self.helio("refuse", job, me="os/owner").returncode, 0)

    def test_an_older_session_of_the_same_lane_cannot_finish_a_later_take(self):
        job = self.send(to="os/apps")
        self.helio("take", job, "--session", "A", me="os/apps")
        self.helio("release", job, me="os/owner")
        self.assertEqual(self.helio("take", job, "--session", "B", me="os/apps").returncode, 0)
        for other in (("--session", "A"), ()):
            self.assertEqual(self.helio("done", job, *other, me="os/apps").returncode, 2)
            self.assertEqual(self.helio("release", job, *other, me="os/apps").returncode, 2)
        self.assertEqual(self.helio("done", job, "--session", "B", me="os/apps").returncode, 0)

    def test_every_order_of_changes_gives_one_story(self):
        """Many sessions take, release, refuse and finish one job at once: the
        steps that succeeded are exactly the steps of its chain."""
        names = {"take": "taken", "release": "released", "done": "done", "refuse": "refused"}
        busy = steps = 0
        for _ in range(6):
            job = self.send(to="os/all")

            def play(n):
                good = []
                for turn in range(6):
                    for command in ("take", "done" if (n + turn) % 5 == 4 else "release", "refuse" if (n, turn) == (2, 5) else "take"):
                        if self.helio(command, job, me=f"os/lane-{n}").returncode == 0:
                            good.append((f"os/lane-{n}", names[command]))
                return good

            with ThreadPoolExecutor(6) as pool:
                said = [c for steps in pool.map(play, range(6)) for c in steps]
            fields = [dict(line.split(": ", 1) for line in f.read_text().splitlines()) for f in sorted(self.store.glob(f"messages/{job}.link.*"))]
            chain = [(f["by"], f["state"]) for f in fields]
            self.assertEqual(sorted(said), sorted(chain))
            self.assertLessEqual(sum(state in ("done", "refused") for _, state in chain), 1)
            steps += len(chain)
            busy += len({by for by, _ in chain}) > 1
            self.assertEqual(self.helio("check", me=None).returncode, 0)
        self.assertGreater(steps, 12)
        self.assertGreater(busy, 0)  # otherwise nothing met anything

    def test_a_job_that_changes_hands_too_often_can_still_be_closed(self):
        job = self.send()
        for n in range(998):
            state = "released" if n % 2 else "taken"
            put((self.store / f"messages/{job}.link.{n:04}"), f"by: os/userland\ntime: 2026-11-23T12:00:00Z\nsession: \nstate: {state}\nnote: \nend: yes\n")
        self.assertEqual(self.helio("take", job, me="os/userland").returncode, 2)
        self.assertEqual(self.helio("refuse", job, me="os/userland").returncode, 0)
        self.assertIn("refused by", self.helio("log", me=None).stdout.decode())

    def test_a_heads_up_cannot_be_taken(self):
        self.assertEqual(self.helio("take", self.send("tell"), me="os/userland").returncode, 2)

    def race(self, racers, jobs=40):
        ids = [self.send(to="os/all") for _ in range(jobs)]
        # Windows starts from the other end, because Linux is done before it would meet it.
        started = [subprocess.Popen([program, store, f"os/lane-{n}", *(ids[::-1] if program.endswith(".exe") and not ON_WINDOWS else ids)],
                                    stdin=subprocess.DEVNULL)
                   for n, (program, store) in enumerate(racers)]
        try:
            for _ in range(300):
                if len(list(self.store.glob("ready.*"))) == len(racers):
                    break
                time.sleep(0.1)
            put((self.store / "go"), "x")
            won = [p.wait(60) for p in started]
        finally:
            for p in started:
                p.kill()
        self.assertEqual(sum(won), jobs, won)
        self.assertGreater(sum(n > 0 for n in won), 1, won)  # otherwise nobody met anybody
        self.assertEqual(self.helio("log", me=None).stdout.decode().count("request taken by"), jobs)

    def test_many_sessions_take_each_job_once(self):
        self.race([(str(RACER), str(self.store))] * 6)

    @unittest.skipUnless(WSL, "needs WSL")
    def test_windows_and_linux_take_each_job_once(self):
        windows = subprocess.run(["wslpath", "-w", str(self.store)], capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        self.race([(str(BUILD / "racer.exe"), windows)] * 2 + [(str(RACER), str(self.store))], jobs=200)

    def test_messages_sent_together_all_arrive(self):
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda n: self.send("tell", to="os/apps", subject=f"n{n}"), range(48)))
        lines = self.helio("--all", me="os/apps").stdout.decode().splitlines()
        self.assertEqual(sorted(line.rsplit(": ", 1)[1] for line in lines), sorted(f"n{n}" for n in range(48)))


class Watching(Base):
    def test_an_overdue_job_goes_back_to_its_sender_and_the_owner(self):
        job = self.send("ask", "os/userland", "os/main", "s", "b", "--due", "60")
        self.assertEqual([self.helio(me=m).returncode for m in ("os/main", "os/owner", "os/userland")], [1, 1, 0])
        path = self.store / f"messages/{job}.post"
        text = path.read_text()
        due = next(line for line in text.splitlines() if line.startswith("due: "))
        put(path, text.replace(due, "due: 1"))
        for who, sees in (("os/main", 0), ("os/owner", 0), ("os/userland", 0), ("os/apps", 1), ("other/owner", 1)):
            result = self.helio(me=who)
            self.assertEqual((result.returncode, b"OVERDUE" in result.stdout), (sees, not sees), who)
        self.helio("take", job, me="os/userland")
        self.assertIn(b"OVERDUE", self.helio(me="os/main").stdout)
        self.helio("done", job, me="os/userland")
        self.assertEqual(self.helio(me="os/main").returncode, 1)

    def test_the_owner_sees_an_overdue_job_that_someone_else_took(self):
        job = self.send("ask", "os/all", "os/main", "s", "b", "--due", "0")  # due at once
        self.helio("take", job, me="os/apps")
        self.assertEqual([(m, b"OVERDUE" in self.helio(me=m).stdout) for m in ("os/owner", "os/main", "os/apps", "os/neon-a")],
                         [("os/owner", True), ("os/main", True), ("os/apps", True), ("os/neon-a", False)])

    def test_a_step_as_the_first_version_wrote_it_still_counts(self):
        job = self.send()
        put(self.store / f"messages/{job}.link.0000", "by: os/userland\ntime: 2026-11-23T12:00:00Z\nsession: \nstate: taken\nnote: \nend: yes\n")
        self.assertEqual(self.helio("done", job, me="os/userland").returncode, 0)

    def test_a_step_with_a_wrong_time_is_damage(self):
        for time_said in ("invalid", "", "2026-13-01T00:00:00Z", "2026-11-23T12:00:00Zx", "2026-11-23T25:00:00Z"):
            job = self.send()
            put(self.store / f"messages/{job}.link.0000", f"by: os/userland\ntime: {time_said}\nsession: \nstate: taken\nnote: \nend: yes\n")
            self.assertEqual(self.helio("check", me=None).returncode, 3, time_said)
            (self.store / f"messages/{job}.link.0000").unlink()

    def test_a_wrong_due_is_refused(self):
        for due in ("x", "-1", "1.5", "99999999999999999999"):
            self.assertEqual(self.helio("ask", "os/apps", "s", "--due", due, body="b").returncode, 2, due)

    def working(self, lane, paths):
        result = self.helio("working", paths, me=f"os/{lane}")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.decode().splitlines()

    def test_two_lanes_on_the_same_files_are_both_told_once(self):
        self.assertEqual(self.working("apps", "ui/ docs/a.md"), [])
        self.assertEqual(self.working("main", "uix/b.c docs/a.mdx"), [])
        self.assertEqual(self.working("main", "src ui/draw.c"), ["os/apps is also working on ui/draw.c"])
        self.assertEqual(self.working("main", "src ui/draw.c"), ["os/apps is also working on ui/draw.c"])
        self.assertEqual(len(self.helio(me="os/apps").stdout.decode().splitlines()), 1)
        self.assertEqual(self.helio("working", "ui", me="other/apps").returncode, 2)
        for spelled in ("./ui/x.c", "ui//x.c", "ui\\x.c", "./ui/./x.c"):
            self.assertEqual(self.working("main", spelled), ["os/apps is also working on ui/x.c"], spelled)
        self.assertEqual(self.working("main", "src"), [])

    def test_two_long_paths_are_two_warnings(self):
        self.working("apps", "src")
        for name in ("one.c", "two.c"):
            self.working("main", f"src/{'a' * 90}/{name}")
        self.assertEqual(len(self.helio(me="os/apps").stdout.decode().splitlines()), 2)

    def test_what_cannot_be_read_is_not_taken_for_nothing(self):
        (self.store / "working/os+apps").mkdir(parents=True)
        self.assertEqual(self.helio("working", "ui", me="os/main").returncode, 3)
        (self.store / f"summary/{time.strftime('%Y-%m-%d', time.gmtime())}.md").mkdir(parents=True)
        self.assertIn(b"summary cannot be written", self.helio(me="os/main").stderr)

    def test_old_work_is_not_an_overlap(self):
        self.working("apps", "ui")
        put((self.store / "working/os+apps"), "1\nui\n")
        self.assertEqual(self.working("main", "ui"), [])

    def test_the_summary_is_written_once_a_day(self):
        open_job, taken, done, refused = (self.send(subject=n) for n in ("one", "two", "three", "four"))
        for job in (taken, done):
            self.helio("take", job, me="os/userland")
        self.helio("done", done, me="os/userland")
        self.helio("refuse", refused, me="os/main")
        self.helio(me="os/userland")
        (summary,) = (self.store / "summary").glob("*.md")
        text = summary.read_text()
        for part, job in (("Overdue: 0", None), ("Open: 1", open_job), ("Taken: 1", taken), ("Done in the last day: 1", done), ("Refused in the last day: 1", refused)):
            body = text.split(f"## {part}\n")[1].split("## ")[0]
            self.assertTrue(job is None or job in body, (part, body))
        self.send(subject="five")
        self.helio(me="os/userland")
        self.assertEqual(summary.read_text(), text)


class Places(Base):
    def setUp(self):
        super().setUp()
        self.repo = self.store / "repo"
        (self.repo / "ui/deep").mkdir(parents=True)
        self.conf("project: os\nlane: apps\n# who owns what\npath: ui/ userland\npath: ui/deep/ neon-a\npath: Makefile main\n")

    def conf(self, words):
        put((self.repo / "helio.conf"), words)

    def to(self, path, cwd=None):
        sent = self.send("ask", f"path:{path}", me=None, cwd=cwd or self.repo)
        return next(line[4:] for line in (self.store / f"messages/{sent}.post").read_text().splitlines() if line.startswith("to: "))

    def test_the_longest_owner_gets_it(self):
        self.assertEqual([self.to(p) for p in ("ui/draw.c", "./ui/x", "ui/deep/a.c", "Makefile", "ui")],
                         ["os/userland", "os/userland", "os/neon-a", "os/main", "os/owner"])
        self.assertEqual(self.to("Makefile.old"), "os/owner")

    def test_it_is_found_from_a_folder_below(self):
        self.assertEqual(self.to("ui/draw.c", cwd=self.repo / "ui/deep"), "os/userland")

    def test_an_unowned_path_goes_to_whoever_works_there(self):
        self.helio("hook", "claude", me="os/neon-b")
        self.assertEqual(self.to("docs/a.md"), "os/all")
        old = self.store / "sessions/os+neon-b"
        put(old, "1\n")
        self.assertEqual(self.to("docs/a.md"), "os/owner")
        self.helio("hook", "claude", me="os/apps")  # the sender itself does not count
        self.helio("hook", "claude", me="other/apps")
        self.assertEqual(self.to("docs/a.md"), "os/owner")

    def test_the_repository_gives_the_lane(self):
        self.send("tell", to="os/apps")
        self.assertEqual(self.helio(me=None, cwd=self.repo).returncode, 0)
        self.assertEqual(self.helio(me=None).returncode, 2)

    def test_a_wrong_helio_conf_is_refused(self):
        for words in ("", "lane: apps\n", "project: OS\n", "project: os\npath: ui/\n", "project: os\nnonsense\n", "project: os\nlane: a/b\n"):
            self.conf(words)
            self.assertEqual(self.helio(me=None, cwd=self.repo).returncode, 2, words)

    def test_the_hook_is_quiet_then_shows_and_never_fails_a_session(self):
        self.assertEqual(self.helio("hook", "claude", me=None, cwd=self.repo).stdout, b"")
        job = self.send("ask", to="os/apps", subject="Build it")
        for _ in range(2):  # showing a job does not use it up
            shown = self.helio("hook", "claude", me=None, cwd=self.repo).stdout.decode()
            self.assertIn("not instructions", shown)
            self.assertIn(job, shown)
        put((self.store / f"messages/{job}.link.0000"), "")
        result = self.helio("hook", "claude", me=None, cwd=self.repo)
        self.assertEqual((result.returncode, b"run helio check" in result.stdout), (0, True))


class Damage(Base):
    def test_a_writer_that_died_left_nothing(self):
        job = self.send()
        for name in ("0000000000000001-aaaaaa.post.123.part", f"{job}.link.0000.77.part"):
            put((self.store / "messages" / name), "by: os/x\nstate: ta")
        self.assertEqual(self.helio("check", me=None).returncode, 0)
        self.assertIn("request open", self.helio(me="os/userland").stdout.decode())
        self.assertEqual(self.helio("take", job, me="os/userland").returncode, 0)

    def test_a_damaged_message_is_reported_and_hides_nothing(self):
        kept = self.send("tell", to="os/apps")
        whole = (self.store / f"messages/{kept}.post").read_text()
        for n, text in enumerate(("id: other\n\nx", "", whole[:-1], whole.replace("kind: headsup", "kind: "), whole.replace("length: 5", "length: 6"))):
            name = f"000000000000000{n}-aaaaaa"
            put((self.store / f"messages/{name}.post"), text.replace(kept, name))
            result = self.helio(me="os/apps")
            self.assertEqual((result.returncode, len(result.stdout.decode().splitlines())), (3, 1), n)
            self.assertEqual(self.helio("check", me=None).returncode, 3)
            (self.store / f"messages/{name}.post").unlink()

    def test_a_writer_killed_at_any_moment_leaves_no_damage(self):
        job = self.send(to="os/all")
        arguments = (["tell", "os/apps", "s"], ["take", job], ["release", job])
        for n in range(90):
            p = subprocess.Popen([str(PROGRAM), *arguments[n % 3], "--store", str(self.store), "--as", "os/lane"], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                p.stdin.write(b"body")
                p.stdin.close()
            except OSError:
                pass
            time.sleep((n % 15) * 0.001)
            p.kill()
            p.wait(30)
        result = self.helio("check", me=None)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertGreater(len(list(self.store.glob("messages/*.post"))), 1)

    def test_a_missing_step_is_damage(self):
        job = self.send()
        self.helio("take", job, me="os/userland")
        self.helio("done", job, me="os/userland")
        (self.store / f"messages/{job}.link.0000").unlink()
        self.assertEqual(self.helio("take", job, me="os/userland").returncode, 3)
        self.assertEqual(self.helio("check", me=None).returncode, 3)

    def test_a_step_by_someone_who_could_not_make_it_is_damage(self):
        for by, session in (("os/apps", "session: A"), ("os/userland", "session: B"), ("os/userland", "other: A")):
            job = self.send()
            self.helio("take", job, "--session", "A", me="os/userland")
            put((self.store / f"messages/{job}.link.0001"), f"by: {by}\ntime: 2026-11-23T12:00:00Z\n{session}\nstate: done\nnote: \nend: yes\n")
            self.assertEqual(self.helio("check", me=None).returncode, 3, by)
            (self.store / f"messages/{job}.link.0001").unlink()

    def test_an_empty_seen_mark_hides_nothing(self):
        sent = self.send("tell", to="os/apps")
        put((self.store / f"messages/{sent}.seen.os+apps"), "")
        self.assertEqual(self.helio(me="os/apps").returncode, 0)
        self.assertEqual(self.helio("check", me=None).returncode, 3)

    def test_a_key_that_leads_to_another_message_is_refused(self):
        first = self.send("ask", "os/userland", "os/main", "s", "b", "--key", "k")
        self.assertEqual(self.helio("ask", "os/userland", "s", "--key", "k", body="other").returncode, 2)
        self.assertEqual(self.send("ask", "os/userland", "os/main", "s", "b", "--key", "k"), first)

    def test_a_damaged_step_is_reported_and_never_guessed(self):
        for text in ("", "by: os/userland\nstate: ta", "by: os/userland\ntime: 2026-11-23T12:00:00Z\nsession: \nstate: taken\nnote: half", "by: os/userland\ntime: 2026-11-23T12:00:00Z\nsession: \nstate: done\nnote: \nend: yes\n", "x" * 40000):
            job = self.send()
            step = self.store / f"messages/{job}.link.0000"
            put(step, text)
            result = self.helio(me="os/userland")
            self.assertEqual(result.returncode, 3)
            self.assertIn("damaged", result.stdout.decode())
            for command in ("take", "refuse", "done"):
                self.assertEqual(self.helio(command, job, me="os/userland").returncode, 3)
            self.assertEqual(self.helio("check", me=None).returncode, 3)
            step.unlink()

    def test_an_event_without_a_message_is_found(self):
        put((self.store / "messages/0000000000000001-aaaaaa.link.0000"), "x")
        self.assertEqual(self.helio("check", me=None).returncode, 3)

    def test_stored_control_characters_are_not_shown(self):
        sent = self.send("tell", to="os/apps", subject="abc")
        path = self.store / f"messages/{sent}.post"
        whole = path.read_bytes()
        for old, new in ((b"subject: abc", b"subject: a\x1bc"), (b"time: 2", b"time: \x1b"), (b"about: ", b"about: \x1b"), (b"\n\nDo it", b"\n\nDo\x1bt")):
            self.assertIn(old, whole)
            path.write_bytes(whole.replace(old, new))
            self.assertNotIn(b"\x1b", self.helio(me="os/apps").stdout + self.helio("show", sent, me="os/apps").stdout, new)

    @unittest.skipIf(ON_WINDOWS, "needs a file system with pipes")
    def test_a_pipe_or_a_link_in_the_store_does_not_hang_or_leave_it(self):
        with tempfile.TemporaryDirectory() as folder:  # D: has no pipes
            self.store = Path(folder)
            self.helio("init", me=None)
            sent = self.send("tell", to="os/apps")
            os.mkfifo(self.store / "messages/0000000000000001-aaaaaa.post")
            job = self.send()
            os.symlink(self.store / "nowhere", self.store / f"messages/{job}.link.0000")
            self.assertEqual(self.helio("take", job, me="os/userland").returncode, 3)
            os.symlink(self.store / f"messages/{sent}.post", self.store / "messages/0000000000000002-aaaaaa.post")
            self.assertEqual(self.helio(me="os/apps").returncode, 3)

    def test_no_store_is_not_an_empty_inbox(self):
        self.assertEqual(run(self.store / "nowhere", cwd=self.store).returncode, 3)


class ArchivingGuards(Base):
    def wait_for(self, path, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.02)
        self.fail(f"timed out waiting for {path}")

    def start_pause(self, op, stages, lane, msg_id):
        return subprocess.Popen(
            [str(RACER), "--pause", op, stages, str(self.store), lane, msg_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_p_remove_deletes_file_preserves_neighbor_and_reports_missing_and_failure(self):
        target = self.store / "target.txt"
        neighbor = self.store / "neighbor.txt"
        put(target, "remove me\n")
        put(neighbor, "keep me\n")
        r1 = subprocess.run([str(RACER), "--remove", str(target)], stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertFalse(target.exists())
        self.assertEqual(neighbor.read_bytes(), b"keep me\n")
        r2 = subprocess.run([str(RACER), "--remove", str(target)], stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        self.assertEqual(r2.returncode, 2, "P_MISSING expected on repeat")
        subdir = self.store / "adir"
        subdir.mkdir()
        r3 = subprocess.run([str(RACER), "--remove", str(subdir)], stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        self.assertEqual(r3.returncode, 4, "P_FAILED expected when removing a directory")
        # Both Linux and Windows must return P_FAILED when the required parent-folder flush fails after deletion.
        flush_victim = self.store / "flush_victim.txt"
        put(flush_victim, "bye\n")
        r_flush = subprocess.run([str(RACER), "--remove-fail-flush", str(flush_victim)], stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        self.assertEqual(r_flush.returncode, 4, "P_FAILED expected when parent-folder flush fails on Linux or Windows")
        self.assertFalse(flush_victim.exists())
        if not ON_WINDOWS:
            locked = self.store / "locked_dir"
            locked.mkdir()
            victim = locked / "victim.txt"
            put(victim, "bye\n")
            locked.chmod(0o300)
            try:
                r4 = subprocess.run([str(RACER), "--remove", str(victim)], stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
                self.assertEqual(r4.returncode, 4, "P_FAILED expected when real OS parent directory flush fails")
            finally:
                locked.chmod(0o700)

    def test_store_job_second_look_notices_archived_post_with_populated_store_open(self):
        # Populate snapshot with link.0000 AND link.0001, pause before reading link.0000, remove post + link.0000
        # while link.0001 remains in snapshot so gap() would be true if checked before live_post!
        job = self.send()
        self.assertEqual(self.helio("take", job, me="os/userland").returncode, 0)
        self.assertEqual(self.helio("release", job, me="os/userland").returncode, 0)
        proc = self.start_pause("job", "before-read", "os/userland", job)
        try:
            self.wait_for(self.store / "paused.before-read")
            (self.store / f"messages/{job}.post").unlink()
            (self.store / f"messages/{job}.link.0000").unlink()
            (self.store / f"messages/{job}.link.0001").unlink()
            put(self.store / "resume.before-read", "go")
            _, stderr = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 2, stderr)
        self.assertIn("it was archived", stderr)
        self.assertEqual(list((self.store / "messages").iterdir()), [])

        # Control: when the live post still exists, a genuine link gap still exits 3 (S_DAMAGED).
        job2 = self.send()
        self.assertEqual(self.helio("take", job2, me="os/userland").returncode, 0)
        self.assertEqual(self.helio("release", job2, me="os/userland").returncode, 0)
        (self.store / f"messages/{job2}.link.0000").unlink()
        res_gap = subprocess.run(
            [str(RACER), "--pause", "job", "", str(self.store), "os/userland", job2],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(res_gap.returncode, 3, res_gap.stderr)
        self.assertIn("a step of this job is missing", res_gap.stderr)

        # Full-chain (MOST_LINKS=1000) boundary: removing post at the full-chain exit still returns S_REFUSED.
        job_full = self.send()
        for n in range(1000):
            state = "released" if n % 2 else "taken"
            put(self.store / f"messages/{job_full}.link.{n:04}", f"by: os/userland\ntime: 2026-11-23T12:00:00Z\nsession: \nstate: {state}\nnote: \nend: yes\n")
        proc_full = self.start_pause("job", "read", "os/userland", job_full)
        try:
            self.wait_for(self.store / "paused.read")
            (self.store / f"messages/{job_full}.post").unlink()
            put(self.store / "resume.read", "go")
            _, stderr_full = proc_full.communicate(timeout=30)
        finally:
            if proc_full.poll() is None:
                proc_full.kill()
        self.assertEqual(proc_full.returncode, 2, stderr_full)
        self.assertIn("it was archived", stderr_full)
        for name in ("paused.read", "resume.read"):
            (self.store / name).unlink(missing_ok=True)

    def test_change_job_second_look_removes_orphan_step_and_handles_edge_cases(self):
        # Case 1: post removed between store_job and add_link; link is created then removed cleanly.
        job = self.send()
        proc = self.start_pause("take", "write,after-write", "os/userland", job)
        try:
            self.wait_for(self.store / "paused.write")
            (self.store / f"messages/{job}.post").unlink()
            put(self.store / "resume.write", "go")
            self.wait_for(self.store / "paused.after-write")
            self.assertTrue((self.store / f"messages/{job}.link.0000").exists(), "writer must have published its link before second look")
            put(self.store / "resume.after-write", "go")
            _, stderr = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 2, stderr)
        self.assertIn("it was archived", stderr)
        self.assertEqual(list((self.store / "messages").iterdir()), [])
        for name in ("paused.write", "resume.write", "paused.after-write", "resume.after-write"):
            (self.store / name).unlink(missing_ok=True)

        # Case 2: already-removed link during rollback counts as clean S_REFUSED (exit 2).
        job2 = self.send()
        proc2 = self.start_pause("take", "write,after-write", "os/userland", job2)
        try:
            self.wait_for(self.store / "paused.write")
            (self.store / f"messages/{job2}.post").unlink()
            put(self.store / "resume.write", "go")
            self.wait_for(self.store / "paused.after-write")
            (self.store / f"messages/{job2}.link.0000").unlink()
            put(self.store / "resume.after-write", "go")
            _, stderr2 = proc2.communicate(timeout=30)
        finally:
            if proc2.poll() is None:
                proc2.kill()
        self.assertEqual(proc2.returncode, 2, stderr2)
        self.assertIn("it was archived", stderr2)
        for name in ("paused.write", "resume.write", "paused.after-write", "resume.after-write"):
            (self.store / name).unlink(missing_ok=True)

        # Case 3: P_EXISTS collision preserves the existing winner's link.
        job3 = self.send(to="os/all")
        proc3 = self.start_pause("take", "write", "os/userland", job3)
        try:
            self.wait_for(self.store / "paused.write")
            self.assertEqual(self.helio("take", job3, me="os/apps").returncode, 0)
            put(self.store / "resume.write", "go")
            _, stderr3 = proc3.communicate(timeout=30)
        finally:
            if proc3.poll() is None:
                proc3.kill()
        self.assertEqual(proc3.returncode, 2, stderr3)
        self.assertIn("already taken", stderr3)
        winner_link = (self.store / f"messages/{job3}.link.0000").read_text()
        self.assertIn("by: os/apps", winner_link)
        for name in ("paused.write", "resume.write"):
            (self.store / name).unlink(missing_ok=True)

        # Case 4: unreadable post (IO error, not P_MISSING) surfaces as exit 3 and preserves our live link.
        job4 = self.send()
        proc4 = self.start_pause("take", "after-write", "os/userland", job4)
        try:
            self.wait_for(self.store / "paused.after-write")
            post4 = self.store / f"messages/{job4}.post"
            post4.unlink()
            post4.mkdir()
            put(self.store / "resume.after-write", "go")
            _, stderr4 = proc4.communicate(timeout=30)
        finally:
            if proc4.poll() is None:
                proc4.kill()
        self.assertEqual(proc4.returncode, 3, stderr4)
        self.assertTrue((self.store / f"messages/{job4}.link.0000").exists(), "IO error must not delete a live link")
        for name in ("paused.after-write", "resume.after-write"):
            (self.store / name).unlink(missing_ok=True)

        # Case 5 (Linux): failed parent-folder flush during rollback surfaces as exit 3.
        if not ON_WINDOWS:
            job5 = self.send()
            proc5 = self.start_pause("take", "write,after-write", "os/userland", job5)
            msgs = self.store / "messages"
            try:
                self.wait_for(self.store / "paused.write")
                (msgs / f"{job5}.post").unlink()
                put(self.store / "resume.write", "go")
                self.wait_for(self.store / "paused.after-write")
                msgs.chmod(0o300)
                put(self.store / "resume.after-write", "go")
                _, stderr5 = proc5.communicate(timeout=30)
            finally:
                msgs.chmod(0o700)
                if proc5.poll() is None:
                    proc5.kill()
            self.assertEqual(proc5.returncode, 3, stderr5)
            self.assertIn("could not be removed", stderr5)
            for name in ("paused.write", "resume.write", "paused.after-write", "resume.after-write"):
                (self.store / name).unlink(missing_ok=True)

            # Case 6 (Linux): failed publication flush during add_link leaves zero orphan link and exits 3.
            for p in list(msgs.iterdir()):
                if p.is_dir():
                    p.rmdir()
                else:
                    p.unlink()
            job6 = self.send()
            proc6 = self.start_pause("take", "write", "os/userland", job6)
            try:
                self.wait_for(self.store / "paused.write")
                (msgs / f"{job6}.post").unlink()
                msgs.chmod(0o300)
                put(self.store / "resume.write", "go")
                _, stderr6 = proc6.communicate(timeout=30)
            finally:
                msgs.chmod(0o700)
                if proc6.poll() is None:
                    proc6.kill()
            self.assertEqual(proc6.returncode, 3, stderr6)
            self.assertEqual(list(msgs.iterdir()), [], "failed publication flush must leave zero orphan link")
            for name in ("paused.write", "resume.write"):
                (self.store / name).unlink(missing_ok=True)

            # Case 7 (Linux): failed publication flush during store_seen leaves zero orphan seen mark and exits 3.
            msg7 = self.send("tell", to="os/userland")
            proc7 = self.start_pause("seen", "seen", "os/userland", msg7)
            try:
                self.wait_for(self.store / "paused.seen")
                (msgs / f"{msg7}.post").unlink()
                msgs.chmod(0o300)
                put(self.store / "resume.seen", "go")
                _, stderr7 = proc7.communicate(timeout=30)
            finally:
                msgs.chmod(0o700)
                if proc7.poll() is None:
                    proc7.kill()
            self.assertEqual(proc7.returncode, 3, stderr7)
            self.assertEqual(list(msgs.iterdir()), [], "failed publication flush must leave zero orphan seen mark")
            for name in ("paused.seen", "resume.seen"):
                (self.store / name).unlink(missing_ok=True)

            # Case 8 (Linux): failed publication flush on a LIVE (non-archived) job preserves the live link
            # and still surfaces exit 3 (never erasing an accepted live publication!).
            job8 = self.send()
            proc8 = self.start_pause("take", "write", "os/userland", job8)
            try:
                self.wait_for(self.store / "paused.write")
                msgs.chmod(0o300)
                put(self.store / "resume.write", "go")
                _, stderr8 = proc8.communicate(timeout=30)
            finally:
                msgs.chmod(0o700)
                if proc8.poll() is None:
                    proc8.kill()
            self.assertEqual(proc8.returncode, 3, stderr8)
            self.assertTrue((msgs / f"{job8}.link.0000").exists(), "failed flush on a live post must NOT erase the live link")

    def test_store_seen_second_look_removes_orphan_seen_mark(self):
        msg = self.send("tell", to="os/userland")
        proc = self.start_pause("seen", "seen,after-seen", "os/userland", msg)
        try:
            self.wait_for(self.store / "paused.seen")
            (self.store / f"messages/{msg}.post").unlink()
            put(self.store / "resume.seen", "go")
            self.wait_for(self.store / "paused.after-seen")
            self.assertTrue((self.store / f"messages/{msg}.seen.os+userland").exists())
            put(self.store / "resume.after-seen", "go")
            _, stderr = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 2, stderr)
        self.assertIn("it was archived", stderr)
        self.assertEqual(list((self.store / "messages").iterdir()), [])
        for name in ("paused.seen", "resume.seen", "paused.after-seen", "resume.after-seen"):
            (self.store / name).unlink(missing_ok=True)

        # Seen-mark collision: existing mark survives, but show/store_seen still refuses when post vanished.
        msg2 = self.send("tell", to="os/userland")
        self.assertEqual(self.helio("show", msg2, me="os/userland").returncode, 0)
        seen_file = self.store / f"messages/{msg2}.seen.os+userland"
        self.assertTrue(seen_file.exists())
        proc2 = self.start_pause("seen", "seen", "os/userland", msg2)
        try:
            self.wait_for(self.store / "paused.seen")
            (self.store / f"messages/{msg2}.post").unlink()
            put(self.store / "resume.seen", "go")
            _, stderr2 = proc2.communicate(timeout=30)
        finally:
            if proc2.poll() is None:
                proc2.kill()
        self.assertEqual(proc2.returncode, 2, stderr2)
        self.assertIn("it was archived", stderr2)
        self.assertTrue(seen_file.exists(), "collision branch must leave existing seen mark intact")

    def start_pause_cli(self, stages, *helio_args, me="os/userland"):
        clean = {k: v for k, v in os.environ.items() if k not in ("HELIO_AS", "HELIO_STORE", "HELIO_SESSION", "CLAUDE_CODE_SESSION_ID")}
        cmd = [str(RACER), "--pause-cli", stages, str(self.store), *helio_args, "--store", str(self.store)]
        if me:
            cmd.extend(["--as", me])
        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self.store,
            env=clean,
        )

    def test_cli_readers_preserve_archived_refusal_without_false_damage(self):
        # 1. Direct `show` gives exit 2 ("it was archived"), no body, and no orphan .seen.* file.
        j_show = self.send(subject="vanish-show")
        p_show = self.start_pause_cli("read", "show", j_show, me="os/userland")
        try:
            self.wait_for(self.store / "paused.read")
            (self.store / f"messages/{j_show}.post").unlink()
            put(self.store / "resume.read", "go")
            out_s, err_s = p_show.communicate(timeout=30)
        finally:
            if p_show.poll() is None:
                p_show.kill()
        self.assertEqual(p_show.returncode, 2, err_s)
        self.assertIn("it was archived", err_s)
        self.assertEqual(out_s, "")
        self.assertEqual(list((self.store / "messages").iterdir()), [])
        for name in ("paused.read", "resume.read"):
            (self.store / name).unlink(missing_ok=True)

        # 2. `log` skips the concurrently-archived job, keeps showing unrelated jobs, and reports no damage.
        j_arch = self.send(subject="vanish-log")
        j_live = self.send(subject="stay-live")
        p_log = self.start_pause_cli("read", "log", me=None)
        try:
            self.wait_for(self.store / "paused.read")
            (self.store / f"messages/{j_arch}.post").unlink()
            put(self.store / "resume.read", "go")
            out_l, err_l = p_log.communicate(timeout=30)
        finally:
            if p_log.poll() is None:
                p_log.kill()
        self.assertEqual(p_log.returncode, 0, err_l)
        self.assertIn("stay-live", out_l)
        self.assertNotIn("vanish-log", out_l)
        self.assertNotIn("damaged", out_l.lower() + err_l.lower())
        for name in ("paused.read", "resume.read"):
            (self.store / name).unlink(missing_ok=True)

        # 3. `watch_summary` + `inbox`: create j_arch2 FIRST so it is enumerated first during watch_summary!
        for p in list((self.store / "messages").iterdir()):
            p.unlink()
        j_arch2 = self.send(subject="vanish-summary")
        j_live2 = self.send(subject="stay-live-2")
        p_in = self.start_pause_cli("read", me="os/userland")
        try:
            self.wait_for(self.store / "paused.read")
            (self.store / f"messages/{j_arch2}.post").unlink()
            put(self.store / "resume.read", "go")
            out_i, err_i = p_in.communicate(timeout=30)
        finally:
            if p_in.poll() is None:
                p_in.kill()
        self.assertEqual(p_in.returncode, 0, err_i)
        self.assertIn("stay-live-2", out_i)
        self.assertNotIn("vanish-summary", out_i)
        self.assertNotIn("damaged", out_i.lower() + err_i.lower())
        summary_files = list((self.store / "summary").glob("*.md"))
        self.assertEqual(len(summary_files), 1)
        summary_text = summary_files[0].read_text()
        self.assertIn("stay-live-2", summary_text)
        self.assertNotIn("Damaged:", summary_text)
        for name in ("paused.read", "resume.read"):
            (self.store / name).unlink(missing_ok=True)

        # 4. `inbox` (summary already exists today, so pause happens directly inside into_inbox on j_arch3!).
        for p in list((self.store / "messages").iterdir()):
            p.unlink()
        j_arch3 = self.send(subject="vanish-inbox")
        j_live3 = self.send(subject="stay-live-3")
        p_in2 = self.start_pause_cli("read", me="os/userland")
        try:
            self.wait_for(self.store / "paused.read")
            (self.store / f"messages/{j_arch3}.post").unlink()
            put(self.store / "resume.read", "go")
            out_i2, err_i2 = p_in2.communicate(timeout=30)
        finally:
            if p_in2.poll() is None:
                p_in2.kill()
        self.assertEqual(p_in2.returncode, 0, err_i2)
        self.assertIn("stay-live-3", out_i2)
        self.assertNotIn("vanish-inbox", out_i2)
        self.assertNotIn("damaged", out_i2.lower() + err_i2.lower())
        for name in ("paused.read", "resume.read"):
            (self.store / name).unlink(missing_ok=True)

        # 5. `hook claude` (j_arch4 created first so pause happens directly inside hook's fill_inbox on j_arch4!).
        for p in list((self.store / "messages").iterdir()):
            p.unlink()
        j_arch4 = self.send(subject="vanish-hook")
        j_live4 = self.send(subject="stay-live-4")
        p_hk = self.start_pause_cli("read", "hook", "claude", me="os/userland")
        try:
            self.wait_for(self.store / "paused.read")
            (self.store / f"messages/{j_arch4}.post").unlink()
            put(self.store / "resume.read", "go")
            out_h, err_h = p_hk.communicate(timeout=30)
        finally:
            if p_hk.poll() is None:
                p_hk.kill()
        self.assertEqual(p_hk.returncode, 0, err_h)
        self.assertIn("stay-live-4", out_h)
        self.assertNotIn("vanish-hook", out_h)
        self.assertNotIn("damaged", out_h.lower() + err_h.lower())
        for name in ("paused.read", "resume.read"):
            (self.store / name).unlink(missing_ok=True)

        # 6. `helio check` during archive removal reports no damage when post and steps vanish together.
        j_chk = self.send(subject="vanish-check")
        self.assertEqual(self.helio("take", j_chk, me="os/userland").returncode, 0)
        p_chk = self.start_pause_cli("read", "check", me=None)
        try:
            self.wait_for(self.store / "paused.read")
            (self.store / f"messages/{j_chk}.post").unlink()
            (self.store / f"messages/{j_chk}.link.0000").unlink()
            put(self.store / "resume.read", "go")
            out_c, err_c = p_chk.communicate(timeout=30)
        finally:
            if p_chk.poll() is None:
                p_chk.kill()
        self.assertEqual(p_chk.returncode, 0, out_c + err_c)
        self.assertEqual(out_c, "")
        for name in ("paused.read", "resume.read"):
            (self.store / name).unlink(missing_ok=True)

        # 6b. `helio check` on a seen mark that disappears during archival:
        #     - when post is also proven gone -> exits 0;
        #     - when post read fails with IO error (directory) -> exits 3 (never healthy!).
        m_seen_ok = self.send("tell", to="os/userland")
        self.assertEqual(self.helio("show", m_seen_ok, me="os/userland").returncode, 0)
        p_cs_ok = self.start_pause_cli("check-seen", "check", me=None)
        try:
            self.wait_for(self.store / "paused.check-seen")
            (self.store / f"messages/{m_seen_ok}.seen.os+userland").unlink()
            (self.store / f"messages/{m_seen_ok}.post").unlink()
            put(self.store / "resume.check-seen", "go")
            out_cs_ok, err_cs_ok = p_cs_ok.communicate(timeout=30)
        finally:
            if p_cs_ok.poll() is None:
                p_cs_ok.kill()
        self.assertEqual(p_cs_ok.returncode, 0, out_cs_ok + err_cs_ok)
        for name in ("paused.check-seen", "resume.check-seen"):
            (self.store / name).unlink(missing_ok=True)

        m_seen_io = self.send("tell", to="os/userland")
        self.assertEqual(self.helio("show", m_seen_io, me="os/userland").returncode, 0)
        p_cs_io = self.start_pause_cli("check-seen", "check", me=None)
        post_io = self.store / f"messages/{m_seen_io}.post"
        try:
            self.wait_for(self.store / "paused.check-seen")
            (self.store / f"messages/{m_seen_io}.seen.os+userland").unlink()
            post_io.unlink()
            post_io.mkdir()
            put(self.store / "resume.check-seen", "go")
            out_cs_io, err_cs_io = p_cs_io.communicate(timeout=30)
        finally:
            if p_cs_io.poll() is None:
                p_cs_io.kill()
            if post_io.is_dir():
                post_io.rmdir()
        self.assertEqual(p_cs_io.returncode, 3, out_cs_io + err_cs_io)
        self.assertIn("cannot be read", out_cs_io + err_cs_io)
        for name in ("paused.check-seen", "resume.check-seen"):
            (self.store / name).unlink(missing_ok=True)

        # 7. Malformed post name (messages/bad.post) is NOT treated as vanished: log exits 3 and summary counts damage.
        put(self.store / "messages/bad.post", "x")
        self.assertEqual(self.helio("log", me=None).returncode, 3)
        for sf in list((self.store / "summary").glob("*.md")):
            sf.unlink()
        self.helio(me="os/userland")
        summary_bad = list((self.store / "summary").glob("*.md"))[0].read_text()
        self.assertIn("Damaged: 1", summary_bad)


class Archive(Base):
    def make_aged_closed_job(self, *, created_us=None, closed_iso="2025-04-15T12:00:00Z", state="done", subject="old-job", key=None, with_seen=False):
        extra = ("--key", key) if key else ()
        if created_us is None:
            job = self.send("ask", "os/userland", "os/main", subject, "body text", *extra)
        else:
            # Synthesize a message whose ID has the exact creation epoch-microseconds (e.g. 2025-02).
            job = f"{created_us:016d}-abc123"
            body = "body text"
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created_us / 1_000_000))
            post = (
                f"id: {job}\ntime: {ts}\nfrom: os/main\nto: os/userland\nkind: request\n"
                f"about: \nurgency: next\nre: \ndue: \nlength: {len(body)}\nsubject: {subject}\n\n{body}"
            )
            put(self.store / f"messages/{job}.post", post)
        put(
            self.store / f"messages/{job}.link.0000",
            f"by: os/userland\ntime: {closed_iso}\nsession: \nstate: taken\nnote: \nend: yes\n",
        )
        put(
            self.store / f"messages/{job}.link.0001",
            f"by: os/userland\ntime: {closed_iso}\nsession: \nstate: {state}\nnote: ok\nend: yes\n",
        )
        if with_seen:
            put(self.store / f"messages/{job}.seen.os+userland", "seen\n")
        return job

    def wait_for(self, path, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.02)
        self.fail(f"timed out waiting for {path}")

    def test_eligibility_month_derivation_and_cli_contract(self):
        # Empty store -> exit 1; extra args -> exit 2; bare invocation works without --as.
        self.assertEqual(self.helio("archive", me=None).returncode, 1)
        self.assertEqual(self.helio("archive", "extra", me=None).returncode, 2)

        # Ineligible: non-job, open job, taken job, closed 30d-60s ago, closed in future.
        msg_tell = self.send("tell", to="os/userland", subject="stay-tell")
        open_job = self.send(subject="stay-open")
        taken_job = self.send(subject="stay-taken")
        self.assertEqual(self.helio("take", taken_job, me="os/userland").returncode, 0)
        # Deterministic exact 30-day boundary via fixed clock T = 2025-05-15T12:00:00Z (1747310400s):
        # T - 30d + 1s (2025-04-15T12:00:01Z) MUST stay live; T - 30d (2025-04-15T12:00:00Z) MUST move!
        fixed_now_us = 1747310400_000_000
        recent_done = self.make_aged_closed_job(closed_iso="2025-04-15T12:00:01Z", state="done", subject="recent-done")
        recent_refused = self.make_aged_closed_job(closed_iso="2025-04-15T12:00:01Z", state="refused", subject="recent-refused")
        future_done = self.make_aged_closed_job(closed_iso="2025-05-15T13:00:00Z", state="done", subject="future-done")
        r_before = subprocess.run([str(RACER), "--now", str(fixed_now_us), "archive", "--store", str(self.store)], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(r_before.returncode, 1, r_before.stdout + r_before.stderr)

        exact_done = self.make_aged_closed_job(closed_iso="2025-04-15T12:00:00Z", state="done", subject="exact-done")
        exact_refused = self.make_aged_closed_job(closed_iso="2025-04-15T12:00:00Z", state="refused", subject="exact-refused")
        res_exact = subprocess.run([str(RACER), "--now", str(fixed_now_us), "archive", "--store", str(self.store)], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(res_exact.returncode, 0, res_exact.stderr)
        self.assertFalse((self.store / f"messages/{exact_done}.post").exists())
        self.assertFalse((self.store / f"messages/{exact_refused}.post").exists())
        self.assertTrue((self.store / f"messages/{recent_done}.post").exists())
        self.assertTrue((self.store / f"messages/{recent_refused}.post").exists())
        # Age recent_done/recent_refused back to now - 1 day so the real-clock archive call below leaves them live too.
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(time.time()) - 86400))
        for rid, st_name in ((recent_done, "done"), (recent_refused, "refused")):
            put(self.store / f"messages/{rid}.link.0001", f"by: os/userland\ntime: {now_iso}\nsession: \nstate: {st_name}\nnote: ok\nend: yes\n")
        put(self.store / f"messages/{future_done}.link.0001", f"by: os/userland\ntime: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(int(time.time()) + 3600))}\nsession: \nstate: done\nnote: ok\nend: yes\n")

        # Eligible: created in 2025-02, closed in 2025-04 -> must land in archive/2025-02/ (ID's month!).
        feb_us = 1739577600_000_000  # 2025-02-15T00:00:00Z
        old_done = self.make_aged_closed_job(created_us=feb_us, closed_iso="2025-04-15T12:00:00Z", state="done", subject="old-done", with_seen=True)
        old_refused = self.make_aged_closed_job(created_us=feb_us + 1, closed_iso="2025-04-16T12:00:00Z", state="refused", subject="old-refused")
        orig_bytes = {
            f.name: f.read_bytes()
            for f in (self.store / "messages").iterdir()
            if f.name.startswith(old_done + ".") or f.name.startswith(old_refused + ".")
        }
        res = self.helio("archive", me=None)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(b"2 archived", res.stdout)
        arch_dir = self.store / "archive/2025-02"
        for name, expected in orig_bytes.items():
            self.assertEqual((arch_dir / name).read_bytes(), expected)
            self.assertFalse((self.store / "messages" / name).exists())
        # Ineligible items stayed live.
        for stay in (msg_tell, open_job, taken_job, recent_done, recent_refused, future_done):
            self.assertTrue((self.store / f"messages/{stay}.post").exists(), stay)
        self.assertEqual(self.helio("check", me=None).returncode, 0)

        # Mixed batch with a damaged job: healthy eligible neighbor still moves, damaged ID named, exit 3.
        bad_job = self.send(subject="bad-job")
        put(self.store / f"messages/{bad_job}.link.0000", "corrupt\n")
        good_old = self.make_aged_closed_job(created_us=feb_us + 2, closed_iso="2025-04-17T12:00:00Z", state="done", subject="good-old")
        res_mix = self.helio("archive", me=None)
        self.assertEqual(res_mix.returncode, 3)
        self.assertIn(bad_job.encode(), res_mix.stderr)
        self.assertTrue((self.store / f"messages/{bad_job}.post").exists())
        self.assertTrue((arch_dir / f"{good_old}.post").exists())
        self.assertFalse((self.store / f"messages/{good_old}.post").exists())

    def test_cut_off_at_every_point_check_before_retry_and_recovery(self):
        stages = [
            "before-copy-post",
            "after-copy-post",
            "before-copy-link",
            "after-copy-links",
            "before-copy-seen",
            "before-remove-post",
            "after-remove-post",
            "before-remove-seen",
            "after-remove-seen",
            "before-remove-link",
        ]
        feb_us = 1739577600_000_000
        for idx, stage in enumerate(stages):
            with self.subTest(stage=stage):
                with tempfile.TemporaryDirectory() as tmp:
                    st = Path(tmp)
                    put(st / "helio.conf", "project: os\n")
                    self.assertEqual(run(st, "init", me=None).returncode, 0)
                    job = f"{feb_us + idx:016d}-c00{idx:03x}"
                    body = f"cut body {idx}"
                    put(
                        st / f"messages/{job}.post",
                        f"id: {job}\ntime: 2025-02-15T00:00:00Z\nfrom: os/main\nto: os/userland\nkind: request\n"
                        f"about: \nurgency: next\nre: \ndue: \nlength: {len(body)}\nsubject: cut-{idx}\n\n{body}",
                    )
                    put(st / f"messages/{job}.link.0000", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: taken\nnote: \nend: yes\n")
                    put(st / f"messages/{job}.link.0001", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: done\nnote: ok\nend: yes\n")
                    put(st / f"messages/{job}.seen.os+userland", "seen\n")
                    orig = {f.name: f.read_bytes() for f in (st / "messages").iterdir()}

                    clean = {k: v for k, v in os.environ.items() if k not in ("HELIO_AS", "HELIO_STORE", "HELIO_SESSION", "CLAUDE_CODE_SESSION_ID")}
                    with subprocess.Popen(
                        [str(RACER), "--pause-cli", stage, str(st), "archive", "--store", str(st)],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        cwd=st,
                        env=clean,
                    ) as proc:
                        try:
                            self.wait_for(st / f"paused.{stage}")
                            proc.kill()
                            proc.wait(timeout=15)
                        finally:
                            if proc.poll() is None:
                                proc.kill()

                    # Before retry: helio check MUST exit 0 (distinguishes interrupted archive from damage!).
                    chk = run(st, "check", me=None)
                    self.assertEqual(chk.returncode, 0, f"check failed at {stage}: {chk.stdout + chk.stderr}")
                    # Retry finishes cleanly with byte-exact copies and empty messages/.
                    res2 = run(st, "archive", me=None)
                    self.assertEqual(res2.returncode, 0, f"retry failed at {stage}: {res2.stderr}")
                    self.assertEqual(list((st / "messages").iterdir()), [])
                    for name, expected in orig.items():
                        self.assertEqual((st / "archive/2025-02" / name).read_bytes(), expected)
                    self.assertEqual(run(st, "check", me=None).returncode, 0)

        # Control 1: intermediate link removal cut (link.0001 already removed, link.0000 survives) -> check 0, retry finishes!
        with tempfile.TemporaryDirectory() as tmp:
            st = Path(tmp)
            put(st / "helio.conf", "project: os\n")
            self.assertEqual(run(st, "init", me=None).returncode, 0)
            jm = f"{feb_us + 88:016d}-a00088"
            (st / "archive/2025-02").mkdir(parents=True, exist_ok=True)
            post_t = f"id: {jm}\ntime: 2025-02-15T00:00:00Z\nfrom: os/main\nto: os/userland\nkind: request\nabout: \nurgency: next\nre: \ndue: \nlength: 4\nsubject: mid\n\nbody"
            l0 = "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: taken\nnote: \nend: yes\n"
            l1 = "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: done\nnote: ok\nend: yes\n"
            put(st / f"archive/2025-02/{jm}.post", post_t)
            put(st / f"archive/2025-02/{jm}.link.0000", l0)
            put(st / f"archive/2025-02/{jm}.link.0001", l1)
            put(st / f"messages/{jm}.link.0000", l0)
            self.assertEqual(run(st, "check", me=None).returncode, 0)
            self.assertEqual(run(st, "archive", me=None).returncode, 0)
            self.assertEqual(list((st / "messages").iterdir()), [])
            self.assertEqual(run(st, "check", me=None).returncode, 0)

        # Control 2: differing live step or malformed live seen mark during recovery MUST fail check and archive (exit 3)!
        with tempfile.TemporaryDirectory() as tmp:
            st = Path(tmp)
            put(st / "helio.conf", "project: os\n")
            self.assertEqual(run(st, "init", me=None).returncode, 0)
            jd = f"{feb_us + 89:016d}-a00089"
            (st / "archive/2025-02").mkdir(parents=True, exist_ok=True)
            put(st / f"archive/2025-02/{jd}.post", f"id: {jd}\ntime: 2025-02-15T00:00:00Z\nfrom: os/main\nto: os/userland\nkind: request\nabout: \nurgency: next\nre: \ndue: \nlength: 4\nsubject: diff\n\nbody")
            put(st / f"archive/2025-02/{jd}.link.0000", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: taken\nnote: \nend: yes\n")
            put(st / f"archive/2025-02/{jd}.link.0001", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: done\nnote: ok\nend: yes\n")
            put(st / f"messages/{jd}.link.0000", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: taken\nnote: DIFF\nend: yes\n")
            self.assertEqual(run(st, "check", me=None).returncode, 3)
            self.assertEqual(run(st, "archive", me=None).returncode, 3)
            self.assertTrue((st / f"messages/{jd}.link.0000").exists())
            (st / f"messages/{jd}.link.0000").unlink()
            put(st / f"messages/{jd}.seen.os+userland", "trunc")
            self.assertEqual(run(st, "check", me=None).returncode, 3)
            self.assertEqual(run(st, "archive", me=None).returncode, 3)
            self.assertTrue((st / f"messages/{jd}.seen.os+userland").exists())

        # Control 3: archived post WITHOUT terminal chain + missing live post MUST exit 3 in BOTH check AND archive!
        job_bad = f"{feb_us + 99:016d}-bad099"
        (self.store / "archive/2025-02").mkdir(parents=True, exist_ok=True)
        put(
            self.store / f"archive/2025-02/{job_bad}.post",
            f"id: {job_bad}\ntime: 2025-02-15T00:00:00Z\nfrom: os/main\nto: os/userland\nkind: request\n"
            f"about: \nurgency: next\nre: \ndue: \nlength: 4\nsubject: bad\n\nbody",
        )
        put(self.store / f"messages/{job_bad}.link.0000", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: taken\nnote: \nend: yes\n")
        self.assertEqual(self.helio("check", me=None).returncode, 3)
        r_bad_arch = self.helio("archive", me=None)
        self.assertEqual(r_bad_arch.returncode, 3)
        self.assertIn(job_bad.encode(), r_bad_arch.stderr)
        self.assertTrue((self.store / f"messages/{job_bad}.link.0000").exists())

    def test_late_seen_mark_preserved_conflict_refused_and_flush_failure_safe(self):
        # 1. Late seen mark written by `show` right before post removal is preserved in archive!
        feb_us = 1739577600_000_000
        job = self.make_aged_closed_job(created_us=feb_us + 10, closed_iso="2025-04-15T12:00:00Z", state="done", subject="late-seen")
        clean = {k: v for k, v in os.environ.items() if k not in ("HELIO_AS", "HELIO_STORE", "HELIO_SESSION", "CLAUDE_CODE_SESSION_ID")}
        proc = subprocess.Popen(
            [str(RACER), "--pause-cli", "before-remove-post", str(self.store), "archive", "--store", str(self.store)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.store,
            env=clean,
        )
        try:
            self.wait_for(self.store / "paused.before-remove-post")
            show_r = self.helio("show", job, me="os/userland")
            self.assertEqual(show_r.returncode, 0, show_r.stderr)
            self.assertTrue((self.store / f"messages/{job}.seen.os+userland").exists())
            put(self.store / "resume.before-remove-post", "go")
            _, err = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual((self.store / f"archive/2025-02/{job}.seen.os+userland").read_bytes(), b"seen\n")
        self.assertEqual(list((self.store / "messages").iterdir()), [])
        self.assertEqual(self.helio("check", me=None).returncode, 0)

        # 2. Differing archive copy refuses with exit 3 and preserves both live and archive copies.
        job2 = self.make_aged_closed_job(created_us=feb_us + 11, closed_iso="2025-04-15T12:00:00Z", state="done", subject="conflict")
        put(self.store / f"archive/2025-02/{job2}.post", "different bytes\n")
        res_conf = self.helio("archive", me=None)
        self.assertEqual(res_conf.returncode, 3)
        self.assertIn(job2.encode(), res_conf.stderr)
        self.assertTrue((self.store / f"messages/{job2}.post").exists())
        self.assertEqual((self.store / f"archive/2025-02/{job2}.post").read_bytes(), b"different bytes\n")
        (self.store / f"archive/2025-02/{job2}.post").unlink()

        # 3. Targeted p_sync failure on identical pre-existing archive copy MUST exit 3 and retain live source!
        put(self.store / f"archive/2025-02/{job2}.post", (self.store / f"messages/{job2}.post").read_text())
        r_sync = subprocess.run(
            [str(RACER), "--fail-sync-cli", "archive", "--store", str(self.store)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(r_sync.returncode, 3)
        self.assertTrue((self.store / f"messages/{job2}.post").exists())
        # 4. Daily inbox archive error MUST surface exit 3 (never exit 0!).
        r_in_err = subprocess.run(
            [str(RACER), "--fail-sync-cli", "inbox", "--store", str(self.store), "--as", "os/userland"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(r_in_err.returncode, 3)
        # 5. Symlinked archive month directory pointing back to messages/ MUST be refused (exit 3, live files preserved)!
        if not ON_WINDOWS:
            with tempfile.TemporaryDirectory() as tmp:
                st = Path(tmp)
                put(st / "helio.conf", "project: os\n")
                self.assertEqual(run(st, "init", me=None).returncode, 0)
                jsym = f"{feb_us + 77:016d}-a00077"
                put(st / f"messages/{jsym}.post", f"id: {jsym}\ntime: 2025-02-15T00:00:00Z\nfrom: os/main\nto: os/userland\nkind: request\nabout: \nurgency: next\nre: \ndue: \nlength: 4\nsubject: sym\n\nbody")
                put(st / f"messages/{jsym}.link.0000", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: taken\nnote: \nend: yes\n")
                put(st / f"messages/{jsym}.link.0001", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: done\nnote: ok\nend: yes\n")
                (st / "archive").mkdir()
                os.symlink(st / "messages", st / "archive/2025-02")
                self.assertEqual(run(st, "archive", me=None).returncode, 3)
                self.assertTrue((st / f"messages/{jsym}.post").exists())
        # 5b. Recovery when archived terminal links already exist MUST preserve live post if a live seen mark conflicts!
        with tempfile.TemporaryDirectory() as tmp:
            st = Path(tmp)
            put(st / "helio.conf", "project: os\n")
            self.assertEqual(run(st, "init", me=None).returncode, 0)
            jrec = f"{feb_us + 88:016d}-a00088"
            post_body = f"id: {jrec}\ntime: 2025-02-15T00:00:00Z\nfrom: os/main\nto: os/userland\nkind: request\nabout: \nurgency: next\nre: \ndue: \nlength: 4\nsubject: rec\n\nbody"
            link0 = "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: taken\nnote: \nend: yes\n"
            link1 = "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: done\nnote: ok\nend: yes\n"
            put(st / f"messages/{jrec}.post", post_body)
            put(st / f"messages/{jrec}.link.0000", link0)
            put(st / f"messages/{jrec}.link.0001", link1)
            put(st / f"messages/{jrec}.seen.os~userland", "seen\n")
            (st / "archive/2025-02").mkdir(parents=True)
            put(st / f"archive/2025-02/{jrec}.post", post_body)
            put(st / f"archive/2025-02/{jrec}.link.0000", link0)
            put(st / f"archive/2025-02/{jrec}.link.0001", link1)
            put(st / f"archive/2025-02/{jrec}.seen.os~userland", "CONFLICTING SEEN\n")
            r_rec = run(st, "archive", me=None)
            self.assertEqual(r_rec.returncode, 3)
            self.assertTrue((st / f"messages/{jrec}.post").exists(), "live post must stay intact when seen mark conflicts")
        # Normal archive recovers cleanly afterwards.
        self.assertEqual(self.helio("archive", me=None).returncode, 0)
        self.assertFalse((self.store / f"messages/{job2}.post").exists())

    def test_keyed_retry_after_archive_returns_original_id_and_never_reopens(self):
        job = self.make_aged_closed_job(closed_iso="2025-04-15T12:00:00Z", state="done", subject="keyed-job", key="k-arch-1")
        self.assertEqual(self.helio("archive", me=None).returncode, 0)
        self.assertFalse((self.store / f"messages/{job}.post").exists())
        # Retry with same key and content -> returns original ID, creates NO live post, cannot be taken again!
        retry = self.helio("ask", "os/userland", "keyed-job", "--key", "k-arch-1", me="os/main", body="body text")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(retry.stdout.decode().strip(), job)
        self.assertFalse((self.store / f"messages/{job}.post").exists())
        self.assertEqual(self.helio("take", job, me="os/userland").returncode, 2)
        # Retry with same key and DIFFERENT body -> refused (exit 2)!
        bad_retry = self.helio("ask", "os/userland", "keyed-job", "--key", "k-arch-1", me="os/main", body="other body")
        self.assertEqual(bad_retry.returncode, 2)

        # P1 race: suspend keyed retry after initial archive lookup returns missing, archive completely, then resume!
        job_r = self.make_aged_closed_job(closed_iso="2025-04-15T12:00:00Z", state="done", subject="keyed-race", key="k-race-1")
        clean = {k: v for k, v in os.environ.items() if k not in ("HELIO_AS", "HELIO_STORE", "HELIO_SESSION", "CLAUDE_CODE_SESSION_ID")}
        proc = subprocess.Popen(
            [str(RACER), "--pause-cli", "post-write", str(self.store), "ask", "os/userland", "keyed-race", "--key", "k-race-1", "--store", str(self.store), "--as", "os/main"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.store,
            env=clean,
        )
        try:
            proc.stdin.write(b"body text")
            proc.stdin.close()
            self.wait_for(self.store / "paused.post-write")
            self.assertEqual(self.helio("archive", me=None).returncode, 0)
            self.assertFalse((self.store / f"messages/{job_r}.post").exists())
            put(self.store / "resume.post-write", "go")
            out_r, err_r = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 0, err_r)
        self.assertEqual(out_r.decode().strip(), job_r)
        self.assertFalse((self.store / f"messages/{job_r}.post").exists(), "racing keyed retry must not leave a reopened live post")
        self.assertEqual(self.helio("take", job_r, me="os/userland").returncode, 2)
        for name in ("paused.post-write", "resume.post-write"):
            (self.store / name).unlink(missing_ok=True)

        # P1 race 2 (killed sender): kill keyed retry RIGHT AFTER publication (before rollback) -> take STILL exits 2!
        job_k = self.make_aged_closed_job(closed_iso="2025-04-15T12:00:00Z", state="done", subject="keyed-kill", key="k-kill-1")
        proc_k = subprocess.Popen(
            [str(RACER), "--pause-cli", "post-write,after-post-write", str(self.store), "ask", "os/userland", "keyed-kill", "--key", "k-kill-1", "--store", str(self.store), "--as", "os/main"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=self.store,
            env=clean,
        )
        try:
            proc_k.stdin.write(b"body text")
            proc_k.stdin.close()
            self.wait_for(self.store / "paused.post-write")
            self.assertEqual(self.helio("archive", me=None).returncode, 0)
            put(self.store / "resume.post-write", "go")
            self.wait_for(self.store / "paused.after-post-write")
            proc_k.kill()
            proc_k.wait(timeout=15)
        finally:
            if proc_k.poll() is None:
                proc_k.kill()
        self.assertTrue((self.store / f"messages/{job_k}.post").exists(), "killed sender left temporary duplicate post")
        take_k = self.helio("take", job_k, me="os/userland")
        self.assertEqual(take_k.returncode, 2, take_k.stderr)
        self.assertIn(b"archived", take_k.stderr)
        self.assertEqual(self.helio("archive", me=None).returncode, 0)
        self.assertFalse((self.store / f"messages/{job_k}.post").exists())
        for name in ("paused.post-write", "resume.post-write", "paused.after-post-write", "resume.after-post-write"):
            (self.store / name).unlink(missing_ok=True)

        # P1 race 3 (create collision racing post removal): live post exists at create, archived before collision read!
        job_c = self.make_aged_closed_job(closed_iso="2025-04-15T12:00:00Z", state="done", subject="keyed-coll", key="k-coll-1")
        proc_c = subprocess.Popen(
            [str(RACER), "--pause-cli", "post-collision", str(self.store), "ask", "os/userland", "keyed-coll", "--key", "k-coll-1", "--store", str(self.store), "--as", "os/main"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.store,
            env=clean,
        )
        try:
            proc_c.stdin.write(b"body text")
            proc_c.stdin.close()
            self.wait_for(self.store / "paused.post-collision")
            self.assertEqual(self.helio("archive", me=None).returncode, 0)
            put(self.store / "resume.post-collision", "go")
            out_c, err_c = proc_c.communicate(timeout=30)
        finally:
            if proc_c.poll() is None:
                proc_c.kill()
        self.assertEqual(proc_c.returncode, 0, err_c)
        self.assertEqual(out_c.decode().strip(), job_c)

        # P1 race 4 (in-flight taker passed pre-check BEFORE archival, then killed retry republishes post):
        # taker MUST refuse (exit 2) on post-publish archive check and roll back its link.0000!
        job_inf = self.make_aged_closed_job(closed_iso="2025-04-15T12:00:00Z", state="done", subject="keyed-inflight", key="k-inf-1")
        # Temporarily remove links so an in-flight taker can pass pre-check while open, then restore terminal links & archive!
        l0_txt = (self.store / f"messages/{job_inf}.link.0000").read_text()
        l1_txt = (self.store / f"messages/{job_inf}.link.0001").read_text()
        (self.store / f"messages/{job_inf}.link.0000").unlink()
        (self.store / f"messages/{job_inf}.link.0001").unlink()
        proc_t = subprocess.Popen(
            [str(RACER), "--pause", "take", "write", str(self.store), "os/userland", job_inf],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.store,
            env=clean,
        )
        try:
            self.wait_for(self.store / "paused.write")
            # Restore terminal links and archive completely while taker is paused right before add_link!
            put(self.store / f"messages/{job_inf}.link.0000", l0_txt)
            put(self.store / f"messages/{job_inf}.link.0001", l1_txt)
            self.assertEqual(self.helio("archive", me=None).returncode, 0)
            # Simulate a killed keyed retry leaving a byte-identical duplicate live post while archived terminal job exists!
            arch_post = next((self.store / "archive").glob(f"*/{job_inf}.post"))
            orig_arch_bytes = arch_post.read_bytes()
            put(self.store / f"messages/{job_inf}.post", orig_arch_bytes.decode())
            put(self.store / "resume.write", "go")
            _, err_t = proc_t.communicate(timeout=30)
        finally:
            if proc_t.poll() is None:
                proc_t.kill()
        self.assertEqual(proc_t.returncode, 2, err_t)
        self.assertIn(b"archived", err_t)
        self.assertFalse((self.store / f"messages/{job_inf}.link.0000").exists(), "rolled-back take must leave no link.0000")
        # A byte-different live post (even differing only in time:) MUST be refused (exit 3) and both files preserved!
        dup_lines = orig_arch_bytes.decode().splitlines(keepends=True)
        diff_txt = "".join("time: 2026-12-31T23:59:59Z\n" if ln.startswith("time: ") else ln for ln in dup_lines)
        put(self.store / f"messages/{job_inf}.post", diff_txt)
        self.assertEqual(self.helio("archive", me=None).returncode, 3)
        self.assertTrue((self.store / f"messages/{job_inf}.post").exists(), "byte-different live post must be preserved")
        self.assertEqual(arch_post.read_bytes(), orig_arch_bytes)
        # Restoring exact archived bytes sweeps cleanly!
        put(self.store / f"messages/{job_inf}.post", orig_arch_bytes.decode())
        self.assertEqual(self.helio("archive", me=None).returncode, 0)
        self.assertFalse((self.store / f"messages/{job_inf}.post").exists())
        self.assertEqual(arch_post.read_bytes(), orig_arch_bytes, "original archived post must remain untouched")
        for name in ("paused.write", "resume.write"):
            (self.store / name).unlink(missing_ok=True)

        # P2 regression (in-progress partial archive in the job's REAL month must NOT cause healthy live job mutation to report damaged!):
        live_part = self.send(to="os/userland", subject="partial-archive-live")
        live_us = int(live_part.split("-", 1)[0])
        live_month = time.strftime("%Y-%m", time.gmtime(live_us / 1_000_000))
        (self.store / f"archive/{live_month}").mkdir(parents=True, exist_ok=True)
        put(self.store / f"archive/{live_month}/{live_part}.post", (self.store / f"messages/{live_part}.post").read_text())
        take_part = self.helio("take", live_part, me="os/userland")
        self.assertEqual(take_part.returncode, 0, take_part.stderr)
        (self.store / f"archive/{live_month}/{live_part}.post").unlink()

    def test_daily_200_limit_hook_first_concurrent_archive_and_overfull_recovery(self):
        # Create 201 eligible closed jobs + 1 open job.
        feb_us = 1739577600_000_000
        for i in range(201):
            self.make_aged_closed_job(created_us=feb_us + i, closed_iso="2025-04-15T12:00:00Z", state="done", subject=f"bulk-{i}")
        open_job = self.send(subject="stay-open-bulk")
        # Hook runs first -> writes summary/<today>.md, does NOT archive.
        self.assertEqual(self.helio("hook", "claude", me="os/userland").returncode, 0)
        self.assertEqual(len(list((self.store / "archive/2025-02").glob("*.post"))) if (self.store / "archive/2025-02").exists() else 0, 0)
        # Paused concurrent first inbox (B sees .archived missing, pauses before claim while A finishes 200,
        # then B resumes): B MUST re-check .archived after acquiring claim and move 0 (total stays 200)!
        clean = {k: v for k, v in os.environ.items() if k not in ("HELIO_AS", "HELIO_STORE", "HELIO_SESSION", "CLAUDE_CODE_SESSION_ID")}
        p_b = subprocess.Popen(
            [str(RACER), "--pause-cli", "before-daily-claim", str(self.store), "inbox", "--store", str(self.store), "--as", "os/userland"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.store,
            env=clean,
        )
        try:
            self.wait_for(self.store / "paused.before-daily-claim")
            self.assertEqual(self.helio(me="os/userland").returncode, 0)
            self.assertEqual(len(list((self.store / "archive/2025-02").glob("*.post"))), 200)
            put(self.store / "resume.before-daily-claim", "go")
            _, err_b = p_b.communicate(timeout=30)
        finally:
            if p_b.poll() is None:
                p_b.kill()
        self.assertEqual(p_b.returncode, 0, err_b)
        self.assertEqual(len(list((self.store / "archive/2025-02").glob("*.post"))), 200)
        self.assertEqual(len(list((self.store / "messages").glob("*.post"))), 2)  # 1 remaining eligible + 1 open
        # Next day (--now tomorrow) gets a fresh budget and archives the 201st automatically!
        tomorrow_us = (int(time.time()) + 86400) * 1_000_000
        r_next = subprocess.run([str(RACER), "--now", str(tomorrow_us), "inbox", "--store", str(self.store), "--as", "os/userland"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(r_next.returncode, 0, r_next.stderr)
        self.assertEqual(len(list((self.store / "archive/2025-02").glob("*.post"))), 201)
        self.assertTrue((self.store / f"messages/{open_job}.post").exists())

        # Interrupted inbox mid-batch + TWO concurrent recovering inboxes MUST NOT exceed 200 total!
        with tempfile.TemporaryDirectory() as tmp:
            st = Path(tmp)
            put(st / "helio.conf", "project: os\n")
            self.assertEqual(run(st, "init", me=None).returncode, 0)
            for i in range(210):
                j = f"{feb_us + 900 + i:016d}-d{i:05x}"
                put(st / f"messages/{j}.post", f"id: {j}\ntime: 2025-02-15T00:00:00Z\nfrom: os/main\nto: os/userland\nkind: request\nabout: \nurgency: next\nre: \ndue: \nlength: 4\nsubject: d\n\nbody")
                put(st / f"messages/{j}.link.0000", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: taken\nnote: \nend: yes\n")
                put(st / f"messages/{j}.link.0001", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: done\nnote: ok\nend: yes\n")
            p_int = subprocess.Popen(
                [str(RACER), "--pause-cli", "after-remove-post", str(st), "inbox", "--store", str(st), "--as", "os/userland"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=st,
                env=clean,
            )
            try:
                self.wait_for(st / "paused.after-remove-post")
                p_int.kill()
                p_int.wait(timeout=15)
            finally:
                if p_int.poll() is None:
                    p_int.kill()
            # Two simultaneous recovering inboxes race for remaining slots: total moved across ALL runs MUST be <= 200!
            with ThreadPoolExecutor(2) as pool:
                rec_codes = list(pool.map(lambda _: run(st, "inbox", me="os/userland").returncode, range(2)))
            for rc in rec_codes:
                self.assertIn(rc, (0, 1))
            moved_total = len(list((st / "archive/2025-02").glob("*.post")))
            self.assertLessEqual(moved_total, 200, f"interrupted + concurrent recovery exceeded 200: {moved_total}")
            self.assertGreater(moved_total, 0)

        # Two truly simultaneous fresh inboxes on 210 eligible jobs MUST NOT exceed 200 total!
        with tempfile.TemporaryDirectory() as tmp:
            st = Path(tmp)
            put(st / "helio.conf", "project: os\n")
            self.assertEqual(run(st, "init", me=None).returncode, 0)
            for i in range(210):
                j = f"{feb_us + 2000 + i:016d}-e{i:05x}"
                put(st / f"messages/{j}.post", f"id: {j}\ntime: 2025-02-15T00:00:00Z\nfrom: os/main\nto: os/userland\nkind: request\nabout: \nurgency: next\nre: \ndue: \nlength: 4\nsubject: e\n\nbody")
                put(st / f"messages/{j}.link.0000", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: taken\nnote: \nend: yes\n")
                put(st / f"messages/{j}.link.0001", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: done\nnote: ok\nend: yes\n")
            with ThreadPoolExecutor(3) as pool:
                sim_codes = list(pool.map(lambda _: run(st, "inbox", me="os/userland").returncode, range(3)))
            for rc in sim_codes:
                self.assertIn(rc, (0, 1))
            moved_sim = len(list((st / "archive/2025-02").glob("*.post")))
            self.assertLessEqual(moved_sim, 200, f"simultaneous inboxes exceeded 200: {moved_sim}")
            self.assertGreater(moved_sim, 0)
            # A subsequent inbox on the same date MUST NOT move any more jobs!
            self.assertIn(run(st, "inbox", me="os/userland").returncode, (0, 1))
            self.assertEqual(len(list((st / "archive/2025-02").glob("*.post"))), moved_sim)

        # Upgrade continuity: legacy summary/<today>.claiming with 100 prior moves caps remaining budget to 100!
        with tempfile.TemporaryDirectory() as tmp:
            st = Path(tmp)
            put(st / "helio.conf", "project: os\n")
            self.assertEqual(run(st, "init", me=None).returncode, 0)
            today_iso = time.strftime("%Y-%m-%d", time.gmtime())
            (st / "summary").mkdir(exist_ok=True)
            put(st / f"summary/{today_iso}.claiming", "1 100\n")
            for i in range(150):
                j = f"{feb_us + 3000 + i:016d}-f{i:05x}"
                put(st / f"messages/{j}.post", f"id: {j}\ntime: 2025-02-15T00:00:00Z\nfrom: os/main\nto: os/userland\nkind: request\nabout: \nurgency: next\nre: \ndue: \nlength: 4\nsubject: f\n\nbody")
                put(st / f"messages/{j}.link.0000", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: taken\nnote: \nend: yes\n")
                put(st / f"messages/{j}.link.0001", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: done\nnote: ok\nend: yes\n")
            self.assertIn(run(st, "inbox", me="os/userland").returncode, (0, 1))
            self.assertEqual(len(list((st / "archive/2025-02").glob("*.post"))), 100)
            self.assertTrue((st / f"summary/{today_iso}.archived").exists())

        # Crash after slot 9 without .archived: next inbox moves 0 and publishes .archived!
        with tempfile.TemporaryDirectory() as tmp:
            st = Path(tmp)
            put(st / "helio.conf", "project: os\n")
            self.assertEqual(run(st, "init", me=None).returncode, 0)
            today_iso = time.strftime("%Y-%m-%d", time.gmtime())
            (st / "summary").mkdir(exist_ok=True)
            for s_idx in range(10):
                put(st / f"summary/{today_iso}.slot.{s_idx}", "1\n")
            j = f"{feb_us + 4000:016d}-a04000"
            put(st / f"messages/{j}.post", f"id: {j}\ntime: 2025-02-15T00:00:00Z\nfrom: os/main\nto: os/userland\nkind: request\nabout: \nurgency: next\nre: \ndue: \nlength: 4\nsubject: s9\n\nbody")
            put(st / f"messages/{j}.link.0000", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: taken\nnote: \nend: yes\n")
            put(st / f"messages/{j}.link.0001", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: done\nnote: ok\nend: yes\n")
            self.assertIn(run(st, "inbox", me="os/userland").returncode, (0, 1))
            self.assertTrue((st / f"messages/{j}.post").exists())
            self.assertTrue((st / f"summary/{today_iso}.archived").exists())

        # Live final-slot owner (slot 9 active, not stale): concurrent inbox MUST NOT publish .archived prematurely,
        # and slot 9's owner MUST finish its 20-job batch and publish .archived on completion!
        with tempfile.TemporaryDirectory() as tmp:
            st = Path(tmp)
            put(st / "helio.conf", "project: os\n")
            self.assertEqual(run(st, "init", me=None).returncode, 0)
            today_iso = time.strftime("%Y-%m-%d", time.gmtime())
            (st / "summary").mkdir(exist_ok=True)
            for s_idx in range(9):
                put(st / f"summary/{today_iso}.slot.{s_idx}", "1\n")
                put(st / f"summary/{today_iso}.done.{s_idx}", "done\n")
            for i in range(25):
                j = f"{feb_us + 5000 + i:016d}-b{i:05x}"
                put(st / f"messages/{j}.post", f"id: {j}\ntime: 2025-02-15T00:00:00Z\nfrom: os/main\nto: os/userland\nkind: request\nabout: \nurgency: next\nre: \ndue: \nlength: 4\nsubject: s9live\n\nbody")
                put(st / f"messages/{j}.link.0000", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: taken\nnote: \nend: yes\n")
                put(st / f"messages/{j}.link.0001", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: done\nnote: ok\nend: yes\n")
            p_owner = subprocess.Popen(
                [str(RACER), "--pause-cli", "before-copy-post", str(st), "inbox", "--store", str(st), "--as", "os/userland"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=st,
                env=clean,
            )
            try:
                self.wait_for(st / "paused.before-copy-post")
                # Slot 9 is now actively owned; concurrent inbox sees all 10 slots occupied and MUST NOT publish .archived!
                self.assertIn(run(st, "inbox", me="os/userland").returncode, (0, 1))
                self.assertFalse((st / f"summary/{today_iso}.archived").exists(), "concurrent inbox published .archived while slot 9 was still active")
                put(st / "resume.before-copy-post", "go")
                _, err_own = p_owner.communicate(timeout=30)
            finally:
                if p_owner.poll() is None:
                    p_owner.kill()
            self.assertIn(p_owner.returncode, (0, 1), err_own)
            self.assertEqual(len(list((st / "archive/2025-02").glob("*.post"))), 20)
            self.assertTrue((st / f"summary/{today_iso}.done.9").exists())
            self.assertTrue((st / f"summary/{today_iso}.archived").exists())

        # Overfull-store recovery via --max-names: inbox refuses, archive drains, inbox recovers!
        with tempfile.TemporaryDirectory() as tmp:
            st = Path(tmp)
            put(st / "helio.conf", "project: os\n")
            self.assertEqual(run(st, "init", me=None).returncode, 0)
            for i in range(4):
                j = f"{feb_us + 500 + i:016d}-f00{i:03x}"
                put(st / f"messages/{j}.post", f"id: {j}\ntime: 2025-02-15T00:00:00Z\nfrom: os/main\nto: os/userland\nkind: request\nabout: \nurgency: next\nre: \ndue: \nlength: 4\nsubject: ovf\n\nbody")
                put(st / f"messages/{j}.link.0000", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: taken\nnote: \nend: yes\n")
                put(st / f"messages/{j}.link.0001", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: done\nnote: ok\nend: yes\n")
            live_open = run(st, "ask", "os/userland", "keep-open", "--key", "keep-k", me="os/main", body="b").stdout.decode().strip()
            # 14 files > max-names 10 -> inbox fails with "too full", archive drains, inbox succeeds!
            r_full = subprocess.run([str(RACER), "--max-names", "10", "inbox", "--store", str(st), "--as", "os/userland"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
            self.assertEqual(r_full.returncode, 3, r_full.stderr)
            self.assertIn("too full", r_full.stderr)
            r_drain = subprocess.run([str(RACER), "--max-names", "10", "archive", "--store", str(st)], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
            self.assertEqual(r_drain.returncode, 0, r_drain.stderr)
            r_ok = subprocess.run([str(RACER), "--max-names", "10", "inbox", "--store", str(st), "--as", "os/userland"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
            self.assertEqual(r_ok.returncode, 0, r_ok.stderr)
            self.assertIn(live_open, r_ok.stdout)
            self.assertTrue(any(f.name.startswith("key.") for f in (st / "messages").iterdir()))

        # Two concurrent `helio archive` runs both exit 0 and move each message once.
        with tempfile.TemporaryDirectory() as tmp:
            st = Path(tmp)
            put(st / "helio.conf", "project: os\n")
            self.assertEqual(run(st, "init", me=None).returncode, 0)
            for i in range(16):
                j = f"{feb_us + 700 + i:016d}-cc{i:04x}"
                put(st / f"messages/{j}.post", f"id: {j}\ntime: 2025-02-15T00:00:00Z\nfrom: os/main\nto: os/userland\nkind: request\nabout: \nurgency: next\nre: \ndue: \nlength: 4\nsubject: cc\n\nbody")
                put(st / f"messages/{j}.link.0000", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: taken\nnote: \nend: yes\n")
                put(st / f"messages/{j}.link.0001", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: done\nnote: ok\nend: yes\n")
            with ThreadPoolExecutor(2) as pool:
                codes = list(pool.map(lambda _: run(st, "archive", me=None).returncode, range(2)))
            self.assertEqual(codes, [0, 0])
            self.assertEqual(len(list((st / "archive/2025-02").glob("*.post"))), 16)
            self.assertEqual(list((st / "messages").iterdir()), [])

    @unittest.skipUnless(WSL, "needs WSL")
    def test_windows_and_linux_archive_race_1000_rounds(self):
        # Place the shared cross-host store on Windows NTFS (accessible from WSL via DrvFs and from Windows
        # natively) so BOTH Linux fsync(dir) AND Windows FlushFileBuffers(dir) run strictly with zero workarounds.
        win_tmp_raw = subprocess.run(["/mnt/c/Windows/System32/cmd.exe", "/c", "echo %TEMP%"], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        wsl_tmp_root = Path(subprocess.run(["wslpath", "-u", win_tmp_raw], capture_output=True, text=True, check=True, timeout=30).stdout.strip())
        with tempfile.TemporaryDirectory(dir=wsl_tmp_root, prefix="helio_race_") as shared_tmp:
            st = Path(shared_tmp)
            put(st / "helio.conf", "project: os\n")
            self.assertEqual(run(st, "init", me=None).returncode, 0)
            feb_us = 1739577600_000_000
            old_ids = []
            for i in range(1000):
                j = f"{feb_us + i:016d}-{i:06x}"
                put(st / f"messages/{j}.post", f"id: {j}\ntime: 2025-02-15T00:00:00Z\nfrom: os/main\nto: os/userland\nkind: request\nabout: \nurgency: next\nre: \ndue: \nlength: 4\nsubject: arch-{i}\n\nbody")
                put(st / f"messages/{j}.link.0000", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: taken\nnote: \nend: yes\n")
                put(st / f"messages/{j}.link.0001", "by: os/userland\ntime: 2025-04-15T12:00:00Z\nsession: \nstate: done\nnote: ok\nend: yes\n")
                old_ids.append(j)
            live_ids = [run(st, "ask", "os/all", f"live-{i}", me="os/main", body="body").stdout.decode().strip() for i in range(20)]
            win_store = subprocess.run(["wslpath", "-w", str(st)], capture_output=True, text=True, check=True, timeout=30).stdout.strip()
            win_racer = str(BUILD / "racer.exe")
            lin_racer = str(RACER)
            # 2 archivers (limit=1 per round: Windows 300 rounds + Linux 1000 rounds, moving all 1,000 distinct eligible jobs),
            # 2 concurrent readers (Windows min 200 + Linux min 800 = 1,000+ rounds running until BOTH archivers finish,
            # checking ALL 20 live controls AND 100% of ALL 1,000 eligible IDs on EVERY SINGLE ROUND),
            # and 2 racing takers (Windows + Linux).
            put(st / "watch_old.txt", "\n".join(old_ids) + "\n")
            take_ids = old_ids[::25] + live_ids
            procs = [
                subprocess.Popen([win_racer, "--archive-loop", win_store, "300"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE),
                subprocess.Popen([lin_racer, "--archive-loop", str(st), "1000"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE),
                subprocess.Popen([win_racer, "--read-loop", win_store, "200", str(len(live_ids)), *live_ids], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE),
                subprocess.Popen([lin_racer, "--read-loop", str(st), "800", str(len(live_ids)), *live_ids], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE),
                subprocess.Popen([win_racer, win_store, "os/win-taker", *take_ids[::-1]], stdin=subprocess.DEVNULL),
                subprocess.Popen([lin_racer, str(st), "os/lin-taker", *take_ids], stdin=subprocess.DEVNULL),
            ]
            try:
                for _ in range(300):
                    if len(list(st.glob("ready.*"))) == len(procs):
                        break
                    time.sleep(0.05)
                put(st / "go", "x")
                _, arch_win_err = procs[0].communicate(timeout=240)
                _, arch_lin_err = procs[1].communicate(timeout=240)
                arch_win_rc = procs[0].returncode
                arch_lin_rc = procs[1].returncode
                # Signal readers ONLY AFTER both archivers have finished all 1,000 moves!
                put(st / "done", "x")
                _, read_win_err = procs[2].communicate(timeout=240)
                _, read_lin_err = procs[3].communicate(timeout=240)
                read_win_rc = procs[2].returncode
                read_lin_rc = procs[3].returncode
                win_won = procs[4].wait(240)
                lin_won = procs[5].wait(240)
            finally:
                for p in procs:
                    if p.poll() is None:
                        p.kill()
                        p.wait(timeout=15)
                    if p.stdout:
                        p.stdout.close()
                    if p.stderr:
                        p.stderr.close()
            self.assertEqual(arch_win_rc, 0, arch_win_err)
            self.assertEqual(arch_lin_rc, 0, arch_lin_err)
            self.assertEqual(read_win_rc, 0, read_win_err)
            self.assertEqual(read_lin_rc, 0, read_lin_err)
            # Every live control job taken exactly once (per-job check, not aggregate!) and zero eligible old jobs taken!
            self.assertEqual(win_won + lin_won, len(live_ids), (win_won, lin_won))
            for lid in live_ids:
                self.assertTrue((st / f"messages/{lid}.post").exists(), lid)
                links = sorted(st.glob(f"messages/{lid}.link.*"))
                self.assertEqual([p.name for p in links], [f"{lid}.link.0000"], lid)
            # Every one of the 1,000 eligible old jobs moved to archive/2025-02 with its complete terminal chain and gone from messages/.
            arch_dir = st / "archive/2025-02"
            for oid in old_ids:
                self.assertFalse((st / f"messages/{oid}.post").exists(), oid)
                self.assertTrue((arch_dir / f"{oid}.post").exists(), oid)
                self.assertTrue((arch_dir / f"{oid}.link.0000").exists(), oid)
                self.assertTrue((arch_dir / f"{oid}.link.0001").exists(), oid)
            self.assertEqual(run(st, "check", me=None).returncode, 0)


if __name__ == "__main__":
    unittest.main()
