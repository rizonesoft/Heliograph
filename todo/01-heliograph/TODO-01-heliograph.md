---
schema_version: 1
id: heliograph
domain: 01-heliograph
status: active
title: "TODO-01 -- Heliograph"
---

# TODO-01 -- Heliograph

> **Goal:** Heliograph does all that its approved design says: the store stays small, a silent taker is reported, every command can be read by a script, and a driver can use it as its queue.

> **Current state:** Measured 2026-11-24 at `438d434`. Messages, jobs with one taker, routing to the lane that owns a path, the Claude hook, overdue jobs, overlap warnings and the daily summary work and are tested in `tests/test_helio.py`; they get no section here. Missing: archiving (`engine/store.c` says "archive it" and there is no such command), expiry, sign-of-life reports, messages for one session, project privacy in `log` and `show`, fixed marks around inbox and log lines, a mark for landed work, an unowned path sent to its repository only, `--json`, `hello`, `summary`, and `take` without an id. The design's command list also differs from the program's. `tools/mail.py`, which the design says to remove, is in neither this repository, Impossible SDK nor the history of the local Impossible OS clone.

## Inputs

- `docs/specs/2026-11-23-heliograph-design.md`: the approved design; each item names the heading it comes from.
- `engine/store.c`: messages, steps and keys (§1, §2, §3, §4, §6, §8). `engine/watch.c`: overdue work and the summary (§3, §5). `engine/place.c`: sessions (§4, §5, §6). `engine/helio.c`: commands and output (all).
- `src/plat_linux.c`, `src/plat_windows.c`: the only files that call a system (§2).

## Outcome

- A closed job and a seen heads-up leave the working store by month, and can still be shown and answered.
- No job is taken twice and no keyed message is sent twice because of archiving, raced from Windows and WSL.
- A silent taker reaches the sender and the owner, and the job stays with its taker until someone releases it.
- Every command has a `--json` form, and `helio take --kind request --lane neon-a --json` takes one job.

**Adjacency:** list=applicable @ D01 T01 §7; document=not-applicable (messages are read in a terminal or by a script); settings=applicable @ D01 T01 §5; reporting=applicable @ D01 T01 §10; notifications=applicable @ D01 T01 §5; permissions=applicable @ D01 T01 §7; audit=applicable @ D01 T01 §3; exchange=applicable @ D01 T01 §10; reverse=applicable @ D01 T01 §5

**Adjacency rationale:** The log is the list and is kept inside a project by §7. The only new setting is how long silence lasts, read in §5. JSON (§10) is the report and the exchange form; nothing is imported. Archived steps stay readable in §3, which keeps the trail. The reverse of a take is `release`, which exists and which §5 proves still works after a silent report; archiving has no undo because an archived message still opens by its id.

## Implementation Order

| Order | Section | Deliverable | Depends On | Status |
| ----- | ------- | ----------- | ---------- | ------ |
| 1 | §1 | Steps that notice an archived job | -- | [ ] |
| 2 | §2 | Move a closed message to its month | §1 | [ ] |
| 3 | §3 | Read, answer and repeat archived messages | §2 | [ ] |
| 4 | §4 | Sessions announced by every call | -- | [ ] |
| 5 | §5 | Report of a silent taker | §4 | [ ] |
| 6 | §6 | Messages for one session | §5 | [ ] |
| 7 | §7 | Projects kept apart in log and show | -- | [ ] |
| 8 | §8 | Mark for landed work | §2 | [ ] |
| 9 | §9 | Fixed marks around every shown message | §7 | [ ] |
| 10 | §10 | JSON output on every command | §3, §5, §6, §8, §9 | [ ] |
| 11 | §11 | Take the next job as a queue | §10 | [ ] |
| 12 | §12 | Expiry of seen heads-ups and findings | §3, §4 | [ ] |
| 13 | §13 | Design and help that match the program | §11, §12, §14 | [ ] |
| 14 | §14 | Unowned paths kept to their repository | §4, §6 | [ ] |

## 1. Steps that notice an archived job

