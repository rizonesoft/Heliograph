---
schema_version: 1
id: heliograph
domain: 01-heliograph
status: active
title: "TODO-01 -- Heliograph"
---

# TODO-01 -- Heliograph

> **Goal:** Heliograph does all that its approved design says: the store stays small, a silent taker is reported, every command can be read by a script, and a driver can use it as its queue.

> **Current state:** Measured 2026-11-24 at `438d434`. Messages, jobs with one taker, routing to the lane that owns a path, the Claude hook, overdue jobs, overlap warnings and the daily summary work and are tested in `tests/test_helio.py`; they get no section here. Missing: archiving (`engine/store.c` says "archive it" and there is no such command), expiry, sign-of-life reports, messages for one session, project privacy in `log` and `show`, fixed marks around inbox and log lines, a mark for landed work, an unowned path sent to its repository only, `--json`, `hello`, `summary`, and `take` without an id. There is no installer: the two programs in `~/ImpossibleOS/bin`, `helio.cmd` there and the wrapper in `~/.local/bin` were copied by hand, no folder with `helio` is on Windows' `PATH`, and nothing records which commit is installed. The design's command list also differs from the program's. `tools/mail.py`, which the design says to remove, is in neither this repository, Impossible SDK nor the history of the local Impossible OS clone.

## Inputs

- `docs/specs/2026-11-23-heliograph-design.md`: the approved design; each item names the heading it comes from.
- `engine/store.c`: messages, steps and keys (§1, §2, §3, §4, §6, §8). `engine/watch.c`: overdue work and the summary (§3, §5). `engine/place.c`: sessions (§4, §5, §6). `engine/helio.c`: commands and output (all).
- Impossible SDK `tools/app_build.py` and `tools/prove.py`: what builds and proves a commit (§15, §17). Its `08-release` file plans packages for released programs; §15 and §16 install from a checkout and end when such a package replaces them.
- `src/plat_linux.c`, `src/plat_windows.c`: the only files that call a system (§2).

## Outcome

- A closed job and a seen heads-up leave the working store by month, and can still be shown and answered.
- No job is taken twice and no keyed message is sent twice because of archiving, raced from Windows and WSL.
- A silent taker reaches the sender and the owner, and the job stays with its taker until someone releases it.
- One command builds, tests and installs Heliograph, and one removes it; after each phase the installed program is the tested one, and the one before it can be put back.
- Every command has a `--json` form, and `helio take --kind request --lane neon-a --json` takes one job.

**Adjacency:** list=applicable @ D01 T01 §7; document=not-applicable (messages are read in a terminal or by a script); settings=applicable @ D01 T01 §5; reporting=applicable @ D01 T01 §10; notifications=applicable @ D01 T01 §5; permissions=applicable @ D01 T01 §7; audit=applicable @ D01 T01 §3; exchange=applicable @ D01 T01 §10; reverse=applicable @ D01 T01 §5

**Adjacency rationale:** The log is the list and is kept inside a project by §7. The only new setting is how long silence lasts, read in §5. JSON (§10) is the report and the exchange form; nothing is imported. Archived steps stay readable in §3, which keeps the trail. The installer keeps the program before it and can remove everything it made (§15). The reverse of a take is `release`, which exists and which §5 proves still works after a silent report; archiving has no undo because an archived message still opens by its id.

## Implementation Order

| Order | Section | Deliverable | Depends On | Status |
| ----- | ------- | ----------- | ---------- | ------ |
| 1 | §1 | Steps that notice an archived job | -- | [x] |
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
| 15 | §15 | Quick installer | -- | [x] |
| 16 | §16 | Installer for Windows sessions | §15 | [x] |
| 17 | §17 | Installed program replaced after a tested phase | §15, §16 | [x] |

## 1. Steps that notice an archived job

> **Started:** 2026-11-24T19:09Z

