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

        # 7. Malformed post name (messages/bad.post) is NOT treated as vanished: log exits 3 and summary counts damage.
        put(self.store / "messages/bad.post", "x")
        self.assertEqual(self.helio("log", me=None).returncode, 3)
        for sf in list((self.store / "summary").glob("*.md")):
            sf.unlink()
        self.helio(me="os/userland")
        summary_bad = list((self.store / "summary").glob("*.md"))[0].read_text()
        self.assertIn("Damaged: 1", summary_bad)


if __name__ == "__main__":
    unittest.main()