Archiving removes files that `store_job` reads one by one. A reader that holds the post while the steps go away would see an open job and take it again. The rule: archiving removes the post first, and whoever reads or makes a step looks for the post again afterwards.

**Phase:** 1
**Needs:** none
**Surface:** no

**Build order:**

1. Add the second look in `engine/store.c`. Done when: a chain whose post is gone is never reported open.
2. Cover it in `tests/test_helio.py`. Done when: the test fails with the second look removed.

- [ ] Look for the live post again at the end of the chain in `store_job` of `engine/store.c` (design, "How is nothing lost or done twice?"). Done when: a removed post gives "it was archived" and exit code 2, never open. Cheaper substitute: waiting a day before archiving.
- [ ] Look again after a step is made in `change_job` of `engine/store.c`, and remove the step this call made when the post is gone. Done when: no step without a post is left and the call is refused. Cheaper substitute: looking only before.
- [ ] Add `p_remove` to `engine/plat.h`, `src/plat_linux.c` and `src/plat_windows.c`, removing one file and flushing its folder. Done when: a missing file gives `P_MISSING` on both systems. Cheaper substitute: a removal that ignores failure.
- [ ] Pause a racer between reading and writing a step in `tests/racer.c`, remove the post and steps meanwhile, and let it go on. Done when: `tests/test_helio.py` sees a refusal and an empty folder, and fails with either second look removed. Cheaper substitute: a test without the pause.
- [ ] Commit: `store: steps notice an archived job`

**Test checkpoint:** `python3 ../ImpossibleSDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

## 2. Move a closed message to its month

A job done or refused for 30 days moves to `archive/<year-month>`. The month comes from the message's own id, so the place needs no index. `helio archive` does it, and the first inbox of the day does at most 200.

**Phase:** 1
**Needs:** none
**Surface:** no

**Build order:**

1. Write `engine/archive.c`. Done when: a closed job's files are in its month and gone from `messages`.
2. Cut it off at every point in `tests/test_helio.py`. Done when: a second run finishes it with nothing lost.

- [ ] Copy the post, steps and seen marks to `archive/<year-month>/` in `engine/archive.c`, each complete or not at all, before anything is removed (design, "How is nothing lost or done twice?"). Done when: the copies equal the originals byte for byte. Cheaper substitute: moving files one at a time.
- [ ] Remove the post, then the seen marks, then the steps from last to first in `engine/archive.c`. Done when: a kill at any moment leaves a store that `helio check` can tell from damage and the next run finishes. Cheaper substitute: any order.
- [ ] Refuse an open, taken or damaged job in `engine/archive.c`. Done when: none of the three is moved and a damaged one is named. Cheaper substitute: skipping silently.
- [ ] Refuse to replace a copy that differs in `engine/archive.c`. Done when: a planted different copy gives exit code 3 and both stay. Cheaper substitute: replacing it.
- [ ] Add `archive` to the commands in `engine/helio.c`, and 200 messages after the daily summary. Done when: two runs at once move each message once and both exit 0. Cheaper substitute: a lock file.
- [ ] Race archivers with takers and readers from Windows and WSL in `tests/test_helio.py`. Done when: no job has two takers and no message is missing in 1000 rounds. Cheaper substitute: one system.
- [ ] Commit: `archive: closed jobs move to their month`

**Test checkpoint:** `python3 ../ImpossibleSDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

## 3. Read, answer and repeat archived messages

An archived message must still be found by its id. Without this a keyed send repeated after archiving would make the message again, because `store_post` sees no post.

**Phase:** 1
**Needs:** none
**Surface:** no

**Build order:**

1. Let `engine/store.c` look in the month that the id names. Done when: `show` and `answer` work on an archived id.
2. Cover repeats in `tests/test_helio.py`. Done when: a repeated key makes nothing new.