**Corrected 2026-11-24:** Incorporated all seven Plan review findings (`docs/reviews/01-heliograph/D01-T01-s01.md`): (1) Build `p_remove` first (before `change_job`), implementing durable file removal + parent-folder flush in `src/plat_linux.c` (`unlink` + `settle` via `fsync(dir_fd)`, tolerating `EINVAL`/`ENOTSUP`) and `src/plat_windows.c` (`DeleteFileW` with transient sharing retry + parent-folder `CreateFileW(FILE_FLAG_BACKUP_SEMANTICS)`/`FlushFileBuffers`, tolerating `ERROR_ACCESS_DENIED`/`ERROR_INVALID_FUNCTION`/`ERROR_NOT_SUPPORTED`/`ERROR_INVALID_PARAMETER`, returning `P_MISSING` on `ERROR_FILE_NOT_FOUND`/`ERROR_PATH_NOT_FOUND` and `P_FAILED` on other errors); exercise both Linux and Windows binaries on existing-file deletion, neighbor preservation, `P_MISSING` on repeat, and observable `P_FAILED` on flush/directory failure. (2) In `store_job`, check the live `<id>.post` via a fresh `live_post` read BEFORE `gap()` decides missing-step damage AND at the full-chain (`MOST_LINKS`) return; distinguish proven `P_MISSING` (`S_REFUSED`, `"it was archived"`, exit 2) from read/IO errors (`P_FAILED`/`P_TOO_LARGE` -> `S_DAMAGED`, exit 3), testing with a populated `store_open` snapshot plus a control where the live post exists and a genuine link gap still exits 3. (3) In `change_job`, remove the newly created step ONLY when THIS call created it (`make` returned `S_OK`, never on `P_EXISTS` collision) AND `live_post` returned proven `P_MISSING` (`S_REFUSED`); treat `p_remove` returning `P_OK` or `P_MISSING` as clean (`S_REFUSED`, `"it was archived"`), and surface any `live_post` IO error or `p_remove` `P_FAILED` as `S_DAMAGED` (exit 3) without deleting a live step; crash-after-link-publication orphan sweep using `archive/<year-month>/<id>.post` is explicitly owned by `§2`. (4) Preserve the distinct `S_REFUSED` (`"it was archived"`) result through all callers (`engine/helio.c`: `show`, `into_inbox`, `into_log`, `line`, hook; `engine/watch.c`: `watch_summary`): direct `show` exits 2 with `"it was archived"` and skips `.seen.*`, while live listings/hooks/summary skip concurrently archived messages without printing `"damaged"`, exiting 3, or counting damage. (5) Apply the same post-write second-look + `p_remove` rollback to `store_seen` so a recipient racing archive removal never leaves an orphan `.seen.*` file. (6) Add two independently synchronized pause points in `tests/racer.c` (using a pure C callback on `struct store` that is `NULL` in production): one before `store_job`'s second look (verifying `S_REFUSED` `"it was archived"` with populated `store_open`), and one between `store_job` and `add_link` plus verification that the link was created and then removed (leaving `messages/` empty), plus a paused `store_seen` race test and exact result/reason checks so reverting either guard fails its corresponding test.

Archiving removes files that `store_job` reads one by one. A reader that holds the post while the steps go away would see an open job and take it again. The rule: archiving removes the post first, and whoever reads or makes a step looks for the post again afterwards.

**Phase:** 1
**Needs:** none
**Surface:** no

**Build order:**

1. Add `p_remove` to `engine/plat.h`, `src/plat_linux.c`, and `src/plat_windows.c`. Done when: both platform binaries remove an existing file, preserve a neighbor, return `P_MISSING` on repeat, and return `P_FAILED` when delete or required flush fails.
2. Add the live-post second look and rollback guards to `store_job`, `change_job`, and `store_seen` in `engine/store.c`, and preserve `S_REFUSED` (`"it was archived"`) through callers in `engine/helio.c` and `engine/watch.c`. Done when: a removed post never reads open or damaged, no orphan link or seen mark survives, and IO/flush errors surface as exit 3.
3. Cover both independent pause points, populated `store_open` snapshots, seen-mark races, error/collision preservation, and `p_remove` in `tests/racer.c` and `tests/test_helio.py`. Done when: each test fails when its corresponding guard is reverted.

- [x] Look for the live post again at the end of the chain in `store_job` of `engine/store.c` (design, "How is nothing lost or done twice?"). Done when: a removed post gives "it was archived" and exit code 2, never open. Cheaper substitute: waiting a day before archiving.
- [x] Look again after a step is made in `change_job` of `engine/store.c`, and remove the step this call made when the post is gone. Done when: no step without a post is left and the call is refused. Cheaper substitute: looking only before.
- [x] Add `p_remove` to `engine/plat.h`, `src/plat_linux.c` and `src/plat_windows.c`, removing one file and flushing its folder. Done when: a missing file gives `P_MISSING` on both systems. Cheaper substitute: a removal that ignores failure.
- [x] Pause a racer between reading and writing a step in `tests/racer.c`, remove the post and steps meanwhile, and let it go on. Done when: `tests/test_helio.py` sees a refusal and an empty folder, and fails with either second look removed. Cheaper substitute: a test without the pause.
- [x] Commit: `store: steps notice an archived job`