- [ ] Read an archived message in `store_message` of `engine/store.c`, kept apart from the live look of §1. Done when: `helio show` gives the same text before and after archiving. Cheaper substitute: "there is no such message".
- [ ] Read an archived job's steps in `store_job` of `engine/store.c`. Done when: its line still names who closed it. Cheaper substitute: "closed" alone.
- [ ] Give the first id for a repeated key in `store_post` of `engine/store.c` when its message is archived. Done when: the repeat prints the first id and `messages` gains no file. Cheaper substitute: removing the key when archiving.
- [ ] Refuse take, release, done and refuse on an archived id in `engine/store.c`. Done when: each exits 2 and says it is archived. Cheaper substitute: exit code 3.
- [ ] Show archived messages with `log --all` in `engine/helio.c`, newest month first. Done when: a test of two months lists both in order. Cheaper substitute: the live store only.
- [ ] Examine archived files in `store_check` of `engine/store.c`. Done when: a byte changed in an archived file is reported. Cheaper substitute: trusting the archive.
- [ ] Commit: `archive: still found by its id`

**Test checkpoint:** `python3 ../ImpossibleSDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

## 4. Sessions announced by every call

Only the Claude hook announces a session, and it records a time alone. Codex and Antigravity never hook, so they look silent. Every call that names its lane becomes a sign of life, and `helio hello` records agent and repository.

**Phase:** 2
**Needs:** none
**Surface:** no

**Build order:**

1. Widen the file in `engine/place.c`. Done when: an old file with a time alone is still read.
2. Call it from `engine/helio.c`. Done when: each command changes the time.

- [ ] Record time, agent, repository and session in `place_hello` of `engine/place.c` (design, "How does it find the reader?"). Done when: each is read back and an old file still counts. Cheaper substitute: the time alone.
- [ ] Keep one file for each session of a lane in `engine/place.c`. Done when: two sessions of one lane do not overwrite each other. Cheaper substitute: one for the lane.
- [ ] Add `hello <agent>` to the commands in `engine/helio.c`, and use it in the hook. Done when: an unknown agent exits 2 and the three known ones are stored. Cheaper substitute: the hook only.
- [ ] Announce the lane on every other call in `engine/helio.c` without failing it. Done when: an unwritable `sessions` leaves the command's exit code unchanged and says why. Cheaper substitute: ignoring it.
- [ ] Commit: `sessions: every call is a sign of life`

**Test checkpoint:** `python3 ../ImpossibleSDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

## 5. Report of a silent taker

A job taken by a lane that has gone quiet is reported even when it has no due time. It is never handed on by itself.

**Phase:** 2
**Needs:** none
**Surface:** no

**Build order:**

1. Decide silence in `engine/watch.c`. Done when: it is a pure function of the sessions, the job and the clock.
2. Show it in `engine/helio.c`. Done when: the sender and the owner see it and others do not.

- [ ] Write `is_silent` in `engine/watch.c` from the taker's session, else its lane (design, "How is nothing lost or done twice?"). Done when: a table of times and sessions passes. Cheaper substitute: the age of the take.
- [ ] Read `silent: <minutes>` from `helio.conf` in `engine/place.c`, 120 when absent. Done when: a wrong value is refused with its line. Cheaper substitute: a fixed time.
- [ ] Return a silent job to its sender and the owner in `into_inbox` of `engine/helio.c`, marked SILENT. Done when: both see it, a stranger does not and the taker keeps it. Cheaper substitute: the owner alone.
- [ ] Add a Silent part to the summary in `engine/watch.c`. Done when: the count and the job are there. Cheaper substitute: counting it as Taken.
- [ ] Release a silent job and take it again in `tests/test_helio.py`. Done when: only `helio release` frees it. Cheaper substitute: a release by itself.
- [ ] Commit: `watch: a silent taker is reported`

**Test checkpoint:** `python3 ../ImpossibleSDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

## 6. Messages for one session

The design lets a message be for a session, not only a lane. `--for-session <name>` adds it to the reader.

**Phase:** 2
**Needs:** none
**Surface:** no

**Build order:**

1. Add the field in `engine/store.c`. Done when: a message without it is read as before.
2. Use it in `is_for` of `engine/store.c`. Done when: the other sessions of the lane do not see it.

- [ ] Write and read `for:` in `store_post` and `store_message` of `engine/store.c` (design, "What does a message hold?"). Done when: old messages pass `helio check` and a bad name is refused. Cheaper substitute: a name in the subject.
- [ ] Compare the session in `is_for` and `objection` of `engine/store.c`. Done when: another session of the lane neither sees nor takes it. Cheaper substitute: hiding it in the inbox alone.
- [ ] Mark it seen for a session in `store_seen` of `engine/store.c`. Done when: one session's look does not hide another's message. Cheaper substitute: one mark for the lane.
- [ ] Show it to the owner when the session is silent in `engine/helio.c`. Done when: it appears once for the owner after the time of §5. Cheaper substitute: it waits unseen.
- [ ] Commit: `messages: for one session`

**Test checkpoint:** `python3 ../ImpossibleSDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

## 7. Projects kept apart in log and show

The design says a session sees only its own project's messages and those sent to it by name. Today `log` prints every project and `show` opens any id.

**Phase:** 2
**Needs:** none
**Surface:** no

**Build order:**

1. Write one rule in `engine/store.c`. Done when: it decides from the message and the lane alone.
2. Use it in `engine/helio.c`. Done when: each command that prints a message passes through it.

- [ ] Write `may_see` in `engine/store.c`: the project of either side, or sent to this lane (design, "How does it find the reader?"). Done when: a table of both sides passes. Cheaper substitute: the receiving side alone.
- [ ] Keep to it in `log`, `show` and `answer` of `engine/helio.c`. Done when: a stranger's id exits 2 with the words of a missing one. Cheaper substitute: a warning.
- [ ] Show every project with `log --all` and no lane in `engine/helio.c`, for the owner's terminal. Done when: a call that names a lane stays inside its project. Cheaper substitute: no way to see all.
- [ ] Commit: `messages: projects stay apart`

**Test checkpoint:** `python3 ../ImpossibleSDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

## 8. Mark for landed work

A linked commit marks a job landed and closes nothing. The engine starts no program, so the caller gives the commit.

**Phase:** 2
**Needs:** none
**Surface:** no

**Build order:**

1. Write the mark in `engine/store.c`. Done when: it is made once and read back.
2. Show it in `engine/helio.c`. Done when: the state of the job is unchanged.

- [ ] Make `<id>.landed.<commit>` in `engine/store.c` for 7 to 64 hexadecimal digits (design, "Not goals"). Done when: a repeat changes nothing and other text exits 2. Cheaper substitute: a note on the job.
- [ ] Add `landed <id> <commit>` to the commands in `engine/helio.c` for the taker, the sender and the owner. Done when: a stranger is refused and the job stays open or taken. Cheaper substitute: closing the job.
- [ ] Show the commit in `line` of `engine/helio.c`, and examine the mark in `store_check` of `engine/store.c`. Done when: a damaged mark is reported. Cheaper substitute: `show` alone.
- [ ] Move the mark with its message in `engine/archive.c`. Done when: an archived job still names its commit. Cheaper substitute: dropping it.
- [ ] Commit: `jobs: a commit marks them landed`

**Test checkpoint:** `python3 ../ImpossibleSDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

## 9. Fixed marks around every shown message

`show` puts a bar before each line. The inbox, the log and the hook print a sender's subject with no closing mark.

**Phase:** 2
**Needs:** none
**Surface:** no

**Build order:**

1. Print the marks in `engine/helio.c`. Done when: each listing begins and ends with one.
2. Try to forge them in `tests/test_helio.py`. Done when: no subject or body can.

- [ ] Print one opening and one closing mark in `inbox`, `whole_log`, `show` and `hook` of `engine/helio.c` (design, "How does it reach the reader?"). Done when: each output has both, and an empty inbox prints nothing. Cheaper substitute: the opening mark alone.
- [ ] Start each line of a message with a bar in `engine/helio.c`. Done when: a subject that spells the closing mark stays behind its bar. Cheaper substitute: refusing such subjects.
- [ ] Count the marks in the 2,000 characters in `fill_inbox` of `engine/helio.c`. Done when: the largest hook output is 2,000 characters or fewer. Cheaper substitute: counting messages alone.
- [ ] Commit: `output: fixed marks around messages`