**Test checkpoint:** `python3 ../ImpossibleSDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

> **Verified:** 2026-11-24 | §1 | b671912bc78b1d68e4a800a14d0fc1615cc5c4b1
> **Review:** `docs/reviews/d01-t01-s1.json`

## 2. Move a closed message to its month

> **Started:** 2026-11-24T19:55Z

**Corrected 2026-11-24:** Incorporated `§1` plan review finding 2 and all ten `§2` Plan review findings (`docs/reviews/01-heliograph/D01-T01-s2.md`): (1) Late `.seen.*` preservation: during orphan sweep, copy any valid live `<id>.seen.*` (`"seen\n"`) to `archive/<year-month>/` (durable write + byte-compare on `P_EXISTS`) BEFORE removing the live mark so a `show` racing post removal never loses its seen mark. (2) Safe recovery invariant: orphan cleanup runs ONLY when live `messages/<id>.post` is proven `P_MISSING` AND `archive/<year-month>/<id>.post` + its archived chain validate to a terminal state (`DONE` or `REFUSED`); while live `.post` exists, resume copying and preserve all live files on any conflict; `store_check` distinguishes an interrupted archive (validated archived terminal chain) from genuine damage (missing step or unarchived orphan exits 3). (3) Durable directory creation and copy publication: flush parent and new directories in `p_folder` on Linux and Windows, add `p_sync` to flush an existing identical archive copy before deleting any live source (closing the `P_UNFLUSHED` retry window), and never remove live sources on write/flush error (exit 3). (4) Keyed-send safety before activation: `store_post` checks `archive/<year-month>/<id>.post` on keyed retries so retrying an archived keyed message returns the original ID without creating a new live post or allowing a second take (tested across post removal and after cleanup). (5) Eligibility contract: only jobs (`request`/`handoff`) in `DONE` or `REFUSED` with `now.when <= now` and `now - now.when >= 30 days` move, to `archive/<YYYY-MM>/` derived from the ID's creation timestamp; non-job messages stay live pending `§12`; tested at `30d-1s`, `30d`, future `when`, and cross-month creation vs closure. (6) Daily 200-message auto-archive trigger: first `inbox` of the day after `watch_summary` checks `summary/<YYYY-MM-DD>.archived`, archives up to 200 eligible messages, and writes `summary/<YYYY-MM-DD>.archived` on completion so hook-first, simultaneous inboxes, interruption resume, same-day skip, next-day re-trigger, and explicit `helio archive` drain all work deterministically. (7) Overfull-store recovery: `helio archive` enumerates `messages/` in bounded batches without tripping `MOST_NAMES` in `store_open`, draining an overfull store back to normal access while preserving ineligible jobs and `key.*` reservations. (8) Manual `helio archive` CLI contract: runs without `--as`, refuses extra args with exit 2, archives healthy eligible neighbors in mixed batches while naming damaged IDs and exiting 3 on any damage/conflict/IO error, exits 1 when zero eligible messages exist, and exits 0 when at least one eligible message was archived or moved by a concurrent peer. (9) Cross-host race test: runs on WSL against Windows without skipping, racing real archivers, takers, and readers across 1000 rounds and verifying every eligible closed job moved with its complete terminal chain while live controls had zero duplicate takes and zero missing messages. (10) Build order: add `engine/archive.h` and `engine/archive.c` to `CMakeLists.txt` and wire `helio archive` before running interruption and race tests through the production path.

A job done or refused for 30 days moves to `archive/<year-month>`. The month comes from the message's own id, so the place needs no index. `helio archive` does it, and the first inbox of the day does at most 200.

**Phase:** 1
**Needs:** none
**Surface:** no

**Build order:**

1. Add `p_sync` and durable `p_folder` flush to `engine/plat.h`, `src/plat_linux.c`, and `src/plat_windows.c`; write `engine/archive.h` and `engine/archive.c`, add them to `CMakeLists.txt`, wire `store_post` archived-key lookup and `store_check` interrupted-archive recognition in `engine/store.c`, and wire `helio archive` plus the daily 200-message inbox trigger in `engine/helio.c`. Done when: a closed job's files move durably to its month and are gone from `messages`, keyed retries are safe, and overfull stores drain.
2. Cut it off at every point (checking `helio check` before retry), test late `.seen.*` preservation, eligibility boundaries, daily 200-message trigger, conflicts, IO/flush errors, overfull recovery, and race Windows + WSL archivers, takers, and readers across 1000 rounds in `tests/test_helio.py`. Done when: a second run finishes every cut cleanly with nothing lost, zero duplicate takes, and zero missing messages.

- [x] Copy the post, steps and seen marks to `archive/<year-month>/` in `engine/archive.c`, each complete or not at all, before anything is removed (design, "How is nothing lost or done twice?"). Done when: the copies equal the originals byte for byte. Cheaper substitute: moving files one at a time.
- [x] Remove the post, then the seen marks, then the steps from last to first in `engine/archive.c`. Done when: a kill at any moment leaves a store that `helio check` can tell from damage and the next run finishes. Cheaper substitute: any order.
- [x] Refuse an open, taken or damaged job in `engine/archive.c`. Done when: none of the three is moved and a damaged one is named. Cheaper substitute: skipping silently.
- [x] Refuse to replace a copy that differs in `engine/archive.c`. Done when: a planted different copy gives exit code 3 and both stay. Cheaper substitute: replacing it.
- [x] Add `archive` to the commands in `engine/helio.c`, and 200 messages after the daily summary. Done when: two runs at once move each message once and both exit 0. Cheaper substitute: a lock file.
- [x] Race archivers with takers and readers from Windows and WSL in `tests/test_helio.py`. Done when: no job has two takers and no message is missing in 1000 rounds. Cheaper substitute: one system.
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

- [ ] Correct the command list, the finished steps, and the obsolete `mail` folder migration line (`docs/specs/2026-11-23-heliograph-design.md:32`) in `docs/specs/2026-11-23-heliograph-design.md`, with the date. Done when: no command or file it names is missing. Cheaper substitute: a note that it is old.
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

## 15. Quick installer

> **Started:** 2026-11-24T00:49Z

**Corrected 2026-11-24:** re-planned with `todo-graph.py replan`; the build first began at 2026-11-23T23:17:30Z; its review chain is kept as `docs/reviews/01-heliograph/D01-T01-s15-chain1.md`. Clarified the journal schema contract: `tools/install.py` is first introduced in this section and writes `schema_version: 2` journals with explicit `pre_wrappers` for `install` and `--back` (while accepting `schema_version` 1 or 2 for `staging` and `--remove`), refusing unrecognized/incomplete `schema_version: 1` install/back journals with exit code 2 rather than attempting impossible migration of unshipped pre-release draft journals that never persisted overwritten wrapper stores.

One command takes a checkout to a working `helio` in WSL. It records what it installed, so that §17 can compare and go back.

**Corrected 2026-11-24:** Added the external-file safety contract (exclusive lock, durable in-progress journal, ownership check before replacing or deleting external files, journal removal rule, and failure/stop recovery) before plan review because `tools/install.py` writes and removes files outside the repository. Updated after plan review (`docs/reviews/01-heliograph/D01-T01-s15-chain1.md`): added legacy-install adoption and reserved-name collision refusal, destination-bound wrapper ownership with a serialized wrapper lock, zero-gap `os.replace` with defer-on-busy instead of rename-away-first (which created an availability gap), full-set crash recovery across install/back/remove, previous-generation provenance and hash verification on `--back`, explicit `Heliograph-signals` default store and override precedence with spaced-path shell tests, nonrecursive fixture-checkout test boundary with missing-SDK clone coverage, dirty-tree tracking in `installed.json`, live-binary verification and exit codes in `--status` plus idempotent `--remove`, and routed the obsolete `mail` folder design note to `D01 T01 §13`. Updated after chain-2 plan review (`docs/reviews/01-heliograph/D01-T01-s15.md`): added refusal of cross-destination wrapper takeover, exact wrapper content and `previous` backup hash verification in `--status`, build-time source-drift refusal, clean-home login-shell `PATH` test, nonrecursive real-checkout pinned-SDK install test, non-skipping WSL Windows-interop proof, partial legacy-pair refusal, retirement of outgoing third wrappers on successive `--wrapper` changes, and structured `OSError` / pending-recovery CLI diagnostics.

**Phase:** 0
**Needs:** none
**Surface:** no

**Build order:**

1. Write `tools/install.py`. Done when: a clean home has a working `helio` after one command.
2. Cover it in `tests/test_install.py`. Done when: a home in a temporary folder is installed, replaced, put back and removed, with crash, lock, ownership and store-override checks.

- [x] Find Impossible SDK beside the checkout in `tools/install.py`, or clone it there (`HELIO_SDK_URL` override for tests), then build and test with its `tools/app_build.py`. Done when: an isolated fixture-checkout test proves both a missing-SDK clone and a real build/install, and a failed test installs nothing and leaves an existing installation intact without recursing through SDK test discovery. Cheaper substitute: `--no-test`.
- [x] Guard external writes in `tools/install.py` with ordered exclusive locks (`<to>/.install.lock` then `<wrapper_dir>/.helio-install.lock`), an atomically written durable journal (`<to>/install.journal.json`, fsynced with its parent directories) removed only after `installed.json` commits (or `--remove` completes), and full-set recovery on failure, stop, or retry. Done when: concurrent runs to the same or different destinations serialize safely, and interrupting install, `--back`, or `--remove` at any publication boundary recovers both binaries, rollback pair, wrapper target, manifest and store to a consistent hash-verified state. Cheaper substitute: writing without locks or a journal.
- [x] Verify ownership of every reserved path (`helio`, `helio.exe`, `helio.old`, `helio.exe.old`, `installed.json`, `install.journal.json`) before mutating `<to>` in `tools/install.py`. Done when: an existing legacy manual installation with no `installed.json` is adopted with `previous.commit = "legacy"`, and any unrelated file or symlink at any reserved name is refused with exit code 2 before changing anything. Cheaper substitute: overwriting existing files blindly.
- [x] Publish `helio` and `helio.exe` to the program folder in `tools/install.py` (`--to`, `bin` beside the checkout when absent) via temp-file, fsync, atomic `os.replace` and directory fsync, deferring with rollback and exit code 3 if the host refuses `os.replace` on an in-use executable. Done when: a call made during replacement gets the old or the new program, never none, verified with a real held Windows `helio.exe` and a second caller. Cheaper substitute: copying over or renaming away first.
- [x] Keep the previous pair as `helio.old` and `helio.exe.old`, and write `installed.json` with `current` and `previous` provenance (`commit`, `dirty`, `installed_at`, `helio_sha256`, `helio_exe_sha256`, `store`, `wrapper`) in `tools/install.py`. Done when: both live and previous hashes and dirty working-tree state are recorded and read back, and `--status` never reports a dirty build as clean `HEAD`. Cheaper substitute: a printed line or a single commit without dirty/previous metadata.
- [x] Make the default store (`Heliograph-signals` beside the checkout, or `--store`) when absent and install a destination-bound wrapper in `~/.local/bin/helio` (carrying `# Managed by Heliograph installer` and `# target=<to>`) executing the native WSL binary with `--store` > `HELIO_STORE` > default store precedence in `tools/install.py`. Done when: an existing store keeps every byte, an unowned file or symlink at the wrapper path is refused and named, and a fresh shell test with spaces in paths forwards arguments and exit status to each selected store. Cheaper substitute: replacing any wrapper or ignoring overrides.
- [x] Add `--status`, `--back` and `--remove` to `tools/install.py`. Done when: `--status` hashes both live binaries and reports exit 0 (healthy), 1 (absent), or 3 (partial/mismatched, naming each file); `--back` verifies both `previous` backups against recorded hashes before swapping and updating `installed.json` (refusing first-install or corrupt backups with live files untouched); and `--remove` is idempotent, deletes only installer-owned files, removes the wrapper only if `# target=` matches `<to>`, and leaves the store and unrelated files untouched. Cheaper substitute: deleting by hand or trusting `installed.json` without hashing live files.
- [x] Put the one install command plus `--status`, `--back`, `--remove` and `--to` usage in the Quick start of `README.md`. Done when: it is the command `tests/test_install.py` runs. Cheaper substitute: the steps as they are.
- [x] Harden edge cases from chain-2 plan review in `tools/install.py` and `tests/test_install.py`: refuse cross-destination wrapper takeover; verify exact wrapper content/store and `previous` backup hashes in `--status`; refuse publication on build-time source drift; refuse partial legacy binary pairs naming the missing partner; retire outgoing third wrappers on successive `--wrapper` changes; report structured exit-3 diagnostics for `OSError` and failed recovery; and add clean-home login-shell `PATH`, nonrecursive real-checkout pinned-SDK install, and non-skipping WSL Windows-interop tests. Done when: every new regression passes and fails when its production guard is reverted. Cheaper substitute: skipping real-SDK or partial-pair checks.
- [x] Commit: `install: one command`

**Test checkpoint:** `python3 ../ImpossibleSDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

> **Verified:** 2026-11-24 | §15 | 5b154710504fe1b194428f4d0a9cb65c466571a7
> **Review:** `docs/reviews/d01-t01-s15.json`

## 16. Installer for Windows sessions

> **Started:** 2026-11-24T03:00Z

**Corrected 2026-11-24:** re-planned with `todo-graph.py replan` (Chain 2); the build first began at 2026-11-24T01:48Z; its prior review chain is kept as `docs/reviews/01-heliograph/D01-T01-s16-chain1.md`. Chain 2 fixes the final two defects (using `HELIO_DEFAULT_STORE` in `engine/helio.c` so the offline-WSL diagnostic applies only to the installed default store with zero batch argv parsing, and pre-recording retired digests in the journal before recovery renames) and drops unshipped pre-stamp draft `list[str]` compatibility so `retired` is always a `dict[str, str]` mapping filename to verified SHA-256 digest and `helio_cmd_text` is persisted for rollback.

A Windows session has no `helio` on its `PATH`, so its hook finds nothing. The installer gives it one that uses the store in WSL. It is worked on in WSL: the Windows half runs through Windows' own Python, as `tools/prove.py` of Impossible SDK does.

**Corrected 2026-11-24:** Added the external-file and Windows-registry safety contract before plan review because `tools/install.py --windows` mutates `%LOCALAPPDATA%\Heliograph` and `HKCU\Environment\Path`. Updated after Chain 1 plan review (`docs/reviews/01-heliograph/D01-T01-s16-chain1.md`) and Chain 2 plan review (`docs/reviews/01-heliograph/D01-T01-s16.md`): place `helio.cmd` and companion Git Bash shim `helio` in `<win_to>` (on `PATH`) and `helio.exe` in `<win_to>\bin\helio.exe` (not on `PATH`) so bare `helio` never bypasses `helio.cmd` via `PATHEXT` and `.claude/settings.json` hooks work in Git Bash; persist `path_added` and surgical `Path` ownership preferring the exact recorded literal entry over user-added `%...%` spellings; detect and report when an earlier `helio` on `PATH` shadows `<win_to>\helio.cmd` in both install and status; verify ownership of every reserved path (`helio.cmd`, `helio`, `installed.win.json`, `install.win.journal.json`, `bin\helio.exe`, `bin\helio.exe.old`, tracked `.retired` copies); guard with ordered external destination and registry locks (`%LOCALAPPDATA%\Heliograph-locks\.dest-<hash>.lock`, `.reg-<hash>.lock`) and durable `install.win.journal.json` using distinct retirement suffixes for publication (`.retired`) vs recovery (`.rec.retired`); initialize new Windows `--store` paths during install before publishing; bind WSL `--windows` handoff to the tested `app_build.py` snapshot by SHA-256; support `--windows --status`, `--windows --back` (exercised through CLI), and `--windows --remove`; implement `--store` > `HELIO_STORE` > `HELIO_DEFAULT_STORE` precedence in `engine/helio.c` with unreachable-WSL diagnostic only when the installed default store is selected; add Windows-native `TestWindowsInstaller` (executed by `prove.py`), WSL handoff tests (including no-override install, `--back` CLI, and real `.claude/settings.json` hook execution in Git Bash), and document Windows install/status/back/remove in `README.md`.

**Phase:** 0
**Needs:** none
**Surface:** no

**Build order:**

1. Write the Windows half of `tools/install.py` (WSL handoff + Windows-native install/status/back/remove). Done when: bare `helio check` works in a fresh Windows terminal.
2. Cover it in `tests/test_install.py` (`TestWindowsInstaller` on Windows + WSL handoff in `TestQuickInstaller`) with isolated temp folders and an isolated registry key (`--reg-key`). Done when: it runs on Windows through `tools/prove.py`.

- [x] Build and snapshot `helio.exe` in WSL via `ensure_sdk_and_build` and hand its path, SHA-256, provenance and `\\wsl.localhost\<distro>` store path to Windows Python (`tools/install.py --windows`), installing `helio.exe` to `%LOCALAPPDATA%\Heliograph\bin\helio.exe` with `bin\helio.exe.old` rollback backup, lock + durable journal + ownership checks, renaming a busy live `helio.exe` to a tracked `.retired` file swept on the next install/remove (or deferring with exit 3 if locked). Done when: a held running `helio.exe` is replaced cleanly, the next install removes the retired copy, a failed build leaves the live install untouched, and interrupted install/back/remove recovers cleanly. Cheaper substitute: running from WSL's disk or skipping hash/ownership checks.
- [x] Write `%LOCALAPPDATA%\Heliograph\helio.cmd` (keeping `bin\helio.exe` off `PATH`) with `--store` > `HELIO_STORE` > default `\\wsl.localhost\<distro>` store precedence, argument/stdin/exit-status forwarding, and exit 3 naming WSL/distro when the selected default WSL store cannot be reached (without blocking a reachable `--store` or `HELIO_STORE` override). Done when: bare `helio` from `cmd.exe` and `powershell.exe` outside the install folder reads a message sent in WSL, respects overrides and spaces, and reports exit 3 for an unreachable default store without creating an alternate store. Cheaper substitute: placing `helio.exe` beside `helio.cmd` on `PATH` or an empty inbox.
- [x] Add `<win_to>` to the user's `Path` once in `tools/install.py` (under a shared registry lock), preserving every other entry and its registry kind (`REG_SZ`/`REG_EXPAND_SZ`, leaving `%...%` unexpanded), persisting `path_added`, and broadcasting `WM_SETTINGCHANGE`. Done when: a second install changes nothing and skips broadcast, a pre-existing equivalent entry sets `path_added=false`, and an entry with `%` stays unexpanded. Cheaper substitute: `setx`, which cuts a long `PATH`.
- [x] Support `--windows --status`, `--windows --back`, and `--windows --remove` in `tools/install.py`, undoing only the installer-owned `Path` entry on `--remove` (preserving intervening edits) and removing `<win_to>` only when empty of unrelated files. Done when: `--status` verifies live/backup hashes, `helio.cmd` bytes, `Path` and store; `--back` restores the previous binary/store and refuses corrupt/missing backups; and `--remove` restores `Path` surgically and removes owned files while preserving unrelated files. Cheaper substitute: leaving `Path` or deleting blindly.
- [x] Document the Windows install, `--status`, `--back`, and `--remove` commands in `README.md` Quick start and cover them in `TestWindowsInstaller` (executed by `prove.py`) plus WSL handoff tests in `tests/test_install.py`. Done when: `prove.py` executes all Windows tests natively with zero skips on the new class and `app_build.py` passes in WSL. Cheaper substitute: skipping Windows tests or omitting docs.
- [x] Commit: `install: Windows sessions`

**Test checkpoint:** `python3 ../ImpossibleSDK/tools/prove.py .` exits 0 after the commit and the new tests fail when their production change is reverted.

> **Verified:** 2026-11-24 | §16 | 7a376e422268ee520a6afa025688585d6350bd5d
> **Review:** `docs/reviews/d01-t01-s16.json`

## 17. Installed program replaced after a tested phase

> **Started:** 2026-11-24T13:44Z

**Corrected 2026-11-24:** re-planned with `todo-graph.py replan` (Chain 2); the build first began at 2026-11-24T04:31Z; its review chain is kept as `docs/reviews/01-heliograph/D01-T01-s17-chain1.md`. Clarified the shipped `schema_version: 2` phase-journal contract and incorporated all Chain 2 plan-review findings (`docs/reviews/01-heliograph/D01-T01-s17.md`): `tools/phase_install.py` is first introduced in this section, so `phase.journal.json` accepts only `schema_version: 2` with `managed_by: "heliograph-phase-installer"` and all required bound fields (`tx_id`, `proved_wsl_sha`, `proved_win_sha`, `win_store`, paths, booleans), refusing any unshipped pre-stamp draft (`schema_version: 1`) or missing-`win_store` / foreign / incomplete record with exit code 2 (leaving journal, marker, and both installations untouched). Every Windows publication and rollback requires an active `.phase-active.json` lock-token marker matching `tx_id` (enforced atomically inside Windows' destination lock via `--phase-tx` with `require_active=True`); terminal states (`notifying` on success and `rolled_back` on rollback) are persisted durably before releasing `.phase-active.json`; repeated retries against a missing-marker refusal consistently refuse with exit 3 and preserve `phase.journal.json` without retiring it; failed-phase rollback (`A -> B -> failed C`) restores BOTH `current=B` AND `previous=A` (binaries + manifest `previous`) on both WSL and Windows so a subsequent ordinary `--back` still restores `A`; recovery checks healthy Windows status (`0` existing, `1` absent) before treating Windows as restored; remote `origin/main` verification uses a single atomic `git ls-remote origin refs/heads/main` call; `process-plan/SKILL.md` (all three copies) and `docs/unattended-prompt.md` check for closed phases missing their `Installed commit:` record before advancing or declaring `plan-done` (with restart tests covering both pre-install and post-install/pre-receipt crashes); `tests/test_install.py` adds a nonrecursive fixture run through the real pinned SDK `app_build.py` and `prove.py` verifying installed digests against the captured pair; and backward store compatibility compiles and exercises genuine historical pre-Phase-0 binaries for BOTH WSL (`helio`) and Windows (`helio.exe`), verifying original message IDs, completed job state, and keyed-send deduplication through the restored Windows entry point.

When a phase is closed and everything is tested, the program that the sessions use becomes the tested one, without anybody asking. It is never replaced by anything else.

**Corrected 2026-11-24:** Added the external-file safety and rollback contract before plan review, and updated after Chain 1 plan review (`docs/reviews/01-heliograph/D01-T01-s17-chain1.md`): declare `§16` dependency and `wsl-windows-interop` Need (with probe in `todo/plan.json`) and refuse unavailable interop before any mutation; define `--win-to` (default `%LOCALAPPDATA%\Heliograph`, accepting Windows paths including spaces, combined with WSL `--to` in `--phase` mode and refused with incompatible modes); require a recognized non-empty phase with every section verified in `todo-graph.py query plan` AND a committed `## Closeout` without an open `PARKED` state in `docs/phase-runs/*-phase-<n>.md`; bind one commit across `HEAD`, authoritative `git ls-remote origin refs/heads/main` tip, build, proof, snapshots, and both installed manifests, rechecking clean tree and `origin/main` before publication; capture `build/linux/linux/helio` and `build/linux/helio.exe` into a read-only isolated snapshot directory after `app_build.py` (verifying `test_windows_and_linux_take_each_job_once` ran and passed without skipping), verify `prove.py` passes AND run Windows unittest verification against the captured snapshot digests while also verifying `build/linux/` binary digests and `st_mtime_ns` did not change across `prove.py`; guard cross-host replacement with `<to>/.phase.lock` and a durable `<to>/phase.journal.json` recording pre-transaction state for both hosts, which hosts actually changed (skipping already-current healthy hosts to preserve distinct `previous` generations on retry), and `phase_state` (`acquiring`, `planned`, `wsl_done`, `both_done`, `notifying`, `rolling_back`, `rolled_back`) so crashes between hosts or during rollback resume cleanly; verify both live installations against the proved digests plus fresh-shell `helio check` on the real store (without `HELIO_STORE`) and an isolated cross-OS `ask`/`take`/`done`/`show`/`check` roundtrip, rolling back only changed hosts (`--back` plus pre-phase `previous` generation restoration, or `--remove` on first install) and exiting 3 on any fault; send one idempotent `helio tell impossible/all` with stable sender `--as impossible/apps`, `--key install-phase-<n>-<commit>`, and noninteractive stdin body only after verification succeeds (retrying only notification when resuming from `notifying`); update `AGENTS.md`, `docs/unattended-prompt.md`, and all three `process-phase/SKILL.md` and `process-plan/SKILL.md` copies with the post-closeout-push phase-install step, pending-install restart check, and separate run-file commit; and test all refusal, crash/rollback, retry, backward-compatibility (distinct historical WSL and Windows binaries reading a populated store and preserving keyed sends), and nonrecursive real-SDK fixture-checkout end-to-end paths in `tests/test_install.py`.