**Test checkpoint:** `python3 ../ImpossibleSDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

## 10. JSON output on every command

A script must not parse lines meant for people. `--json` prints one JSON value, and an error is one too. `summary` becomes a command that prints the day's counts.

**Phase:** 3
**Needs:** none
**Surface:** no

**Build order:**

1. Write `engine/json.c`. Done when: Python reads every output.
2. Use it in `engine/helio.c`. Done when: every command in the table has a JSON test.

- [ ] Escape text in `engine/json.c` and replace bytes that are not UTF-8. Done when: Python's `json` reads random bytes of 10000 messages. Cheaper substitute: copying bytes.
- [ ] Print inbox, log, show and check in `engine/helio.c` with every field of the message, its state and its taker (design, "Commands"). Done when: each field equals the stored one. Cheaper substitute: the line as a string.
- [ ] Print the id or the new state for send, take, release, done, refuse, landed, hello, working, archive and init in `engine/helio.c`. Done when: each is read in `tests/test_helio.py`. Cheaper substitute: no output.
- [ ] Add `summary` to the commands in `engine/helio.c`, as text and as JSON from `engine/watch.c`. Done when: its counts equal the file's. Cheaper substitute: the path of the file.
- [ ] Print an error as `{"error": ..., "code": ...}` in `fail` of `engine/helio.c`. Done when: exit codes are unchanged for 0, 1, 2 and 3. Cheaper substitute: text on an error.
- [ ] Commit: `output: JSON on every command`

**Test checkpoint:** `python3 ../ImpossibleSDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

## 11. Take the next job as a queue

The Gemini driver's queue is `helio take --kind request --lane neon-a --json`. It takes the most urgent, then oldest, open job of that kind.

**Phase:** 3
**Needs:** none
**Surface:** no

**Build order:**

1. Choose and take in `engine/helio.c`. Done when: a job someone else took first is passed over for the next.
2. Race it in `tests/test_helio.py`. Done when: each job has one taker.

- [ ] Read `--kind` and `--lane` in `parse` of `engine/helio.c` (design, "How does it reach the reader?"). Done when: a kind that is not a job exits 2 and `--lane` takes the project from `helio.conf`. Cheaper substitute: `--as` alone.
- [ ] Try jobs by urgency then age in `engine/helio.c` until one is taken. Done when: none left exits 1 and prints `null` with `--json`. Cheaper substitute: giving up at the first lost race.
- [ ] Race 8 drivers from Windows and WSL over 100 jobs in `tests/test_helio.py`. Done when: 100 takes, none twice. Cheaper substitute: one system.
- [ ] Declare the need `heliograph-queue` in `app.json`: the OS Neon driver uses `helio take` in place of its own queue folder (design, "Limits"). Done when: it is on main in the form `AGENTS.md` gives. Cheaper substitute: editing the OS repository.
- [ ] Commit: `jobs: take the next one`

**Test checkpoint:** `python3 ../ImpossibleSDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

## 12. Expiry of seen heads-ups and findings

Seen heads-ups, findings and answers stay in the working store for ever. They leave it 30 days after they were sent, once all who should see them have.

**Phase:** 3
**Needs:** none
**Surface:** no

**Build order:**

1. Decide expiry in `engine/archive.c`. Done when: it is a pure function of the message, its marks, the sessions and the clock.
2. Cover it in `tests/test_helio.py`. Done when: an unseen message never expires.

- [ ] Archive a seen message for one lane after 30 days in `engine/archive.c` (design, "Not goals"). Done when: the day before it stays and the day after it moves. Cheaper substitute: age alone.
- [ ] Keep a message for `project/all` in `engine/archive.c` until every lane with a sign of life in those 30 days has seen it. Done when: one unseeing live lane holds it. Cheaper substitute: the first reader ends it.
- [ ] Keep an unseen message in `engine/archive.c` and count it in the summary of `engine/watch.c` after 30 days. Done when: it is in the summary and still in the inbox. Cheaper substitute: deleting it.
- [ ] Commit: `archive: seen messages expire`

**Test checkpoint:** `python3 ../ImpossibleSDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