**Phase:** 0
**Needs:** wsl-windows-interop
**Surface:** no

**Build order:**

1. Write the conditions, proved-snapshot proof binding, durable cross-host phase transaction, fresh-shell verification with rollback, `--win-to` CLI contract, and idempotent notification in `tools/install.py --phase <n>`. Done when: each unmet condition refuses and says which, and a planted verification or mid-transaction fault restores changed hosts and exits 3.
2. Add the phase-end step to `AGENTS.md`, `docs/unattended-prompt.md`, and the `process-phase` skills, and cover all conditions, cross-host recovery, retry idempotence, backward store compatibility, and nonrecursive fixture-checkout end-to-end proof in `tests/test_install.py`. Done when: attended and unattended runs both reach it and `app_build.py` passes.

- [x] Refuse in `tools/install.py --phase` unless `scripts/todo-graph.py query plan` shows every section of the phase verified. Done when: one open section refuses. Cheaper substitute: the run's word.
- [x] Refuse unless the tree is clean, `HEAD` is on `origin/main`, `app_build.py` passes in WSL with the race against Windows run and not skipped, and `prove.py` passes, in `tools/install.py --phase`. Done when: each is planted and refuses with the installed program untouched. Cheaper substitute: a passing build.
- [x] Install the programs that were proved, by their SHA-256 taken before the proof, in `tools/install.py --phase`. Done when: a program rebuilt after the proof is refused. Cheaper substitute: building again.
- [x] Try both installed programs after the replacement in `tools/install.py --phase`: `helio check` on the real store, then send, take and finish between Windows and WSL in a store of its own. Done when: a planted fault puts the program before it back and exits 3. Cheaper substitute: `helio help`.
- [x] Run the program before it on a store the new one wrote, in `tests/test_install.py`. Done when: `helio check` passes, so that going back loses nothing. Cheaper substitute: trusting the format.
- [x] Add the step to the end of a phase in `AGENTS.md` and `docs/unattended-prompt.md`: run it after the closeout, record the commit in the run file, and heal a refusal as any red gate. Done when: both name the one command. Cheaper substitute: a note in `README.md`.
- [x] Tell the other lanes with `helio tell impossible/all` from `tools/install.py --phase`, keyed by the commit. Done when: a repeat sends nothing new. Cheaper substitute: silence.
- [x] Commit: `install: after a tested phase`

**Test checkpoint:** `python3 ../ImpossibleSDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

> **Verified:** 2026-11-24 | §17 | bad7ff6ca4483ebfa1f78a397b98fef5a9729cd8
> **Review:** `docs/reviews/d01-t01-s17.json`

## Verification

- A job is sent on Windows, taken in WSL with `helio take --kind request --lane neon-a --json`, marked landed, done, archived and shown again by its id.
- `python3 ../ImpossibleSDK/tools/prove.py .` exits 0 after the last commit.
- `python3 scripts/todo-graph.py validate` and `python3 scripts/todo-graph.py plan --check` pass.