## 13. Design and help that match the program

The design lists `send` where the program has `hand` and `find`, and still lists removing `tools/mail.py` as a step. Help, `README.md` and design are made to say the same.

**Phase:** 3
**Needs:** none
**Surface:** no

**Build order:**

1. Correct `docs/specs/2026-11-23-heliograph-design.md` and `README.md`. Done when: each command they name runs.
2. Compare them in `tests/test_helio.py`. Done when: a command in one and not the other fails.

- [ ] Correct the command list and the finished steps in `docs/specs/2026-11-23-heliograph-design.md`, with the date. Done when: no command or file it names is missing. Cheaper substitute: a note that it is old.
- [ ] State the new commands, times and limits in `README.md`, and the archive folder. Done when: the Status line is true of the program. Cheaper substitute: the help alone.
- [ ] Compare the commands in the help of `engine/helio.c`, `README.md` and the design in `tests/test_helio.py`. Done when: a planted extra command fails it. Cheaper substitute: reading them.
- [ ] Commit: `docs: the design matches the program`

**Test checkpoint:** `python3 ../ImpossibleSDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

## 14. Unowned paths kept to their repository

A path that no lane owns goes to whoever works in that repository, and to the owner when nobody is there (design, "How does it find the reader?"). Today `reader` in `engine/helio.c` sends it to the whole project when any lane of the project was seen in the last half hour, and a test asks for that.

**Phase:** 2
**Needs:** none
**Surface:** no

**Build order:**

1. Store the repository as the reader in `engine/store.c`. Done when: a message without it is read as before.
2. Send and show it in `engine/helio.c`. Done when: the three cases of the design are tested.

- [ ] Read `repository: <name>` from `helio.conf` in `place_open` of `engine/place.c`: the name is the identity, so Windows and WSL agree on one checkout. Done when: both spell it alike, two repositories of one project differ, a path is refused, and without the line an unowned path goes to the project as before. Cheaper substitute: the name of the folder.
- [ ] Write and read `repository:` in `store_post` and `store_message` of `engine/store.c`, beside the reader of §6. Done when: old messages pass `helio check`. Cheaper substitute: one message for each lane.
- [ ] Write the actor's repository into each step in `add_link` and read it in `store_job` of `engine/store.c`, as the session is. Done when: `helio check` accepts a take after its session has ended, a step without it still counts for a message without a repository, and one that names another repository is damage. Cheaper substitute: checking the live actor alone.
- [ ] Give the repository from the sessions of §4 in `place_busy` of `engine/place.c`, in place of one yes or no for the project. Done when: a lane seen only in another repository of the project does not count. Cheaper substitute: the project.
- [ ] Choose the reader in `reader` of `engine/helio.c`: the lane that owns the path, else this repository, else the owner. Done when: each of the three is chosen in a test. Cheaper substitute: the owner whenever no lane owns it.
- [ ] Compare the actor's repository in `is_for` and `objection` of `engine/store.c`. Done when: two lanes of the repository both see the one job and one of them takes it, and a lane of another repository neither sees nor takes it. Cheaper substitute: hiding it in the inbox alone.
- [ ] Replace `test_an_unowned_path_goes_to_whoever_works_there` in `tests/test_helio.py` with the three cases, its lane announced from inside the repository. Done when: it fails with the old `reader`. Cheaper substitute: keeping the project-wide expectation.
- [ ] Commit: `routing: an unowned path stays in its repository`

**Test checkpoint:** `python3 ../ImpossibleSDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

## Verification

- A job is sent on Windows, taken in WSL with `helio take --kind request --lane neon-a --json`, marked landed, done, archived and shown again by its id.
- `python3 ../ImpossibleSDK/tools/prove.py .` exits 0 after the last commit.
- `python3 scripts/todo-graph.py validate` and `python3 scripts/todo-graph.py plan --check` pass.
