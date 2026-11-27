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
| 2 | §2 | Move a closed message to its month | §1 | [x] |
| 3 | §3 | Read, answer and repeat archived messages | §2 | [x] |
| 4 | §4 | Sessions announced by every call | -- | [x] |
| 5 | §5 | Report of a silent taker | §4 | [x] |
| 6 | §6 | Messages for one session | §5 | [x] |
| 7 | §7 | Projects kept apart in log and show | -- | [x] |
| 8 | §8 | Mark for landed work | §2 | [x] |
| 9 | §9 | Fixed marks around every shown message | §7 | [x] |
| 10 | §10 | JSON writer and one reply per call | §3, §5, §6, §8, §9 | [ ] |
| 11 | §11 | Take the next job as a queue | §10, §22 | [ ] |
| 12 | §12 | Expiry of seen heads-ups and findings | §3, §4 | [ ] |
| 13 | §13 | Design and help that match the program | §11, §12, §14, §21, §23 | [ ] |
| 14 | §14 | Unowned paths kept to their repository | §4, §6 | [x] |
| 15 | §15 | Quick installer | -- | [x] |
| 16 | §16 | Installer for Windows sessions | §15 | [x] |
| 17 | §17 | Installed program replaced after a tested phase | §15, §16 | [x] |
| 18 | §18 | Session tie and listing tests held to their result | §14 | [x] |
| 19 | §19 | Archive paths opened without following a swapped folder | §3 | [x] |
| 20 | §20 | Windows launches from WSL that survive an interop timeout | §17 | [x] |
| 21 | §21 | JSON for the inbox, log, show and check | §10 | [ ] |
| 22 | §22 | JSON for sending, job changes, archive and working | §10 | [ ] |
| 23 | §23 | Summary command read from the day's snapshot | §10 | [ ] |

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

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

> **Verified:** 2026-11-24 | §1 | b671912bc78b1d68e4a800a14d0fc1615cc5c4b1
> **Review:** `docs/reviews/d01-t01-s1.json`

## 2. Move a closed message to its month

> **Started:** 2026-11-25T00:39Z

**Corrected 2026-11-25:** re-planned with `todo-graph.py replan` (original build began 2026-11-24T19:55Z; prior review chain kept as `docs/reviews/01-heliograph/D01-T01-s2-chain1.md`) to resolve three mutually exclusive demands raised across Reconciliation 5 and 6: (a) Windows UNC/9p directory flush (`\\wsl.localhost\...`): Microsoft's `P9Rdr` kernel redirector unconditionally returns `ERROR_INVALID_FUNCTION`/`ERROR_NOT_SUPPORTED` for `FlushFileBuffers` on directory handles while strictly supporting `FlushFileBuffers` on file handles; therefore directory flushes are strictly enforced on Linux and local Windows NTFS, while on Windows UNC/9p redirector paths `CreateFileW` directory-open checks and file-handle `FlushFileBuffers` (`p_write` + `p_sync` on every archive copy) are strictly enforced and live sources are preserved on any file write/flush or directory-open error so Windows `helio archive` works on the designated WSL store. (b) Lock-free daily 200-job cap: `archive_daily` uses 10 immutable exclusive-create slots (`summary/<YYYY-MM-DD>.slot.0` .. `.slot.9`, 20 jobs each, honoring legacy `.claiming` debits) plus per-slot `.done.<k>` completion files and publishes `summary/<YYYY-MM-DD>.archived` on exhaustion, all 10 `.done.<k>` complete, or stale-slot recovery (`>= 60s` without a lock file), propagating any I/O error as exit 3. (c) Cross-host 1,000-round race on the designated WSL store (`\\wsl.localhost\...`): Windows + WSL archivers move 1,000 distinct eligible jobs across 1,000+ single-message rounds while Windows + WSL concurrent readers run 1,000+ rounds until both archivers finish, verifying `store_open`, full parsed readability of all 20 live controls, 100% presence of all 1,000 eligible IDs across `messages/` and `archive/<month>/`, and full parsed readability of a rotating window of 10 eligible IDs per round, alongside Windows + WSL takers confirming zero duplicate takes and zero missing messages.

**Corrected 2026-11-24:** Incorporated `§1` plan review finding 2 and all ten `§2` Plan review findings (`docs/reviews/01-heliograph/D01-T01-s2-chain1.md`): (1) Late `.seen.*` preservation: during orphan sweep, copy any valid live `<id>.seen.*` (`"seen\n"`) to `archive/<year-month>/` (durable write + byte-compare on `P_EXISTS`) BEFORE removing the live mark so a `show` racing post removal never loses its seen mark. (2) Safe recovery invariant: orphan cleanup runs ONLY when live `messages/<id>.post` is proven `P_MISSING` AND `archive/<year-month>/<id>.post` + its archived chain validate to a terminal state (`DONE` or `REFUSED`); while live `.post` exists, resume copying and preserve all live files on any conflict; `store_check` distinguishes an interrupted archive (validated archived terminal chain) from genuine damage (missing step or unarchived orphan exits 3). (3) Durable directory creation and copy publication: flush parent and new directories in `p_folder` on Linux and Windows, add `p_sync` to flush an existing identical archive copy before deleting any live source (closing the `P_UNFLUSHED` retry window), and never remove live sources on write/flush error (exit 3). (4) Keyed-send safety before activation: `store_post` checks `archive/<year-month>/<id>.post` on keyed retries so retrying an archived keyed message returns the original ID without creating a new live post or allowing a second take (tested across post removal and after cleanup). (5) Eligibility contract: only jobs (`request`/`handoff`) in `DONE` or `REFUSED` with `now.when <= now` and `now - now.when >= 30 days` move, to `archive/<YYYY-MM>/` derived from the ID's creation timestamp; non-job messages stay live pending `§12`; tested at `30d-1s`, `30d`, future `when`, and cross-month creation vs closure. (6) Daily 200-message auto-archive trigger: first `inbox` of the day after `watch_summary` checks `summary/<YYYY-MM-DD>.archived`, archives up to 200 eligible messages, and writes `summary/<YYYY-MM-DD>.archived` on completion so hook-first, simultaneous inboxes, interruption resume, same-day skip, next-day re-trigger, and explicit `helio archive` drain all work deterministically. (7) Overfull-store recovery: `helio archive` enumerates `messages/` in bounded batches without tripping `MOST_NAMES` in `store_open`, draining an overfull store back to normal access while preserving ineligible jobs and `key.*` reservations. (8) Manual `helio archive` CLI contract: runs without `--as`, refuses extra args with exit 2, archives healthy eligible neighbors in mixed batches while naming damaged IDs and exiting 3 on any damage/conflict/IO error, exits 1 when zero eligible messages exist, and exits 0 when at least one eligible message was archived or moved by a concurrent peer. (9) Cross-host race test: runs on WSL against Windows without skipping, racing real archivers, takers, and readers across 1000 rounds and verifying every eligible closed job moved with its complete terminal chain while live controls had zero duplicate takes and zero missing messages. (10) Build order: add `engine/archive.h` and `engine/archive.c` to `CMakeLists.txt` and wire `helio archive` before running interruption and race tests through the production path.

A job done or refused for 30 days moves to `archive/<year-month>`. The month comes from the message's own id, so the place needs no index. `helio archive` does it, and the first inbox of the day does at most 200.

**Phase:** 1
**Needs:** wsl-windows-interop
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
- [x] Commit: `archive: closed jobs move to their month`

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

> **Verified:** 2026-11-25 | §2 | 8f9752acd0ec10475197ed6b416d1698e43a4a40
> **Review:** `docs/reviews/d01-t01-s2.json`

## 3. Read, answer and repeat archived messages

> **Started:** 2026-11-25T04:36Z

**Corrected 2026-11-25:** Incorporated all eight Plan review findings (`docs/reviews/01-heliograph/D01-T01-s3.md`): (1) Archived `show` seen marks: when `helio show <id>` reads an archived message (`m.archived`) addressed to `me`, it persists the seen mark directly to `archive/<YYYY-MM>/<id>.seen.<me>` (never `messages/`), exiting 0 on first and repeated reads with zero live files created; if a live `show` races a real archiver between reads, it falls back to the archive copy (while preserving §1's exit-2 refusal when no archive copy exists). (2) Separate explicit-ID vs live-scan lookups: `store_message` resolves live `messages/` first and falls back to `archive/<YYYY-MM>/` ONLY on proven `P_MISSING` (setting `m.archived = true`, preserving exit 2 for invalid/unknown IDs and exit 3 for read/parse errors), while `store_message_live` stays live-only for `every()` (`inbox`, plain `log`), `watch_summary`, and the live loop of `store_check`. (3) Complete archived chain replay and refusal safety: `store_job` replays the complete chain from `m->archived ? archive/<YYYY-MM>/ : messages/`, asserting exact terminal state (`done`/`refused`) and actor across multi-step chains (`taken`/`released`/`taken`/`done|refused`), rejecting broken/incomplete archive-only chains as `S_DAMAGED` (exit 3), and `store_take`/`store_release`/`store_end` refuse archived IDs with exit 2 (`"it was archived"`) leaving both `messages/` and `archive/` untouched. (4) Interrupted-move handling: `log --all` deduplicates IDs present in both `messages/` and `archive/<YYYY-MM>/` so each message appears once with its complete state; `store_check` accepts a byte-identical in-progress archive copy alongside a live post, flags a conflicting copy as damage, and reports an incomplete archive-only job as damaged. (5) Archive validation contract in `store_check`: performs full structural and content-rule validation on every archived file (truncated/corrupted posts, body-length/header mismatches, month-folder vs ID-timestamp mismatches, missing/gapped/invalid/non-terminal job steps, orphan links/seen marks, malformed seen marks, unexpected files/folders, conflicting in-progress copies, and unreadable files, naming the offending file/ID and exiting 3); same-length payload edits that remain structurally valid cannot be detected without external checksums, which the stored plain-text format does not carry. (6) `answer` verification: asserts one new live reply in `messages/` with exact `to`, `subject`, `body`, and `re` equal to the archived ID, readable by the original sender in a fresh process, and verifies unknown/unreadable parents create no reply. (7) Keyed repeat at capacity: `store_post` checks an existing `--key` reservation (live or archived) BEFORE enforcing the live-store admission limit (`store_max_names / 2`), so an exact repeat returns its original ID at capacity, a conflicting repeat refuses, and a genuinely new send remains subject to the limit. (8) `log --all` traversal: wired via `o->all`, emits live rows plus archived months newest-month-first using bounded per-month batch traversal (unbounded by `store_open`'s `MOST_NAMES`), keeps plain `log` live-only, exits 1 on an empty/new store before `archive/` exists, and prints healthy rows while exiting 3 on a damaged/unreadable month.

An archived message must still be found by its id. Without this a keyed send repeated after archiving would make the message again, because `store_post` sees no post.

**Phase:** 1
**Needs:** none
**Surface:** no

**Build order:**

1. Let `engine/store.c` look in the month that the id names. Done when: `show` and `answer` work on an archived id.
2. Cover repeats in `tests/test_helio.py`. Done when: a repeated key makes nothing new.

- [x] Read an archived message in `store_message` of `engine/store.c`, kept apart from the live look of §1. Done when: `helio show` gives the same text before and after archiving. Cheaper substitute: "there is no such message".
- [x] Read an archived job's steps in `store_job` of `engine/store.c`. Done when: its line still names who closed it. Cheaper substitute: "closed" alone.
- [x] Give the first id for a repeated key in `store_post` of `engine/store.c` when its message is archived. Done when: the repeat prints the first id and `messages` gains no file. Cheaper substitute: removing the key when archiving.
- [x] Refuse take, release, done and refuse on an archived id in `engine/store.c`. Done when: each exits 2 and says it is archived. Cheaper substitute: exit code 3.
- [x] Show archived messages with `log --all` in `engine/helio.c`, newest month first. Done when: a test of two months lists both in order. Cheaper substitute: the live store only.
- [x] Examine archived files in `store_check` of `engine/store.c`. Done when: a byte changed in an archived file is reported. Cheaper substitute: trusting the archive. **Corrected 2026-11-25:** validates structural and content-rule damage across archived posts, steps, seen marks, and month folders (same-length valid-payload edits require external checksums not carried by the plain-text format; see decision in `docs/phase-runs/2026-11-24-phase-1.md`).
- [x] Commit: `archive: still found by its id`

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

> **Verified:** 2026-11-25 | §3 | 9093c2f9aa6e806af45ebde1b1708ca5be282602
> **Review:** `docs/reviews/d01-t01-s3.json`

## 4. Sessions announced by every call

> **Started:** 2026-11-25T06:28Z

Only the Claude hook announces a session, and it records a time alone. Codex and Antigravity never hook, so they look silent. Every call that names its lane becomes a sign of life, and `helio hello` records agent and repository. **Corrected 2026-11-25:** Plan, Round 1, Completion, and Reconciliation reviews (`docs/reviews/01-heliograph/D01-T01-s4.md`) clarified and redesigned per-session updates: replace lock files entirely with lock-free monotonic CAS generation files (`<base>` for seq 0, `<base>~<seq>` for seq >= 1) created atomically via `p_create_excl` (retrying on `P_EXISTS` with merged `agent` and monotonic `time`, sweeping the superseded generation best-effort, and grouping by highest generation in `place_session`/`place_each_session`) so there are zero lock files, zero stale-lock recovery races, zero lock timeouts, a single atomic publication point (pre-publish failures leave prior metadata untouched; post-publish `P_UNFLUSHED` reports exit 3 with explicit un-flushed wording), and read-error propagation in `place_each_session` and `place_busy`; resolve integration session IDs from `HELIO_SESSION`, else the single set integration env var (`CODEX_THREAD_ID`, `ANTIGRAVITY_CONVERSATION_ID`, `CLAUDE_CODE_SESSION_ID`), else a deterministic `nested-<16-hex>` hash of all set integration vars (failing on allocation error) so nested siblings stay distinct while every command in one child agrees; read strict one-token `repository: <name>` from `helio.conf` (leaving it empty outside a configured repository); announce best-effort across `help` (when store and lane are available), `init` (after creation), `archive`, and valid `hook` (continuing delivery on warning); validate `<agent>` and `session_ok` (1..64 bytes unchanged by `clean`) before `store_open`; validate complete named-session filenames in enumeration; and exercise `place_session`/`place_each_session` through the C test harness with a 512-byte read cap.

**Phase:** 2
**Needs:** none
**Surface:** no

**Build order:**

1. Widen the file in `engine/place.c`. Done when: an old file with a time alone is still read.
2. Call it from `engine/helio.c`. Done when: each command changes the time.

- [x] Record time, agent, repository and session in `place_hello` of `engine/place.c` (design, "How does it find the reader?"). Done when: each is read back and an old file still counts. Cheaper substitute: the time alone.
- [x] Keep one file for each session of a lane in `engine/place.c`. Done when: two sessions of one lane do not overwrite each other. Cheaper substitute: one for the lane.
- [x] Add `hello <agent>` to the commands in `engine/helio.c`, and use it in the hook. Done when: an unknown agent exits 2 and the three known ones are stored. Cheaper substitute: the hook only.
- [x] Announce the lane on every other call in `engine/helio.c` without failing it. Done when: an unwritable `sessions` leaves the command's exit code unchanged and says why. Cheaper substitute: ignoring it.
- [x] Commit: `sessions: every call is a sign of life`

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

> **Verified:** 2026-11-25 | §4 | 5da8c72349bd58f046552c217fe829bc925152e4
> **Review:** `docs/reviews/d01-t01-s4.json`

## 5. Report of a silent taker

> **Started:** 2026-11-25T11:27Z

A job taken by a lane that has gone quiet is reported even when it has no due time. It is never handed on by itself. **Corrected 2026-11-25:** Plan review (`docs/reviews/01-heliograph/D01-T01-s5.md`) clarified: initialize silence thresholds and lazy session loading in shared `fill_inbox` so `hook claude` and `inbox` behave identically; load sessions lazily with an explicit failure state (`SESS_FAILED`) only when a relevant `TAKEN` job needs classification so unreadable sessions never break unrelated exit codes or fabricate false `SILENT` labels; persist each repository's configured `silent:` threshold per project (`<store>/silent/<project>`, overridden by live `helio.conf` for its own project, defaulting to 120 minutes; an edit to `silent:` in `helio.conf` becomes shared-store-authoritative on the next `helio` call in that repository, synced by `place_record_silent` before reporting and summary) so shared-store summaries and out-of-repo readers use each job's project threshold regardless of first-caller order; accept positive integers up to the checked `uint64_t` conversion bound (`UINT64_MAX / 60000000` minutes); refresh the calling session before `watch_summary` on `inbox`/`hook` so a returning taker clears its own alert; and list overdue+silent jobs under both `Overdue` and `Silent` (excluding them from `Taken`) in the daily summary.

**Phase:** 2
**Needs:** none
**Surface:** no

**Build order:**

1. Decide silence in `engine/watch.c`. Done when: it is a pure function of the sessions, the job and the clock.
2. Show it in `engine/helio.c`. Done when: the sender and the owner see it and others do not.

- [x] Write `is_silent` in `engine/watch.c` from the taker's session, else its lane (design, "How is nothing lost or done twice?"). Done when: a table of times and sessions passes. Cheaper substitute: the age of the take.
- [x] Read `silent: <minutes>` from `helio.conf` in `engine/place.c`, 120 when absent. Done when: a wrong value is refused with its line. Cheaper substitute: a fixed time.
- [x] Return a silent job to its sender and the owner in `into_inbox` of `engine/helio.c`, marked SILENT. Done when: both see it, a stranger does not and the taker keeps it. Cheaper substitute: the owner alone.
- [x] Add a Silent part to the summary in `engine/watch.c`. Done when: the count and the job are there. Cheaper substitute: counting it as Taken.
- [x] Release a silent job and take it again in `tests/test_helio.py`. Done when: only `helio release` frees it. Cheaper substitute: a release by itself.
- [x] Commit: `watch: a silent taker is reported`

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

> **Verified:** 2026-11-25 | §5 | 34e4b9735c265e86c084d2f1cc0647732272c95b
> **Review:** `docs/reviews/d01-t01-s5.json`

## 6. Messages for one session

> **Started:** 2026-11-25T12:36Z

The design lets a message be for a session, not only a lane. `--for-session <name>` adds it to the reader. **Corrected 2026-11-25:** Plan review (`docs/reviews/01-heliograph/D01-T01-s6.md`) and Round 1 review clarified: validate `seen.*` filenames through one shared `store_valid_seen_event` grammar in `store_check`, `archive.c` (admission, recovery, sweep, and archive check), using `seen.<proj>+<lane>`, `seen.<proj>+<lane>+<32-hex-sha256-128>`, and lane-wide owner alert ack `seen.<proj>+<lane>~alert[NNNN]` (with exact byte-length payload checks and existing-mark verification on `P_EXISTS`); refuse `--for-session` containing secrets in `store_post` and refuse literal or `path:`-resolved `/all` in `store_post` (before key reservation) and `store_parse_post`; block mismatched or unnamed sessions on the addressed lane from refusing self-addressed or owner-addressed targeted jobs in `objection` and replay; acknowledge silent-session owner alerts on successful `helio show <id>` only when an alert is currently outstanding (keyed by release generation `links`), using a lane-wide `~alert` mark scoped to the destination project's owner without hiding the message from the target or clearing independent overdue reporting; distinguish absent/seen/error in live-disk `store_was_seen` (suppressing escalation on read error); measure silence from send time (or release time for reopened jobs); document both `--session` and `--for-session` roles in `help` and display `(for <session>)` in `show`/listings; and update both installed WSL and Windows binaries together via `tools/install.py --phase 2` before any caller sends a targeted `--for-session` post or any named caller runs `show` against a shared store (legacy posts remain fully compatible).

**Phase:** 2
**Needs:** none
**Surface:** no

**Build order:**

1. Add the field in `engine/store.c`. Done when: a message without it is read as before.
2. Use it in `is_for` of `engine/store.c`. Done when: the other sessions of the lane do not see it.

- [x] Write and read `for:` in `store_post` and `store_message` of `engine/store.c` (design, "What does a message hold?"). Done when: old messages pass `helio check` and a bad name is refused. Cheaper substitute: a name in the subject.
- [x] Compare the session in `is_for` and `objection` of `engine/store.c`. Done when: another session of the lane neither sees nor takes it. Cheaper substitute: hiding it in the inbox alone.
- [x] Mark it seen for a session in `store_seen` of `engine/store.c`. Done when: one session's look does not hide another's message. Cheaper substitute: one mark for the lane.
- [x] Show it to the owner when the session is silent in `engine/helio.c`. Done when: it appears once for the owner after the time of §5. Cheaper substitute: it waits unseen.
- [x] Commit: `messages: for one session`

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

> **Verified:** 2026-11-25 | §6 | 761a76efa0ac63be8ed968b3f1db6b6a707e5424
> **Review:** `docs/reviews/d01-t01-s6.json`

## 7. Projects kept apart in log and show

> **Started:** 2026-11-25T13:57Z

The design says a session sees only its own project's messages and those sent to it by name. Today `log` prints every project and `show` opens any id. **Corrected 2026-11-25:** Plan review (`docs/reviews/01-heliograph/D01-T01-s7.md`) clarified: enforce `may_see` in `change_job` (`take`, `release`, `done`, `refuse`) immediately after loading the post so foreign IDs return the exact missing-message refusal (`there is no such message`) without disclosing state; reject explicitly empty or malformed `--as` / `HELIO_AS` (including present-but-empty `HELIO_AS=""`) and fail closed on lane allocation failure in `log` so a scoped call can never silently widen to owner all-project mode; require `--as` or `--all` for `log` outside a configured repository while keeping both named-lane and project-only `helio.conf` checkouts scoped to their project under both `log` and `log --all` (walking ancestors via `p_cwd` to the exact POSIX, Windows drive, or UNC share root with fail-closed error handling); pass a visibility predicate into `archive_every` before archived chain replay so foreign damaged archived chains cannot affect a scoped caller; scope `watch_classify_job` / `watch_classify_session_silent` session reads to the exact taker session (`place_session`) for named takes and taker lane (`place_lane_sessions`) for sessionless takes so unrelated unreadable session files never mark a visible `log` damaged; keep `inbox`'s automatic `archive_daily` error message generic (no foreign IDs on stderr; detailed report stays on explicit `helio archive`), noting that `summary/<day>.md` remains a persisted store-wide owner report; check `may_see` at the top of each `show` rendering pass (including concurrent-archive fallback) and in `into_inbox`; update `help` and `README.md`; and verify exact `(rc, stdout, stderr)` missing-ID equivalence and zero message-state side effects (no reply, key reservation, job step, seen mark, or alert ack; `§4` session announcements still run) across live and archived foreign IDs.

**Phase:** 2
**Needs:** none
**Surface:** no

**Build order:**

1. Write one rule in `engine/store.c`. Done when: it decides from the message and the lane alone.
2. Use it in `engine/helio.c`. Done when: a same-project sibling can read a session-targeted message through `log` and `show`, while `inbox` and `hook` withhold it and its read leaves the target's unread state intact.

- [x] Write `may_see` in `engine/store.c`: the project of either side, or sent to this lane (design, "How does it find the reader?"). Done when: a table of both sides passes. Cheaper substitute: the receiving side alone.
- [x] Keep to it in `log`, `show` and `answer` of `engine/helio.c`. Done when: a stranger's id exits 2 with the words of a missing one. Cheaper substitute: a warning.
- [x] Show every project with `log --all` when no lane is named outside a configured repository in `engine/helio.c`, for the owner's terminal. Done when: a call with a lane or inside a configured checkout stays inside its project. Cheaper substitute: no way to see all.
- [x] Commit: `messages: projects stay apart`

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

> **Verified:** 2026-11-25 | §7 | 36dc2566ca3068ea62fc0d9b3cc07c8c442fa3cd
> **Review:** `docs/reviews/d01-t01-s7.json`

## 8. Mark for landed work

> **Started:** 2026-11-25T16:23Z

A linked commit marks a job landed and closes nothing. The engine starts no program, so the caller gives the commit. **Corrected 2026-11-25:** Plan and Round 1 reviews (`docs/reviews/01-heliograph/D01-T01-s8.md`) clarified: dispatch `landed` explicitly to `store_landed` (never falling through to `store_end`); validate commit syntax (7 to 64 hex digits, normalized to lowercase) and session syntax in CLI before `store_open` as well as in `store_landed`; enforce `wrong_dest_session` precedence before sender/owner overrides so session-targeted jobs cannot be marked by a wrong/empty sibling session; treat atomic `make_ex` publication after pre-check authorization as final and immutable (no post-hoc rollback that could erase another caller's acknowledged mark), call `p_sync` on `P_EXISTS` repeats so an unflushed prior publication is durably flushed before exit 0, and promote any concurrently archived published mark via `archive_promote_landed` (`copy_durable` + live removal, returning exit 0) so an acknowledged mark always survives and an exit-2 refusal (before `make_ex`) never introduces an archived mark; merge both archived and live landed marks in `store_landed_commits` and `archive_every` so duplicate-post and interrupted-archive states never hide an acknowledged commit; load landed marks after visibility/eligibility checks, propagate read/flush/OOM errors as exit 3, bound inbox/hook commit display so one heavily marked job never overflows the 2,000-byte budget, and include landed marks in all archive orphan, sweep, and check paths.

**Phase:** 2
**Needs:** none
**Surface:** no

**Build order:**

1. Write the mark in `engine/store.c`. Done when: it is made once and read back.
2. Show it in `engine/helio.c`. Done when: the state of the job is unchanged.

- [x] Make `<id>.landed.<commit>` in `engine/store.c` for 7 to 64 hexadecimal digits (design, "Not goals"). Done when: a repeat changes nothing and other text exits 2. Cheaper substitute: a note on the job.
- [x] Add `landed <id> <commit>` to the commands in `engine/helio.c` for the taker, the sender and the owner. Done when: a stranger is refused and the job stays open or taken. Cheaper substitute: closing the job.
- [x] Show the commit in `line` of `engine/helio.c`, and examine the mark in `store_check` of `engine/store.c`. Done when: a damaged mark is reported. Cheaper substitute: `show` alone.
- [x] Move the mark with its message in `engine/archive.c`. Done when: an archived job still names its commit. Cheaper substitute: dropping it.
- [x] Commit: `jobs: a commit marks them landed`

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

> **Verified:** 2026-11-25 | §8 | 3eb564fd010360898f86137fe69d3fc7aeee91d1
> **Review:** `docs/reviews/d01-t01-s8.json`

## 9. Fixed marks around every shown message

> **Started:** 2026-11-25T17:30Z

`show` puts a bar before each line. The inbox, the log and the hook print a sender's subject with no closing mark. **Corrected 2026-11-25:** Plan review (`docs/reviews/01-heliograph/D01-T01-s9.md`) clarified: emit `| \n` for `show`'s blank separator line so every interior line between the fixed opening and closing marks strictly starts with `| `; derive exact overhead from the emitted literals (`MARK_BEGIN` 108 bytes, `MARK_END` 32 bytes, `HOOK_DAMAGE` 64 bytes, `TRAILER_RESERVE` 32 bytes, plus exact hook header length) so even the largest `hook` output with maximum-length lane, held trailer, and damage notice stays `<= 2,000` bytes; use one `emit_log_row` path for live and archived log rows that opens on the first formatted row, checks both mark writes, stops after any write error, and exits 3; accumulate `damaged += every(...)` across all three urgency passes in `fill_inbox`; put `MARK_END` inside `show`'s checked buffer before seen-state writes; and test literal mark subjects/bodies, archive-only `log --all`, damage-only hook, and byte-cutoff boundary in `FixedMarks`.

**Phase:** 2
**Needs:** none
**Surface:** no

**Build order:**

1. Print the marks in `engine/helio.c`. Done when: each listing begins and ends with one.
2. Try to forge them in `tests/test_helio.py`. Done when: no subject or body can.

- [x] Print one opening and one closing mark in `inbox`, `whole_log`, `show` and `hook` of `engine/helio.c` (design, "How does it reach the reader?"). Done when: each output has both, and an empty inbox prints nothing. Cheaper substitute: the opening mark alone.
- [x] Start each line of a message with a bar in `engine/helio.c`. Done when: a subject that spells the closing mark stays behind its bar. Cheaper substitute: refusing such subjects.
- [x] Count the marks in the 2,000 characters in `fill_inbox` of `engine/helio.c`. Done when: the largest hook output is 2,000 characters or fewer. Cheaper substitute: counting messages alone.
- [x] Commit: `output: fixed marks around messages`

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

> **Verified:** 2026-11-25 | §9 | 124fad2bfbaebd3f02eab670f55a37c5e0ab5334
> **Review:** `docs/reviews/d01-t01-s9.json`

## 10. JSON writer and one reply per call

> **Started:** 2026-11-27T13:40:00Z

A script must not parse lines meant for people. This section builds the writer and the one rule every command keeps: with `--json` a call gives exactly one JSON value. The commands' own shapes are `D01 T01 §21` (reading), `D01 T01 §22` (sending and changing) and `D01 T01 §23` (summary).

**Corrected 2026-11-27 (plan review, `docs/reviews/01-heliograph/D01-T01-s10.md`):** the section was "JSON output on every command" with the reading, changing and summary commands in it. The plan review found twelve gaps that make each of those its own feature (bounded inbox, visibility, read-back of each change, the summary snapshot and overlap count), so they moved to `§21`, `§22` and `§23` and this section keeps the writer, the one-reply rule, the flag, errors, `help` and `hook`.

**Contract:** with `--json` anywhere among a call's words (before or after the command, never as the value of an option that takes one, such as `--about --json`) the call prints exactly one JSON value and a newline on stdout and nothing on stderr, also when it is refused while its words are read or its `helio.conf` is bad. Exit codes stay 0 done, 1 nothing, 2 refused, 3 damaged (design, "Commands"). A call that did part of its work and then failed gives one value holding both: its result fields and `"error": <text>, "code": <exit code>` (and `"problems": [<text>...]` where it found several), never a result followed by a second value. A failed write of the reply exits 3 and does not acknowledge what it did not print (no seen mark for an unread message). Text is UTF-8; a byte sequence that is not UTF-8 becomes U+FFFD; the engine still uses no floating point or system call (`AGENTS.md`). `helio help --json` gives `{"help": <text>}`; `helio hook claude --json` gives `{"context": <text or null>}`, and without the flag the hook's installed output is unchanged.

**Phase:** 3
**Needs:** none
**Surface:** no

**Build order:**

1. Write `engine/json.c` and wire it into the build. Done when: the encoder's output parses in Python to the exact strings given.
2. One reply per call in `engine/helio.c`. Done when: every error and partial result path gives one value.

- [ ] Write `engine/json.h` and `engine/json.c` (object, array, string, number, boolean, null into a `text`) and add `engine/json.c` to `CORE` in `CMakeLists.txt`. Done when: both hosts build it with `-Werror`.
- [ ] Escape strings in `engine/json.c`: `"`, `\`, control characters as `\u00XX`, and any byte sequence that is not UTF-8 (truncated, overlong, a surrogate, past U+10FFFF) as U+FFFD. Done when: a racer entry point in `tests/racer.c` encodes given bytes and a test in `tests/test_helio.py` decodes stdout strictly as UTF-8 and finds the exact expected strings for ASCII, quotes, backslashes, newlines, valid two- to four-byte text, each invalid kind, and 10000 random byte strings (each compared with Python's own decoding with `errors="replace"`). Cheaper substitute: checking only that the output parses.
- [ ] Recognize `--json` for the whole call in the word reader of `engine/helio.c`, before any refusal, without taking it from an option's value. Done when: `helio show --bad --json`, `helio --json show`, a missing option value and a bad `helio.conf` each give one error value, and `--about --json` keeps `--json` as the paths. Cheaper substitute: the flag only after the command.
- [ ] Route every reply through one writer in `engine/helio.c`: `fail` and every partial-result path add `"error"` and `"code"` to the call's one value. Done when: a test runs each path that today prints and then fails (an inbox with a damaged job beside readable messages, a session announcement failing after a send, `show` failing to write its seen mark after printing) and the whole stdout is one JSON value, stderr is empty, and the exit code is the old one. Cheaper substitute: checking only the exit code.
- [ ] Refuse to acknowledge what was not printed: a failed write of the JSON reply exits 3 and writes no seen mark. Done when: a test with stdout closed early (`SIGPIPE` ignored, as `eebea74` does) leaves the message unseen for the next inbox.
- [ ] Give `help` and `hook` their JSON forms in `engine/helio.c`. Done when: `helio help --json` parses to the help text, `helio hook claude --json` gives `{"context": ...}` for messages, `null` for none and an error value for a damaged store, and the hook without `--json` prints what it printed before (byte-compared).
- [ ] Commit: `output: one JSON reply per call`

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0, and the tests fail with the escaping, the whole-call flag or the one-writer rule removed.

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

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

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

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

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

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

## 14. Unowned paths kept to their repository

> **Started:** 2026-11-25T18:06Z

A path that no lane owns goes to whoever works in that repository, and to the owner when nobody is there (design, "How does it find the reader?"). Today `reader` in `engine/helio.c` sends it to the whole project when any lane of the project was seen in the last half hour, and a test asks for that. **Corrected 2026-11-25:** Plan review (`docs/reviews/01-heliograph/D01-T01-s14.md`) clarified: pass the selected `repository` from `reader` through `post` into `store_post` (keeping empty for direct addresses, explicit `/all`, and `answer`); pass the dispatch `actor` (carrying `here.repository`) into `hook` and use designated initializers for all `actor` literals; enforce repository identity for taker authority in `objection`, `landed_objection`, replay (`store_apply_step`), and `watch.c` `is_silent` liveness while preserving independent sender and owner powers from any checkout; use one shared `repo_ok` validator (`word` and not `"all"`) across `helio.conf` and persistence; display `(in <repo>)` in `format_line` and `watch_summary`; update `helio.conf`, `README.md`, and the §4 legacy-session fixture; rollout boundary: update both WSL and Windows installed binaries together via `tools/install.py --phase 2` before emitting `repository:`-scoped posts to a shared store.

**Phase:** 2
**Needs:** none
**Surface:** no

**Build order:**

1. Store the repository as the reader in `engine/store.c`. Done when: a message without it is read as before.
2. Send and show it in `engine/helio.c`. Done when: the three cases of the design are tested.

- [x] Read `repository: <name>` from `helio.conf` in `place_open` of `engine/place.c`: the name is the identity, so Windows and WSL agree on one checkout. Done when: both spell it alike, two repositories of one project differ, a path is refused, and without the line an unowned path goes to the project as before. Cheaper substitute: the name of the folder.
- [x] Write and read `repository:` in `store_post` and `store_message` of `engine/store.c`, beside the reader of §6. Done when: old messages pass `helio check`. Cheaper substitute: one message for each lane.
- [x] Write the actor's repository into each step in `add_link` and read it in `store_job` of `engine/store.c`, as the session is. Done when: `helio check` accepts a take after its session has ended, a step without it still counts for a message without a repository, and one that names another repository is damage. Cheaper substitute: checking the live actor alone.
- [x] Give the repository from the sessions of §4 in `place_busy` of `engine/place.c`, in place of one yes or no for the project. Done when: a lane seen only in another repository of the project does not count. Cheaper substitute: the project.
- [x] Choose the reader in `reader` of `engine/helio.c`: the lane that owns the path, else this repository, else the owner. Done when: each of the three is chosen in a test. Cheaper substitute: the owner whenever no lane owns it.
- [x] Compare the actor's repository in `is_for` and `objection` of `engine/store.c`. Done when: two lanes of the repository both see the one job and one of them takes it, and a lane of another repository neither sees nor takes it. Cheaper substitute: hiding it in the inbox alone.
- [x] Replace `test_an_unowned_path_goes_to_whoever_works_there` in `tests/test_helio.py` with the three cases, its lane announced from inside the repository. Done when: it fails with the old `reader`. Cheaper substitute: keeping the project-wide expectation.
- [x] Commit: `routing: an unowned path stays in its repository`

**Test checkpoint:** `python3 ../ImpossibleSDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted. **Corrected 2026-11-26:** Impossible SDK's folder is now `SDK` (051e1c9), so the command is `python3 ../SDK/tools/app_build.py .`.

> **Verified:** 2026-11-26 | §14 | 5f18e62b045e2a163d4579daecd1c281cf94a615
> **Review:** `docs/reviews/d01-t01-s14.json`

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

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

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

**Test checkpoint:** `python3 ../SDK/tools/prove.py .` exits 0 after the commit and the new tests fail when their production change is reverted.

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

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0 and the new tests fail when their production change is reverted.

> **Verified:** 2026-11-24 | §17 | bad7ff6ca4483ebfa1f78a397b98fef5a9729cd8
> **Review:** `docs/reviews/d01-t01-s17.json`

## 18. Session tie and listing tests held to their result

> **Started:** 2026-11-27T08:20:00Z

Filed 2026-11-26 from the last reconciliation of `D01 T01 §14` (`docs/reviews/01-heliograph/D01-T01-s14.md`, gaps 25 and 40, both P3). §14 breaks an exact tie between two named agents (same agent order, same agent time) on the repository name, but its regression forces only equal agent orders, so removing that last tie-break leaves the test passing whenever the two naming times differ. The one-row listing assertion parses the racer's output without checking its exit code, so a listing that prints the expected row and then fails would pass.

**Phase:** 2
**Needs:** none
**Surface:** no

**Build order:**

1. Force an exact tie in `tests/test_helio.py` and assert its winner. Done when: removing the repository-name tie-break in `merge_record` of `engine/place.c` makes it fail.
2. Check the listing's exit code. Done when: a listing that fails after printing is refused by the test.

- [x] Write one record per repository for one named session in `tests/test_helio.py` (`RepositoryRouting`), with equal `time:`, `order:`, `agent-order:` and `agent-time:` and a different agent each, then read the session with `--read-session`. Place each tied record in turn in the key an older build wrote under the session's name (`<lane>+<hex>`, which `place_session` reads before it lists the repository keys) and the rest in repository keys, so the reader merges the winner first in one case and after the others in another on every filesystem, over two-name and three-name sets. Done when: the agent and repository of the repository name that sorts last are read in every case. Cheaper substitute: equal agent orders alone, or one name pair in whatever order the folder lists it.
  **Corrected 2026-11-27:** said to observe the merge order with `os.listdir`; a name-sorted listing (NTFS on `windows-latest`) only ever lists the winner last, and a separate scan is no evidence of the reader's own order (round 1 findings 1 and 2, `docs/reviews/01-heliograph/D01-T01-s18.md`).
  **Corrected 2026-11-27:** said equal `order:`, `agent-order:` and `agent-time:` only and "the same after a refresh" for both fields. The repository tie-break in `merge_record` (`engine/place.c`) is reached only when `time:` ties too, and a refresh writes `order:` one past the highest (`publish_session_key`), so its repository is the latest by design; only the agent (kept by agent order and agent time) must hold. One name pair alone can pass without the tie-break when the folder lists the winning key first.
- [x] After the tie, refresh the session without naming an agent (`helio inbox --session`) in each repository in turn. Done when: after **each** refresh the command succeeds, the read session names the refreshed repository and the winning agent, and the refreshed key's stored `order:` rose (so a refresh that wrote nothing cannot pass, also in the repository that already won). Cheaper substitute: one read after all refreshes.
  **Corrected 2026-11-27:** split from the first item and made per refresh with a stored-order check (plan review finding 3, `docs/reviews/01-heliograph/D01-T01-s18.md`); the two mutations split per field (finding 2); listing order observed rather than assumed (finding 1); checkpoint names the real mutations (finding 4).
- [x] Prove each tie-break separately: build once with the repository-name comparison removed from the `latest` choice in `merge_record` of `engine/place.c`, and once with it removed from the agent choice only, and run the case on each. Done when: the first fails on its repository assertion, the second on its agent assertion, and the case passes on the section's commit, all three results in the commit body.
- [x] Check the exit code of `--list-sessions` before its rows are parsed in `test_a_merged_session_never_lends_another_keys_time_to_a_repository` of `tests/test_helio.py`. Done when: the test asserts exit 0 and fails when the racer is made to return 3 after printing.
- [x] Commit: `tests: an exact agent tie and the session listing are held to their result`

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0 on the section's commit; the new case fails with the repository comparison removed from the `latest` choice and, separately, with it removed from the agent choice in `merge_record`; and the listing case fails when `--list-sessions` in `tests/racer.c` returns 3 after printing its rows.
**Corrected 2026-11-27:** said the new tests fail "when their production change is reverted", but §18 changes tests only (plan review finding 4).

> **Verified:** 2026-11-27 | §18 | a0dcccdd758e5d89c269f5c4cd0a88ccba6ca8bb
> **Review:** `docs/reviews/d01-t01-s18.json`

## 19. Archive paths opened without following a swapped folder

> **Started:** 2026-11-27T09:25:00Z

Filed 2026-11-27 from the Astra review round 2 of `b9388b0` (P2, pre-existing, recorded with the `a9d9505` fix for the archive folder created meanwhile). `check_arch_dirs` in `engine/archive.c` checks `archive/` and `archive/<month>/` once, and some entry points skip it (`archive_read_landed_one` reads by path directly, `archive_check` starts by listing `archive/`), then every reader, writer and lister of archive files (`archive_read_message`, `archive_read_job_ex`, `archive_write_seen`, the copy in `copy_and_unlink_post`, `archive_promote_landed` for `helio landed`, `archive_every` and `archive_check` among them) builds the path again with `arch_path` or `arch_dir` and opens it. The scope is that mechanism, not a list of callers: no filesystem operation under `archive/` (creating the month folder with `p_folder`, opening, listing, renaming or removing) acts on a rebuilt full path; each is done relative to a held `archive/` or `archive/<month>/` folder. A program with write access to the store that swaps either folder for a symlink between the check and the open sends the read or the write outside the store, because `lstat` and `O_NOFOLLOW` in `p_read` of `src/plat_linux.c` guard only the last component, and `GetFileAttributesW` in `src/plat_windows.c` checks only the folder it names.

**Phase:** 2
**Needs:** wsl-windows-interop
**Surface:** no

**Scope:** containment. A program with write access to the store can already change or remove any file in it, so the guarantee is that no read, write, listing or folder creation under `archive/` follows a symlink, junction or other reparse point out of the store folder, and that nothing is reported as written when it was not. On Windows a held folder cannot be moved, so each operation stays in the store. On Linux a folder moved elsewhere while one operation holds it keeps that operation's write wherever the mover put it: the mover already holds those files and chose the place, and the next operation opens the path afresh. **Corrected 2026-11-27 (round 1, finding 4):** said no operation "reaches outside the store folder", which a held Linux descriptor cannot promise against a folder renamed out of the store while held. Keeping a job reachable at `archive/<month>/` against such a writer moving folders is not promised (it could delete the job just as well). **Decision 2026-11-27:** taken as the industry-standard boundary for a store shared by trusted local programs (plan review findings 1 and 2, `docs/reviews/01-heliograph/D01-T01-s19.md`); reversing it means a journal for every archive move and seen mark, a new section of its own, cost high. The held folder lives in Heliograph's platform layer beside its other store primitives: every Windows call it needs is already tier 0 in Impossible SDK's `abi/tiers.json`, so nothing is asked of the SDK (moving it there later: medium).

**Build order:**

1. Open archive files relative to a held folder on Linux. Done when: a folder swapped for a symlink after the check is never followed: the read uses the original held folder or is refused.
2. Open archive files relative to a held folder handle on Windows. Done when: a folder swapped for a reparse point after the check is never followed: the read uses the original held folder or is refused.
3. Prove both with a paused swap. Done when: the regression fails on the path-based opens and passes on the held ones.

- [x] Add a held folder to `engine/plat.h`: `p_dir`, opened by `p_dir_open(root, rel, &dir)` for `archive` or `archive/<month>` under the store folder, closed by `p_dir_close`, with `p_dir_read`, `p_dir_write`, `p_dir_remove`, `p_dir_sync`, `p_dir_list` and `p_dir_folder` (a folder made inside it) acting on a name inside it. Linux: each folder of `rel` opened from the one before with `openat(..., O_DIRECTORY | O_NOFOLLOW)` starting at the store folder, then every operation through `openat` (with `O_NOFOLLOW`), `renameat`, `linkat`, `unlinkat`, `mkdirat` and `fstatat` on the held descriptor, the path calls becoming the same code at `AT_FDCWD`. Windows: each folder of `rel` opened with `FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT`, `FILE_LIST_DIRECTORY` access and no `FILE_SHARE_DELETE`, so it cannot be renamed or removed while held, and refused when it is a reparse point; each file inside it is opened with `FILE_FLAG_OPEN_REPARSE_POINT` and without `FILE_SHARE_DELETE`, refused when it is a reparse point, and read, flushed or checked through that handle (Heliograph's own calls in `src/plat_windows.c`, not the SDK's path-based `im_read`). Done when: `src/plat_linux.c` and `src/plat_windows.c` both build it, a symlink (Linux) or junction or file symlink (Windows) as either folder or as the final file returns `P_FAILED`, and a missing one `P_MISSING`.
  **Corrected 2026-11-27 (build):** named `openat2` with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS` and also `p_dir_create_excl`, `p_dir_claim_remove` and `p_dir_mtime`. The per-folder `O_NOFOLLOW` walk refuses a link in any folder of `rel` as `openat2` would, on every kernel and with no fallback to keep; the archive creates, renames away and reads the times of live and summary files only, so those three calls had no caller and were not built.
  **Corrected 2026-11-27:** said "opens `archive/<month>/` as a held folder (`O_DIRECTORY | O_NOFOLLOW` per component on Linux, `FILE_FLAG_OPEN_REPARSE_POINT` with a reparse-point refusal on Windows) and reads, writes and lists names inside it". The operations are named; `openat2` refuses a symlink anywhere below the store in one call; the Windows hold denies `FILE_SHARE_DELETE` (without which a held handle does not stop a rename) and covers the final file, which `im_read` opens by path following a reparse point (plan review finding 3). Over `\\wsl.localhost` a Linux symlink is a reparse point Windows cannot traverse (probed 2026-11-27: attributes `0x400`, reads `ENOENT`), so the Windows hold matters on a local NTFS store.
- [x] Replace `arch_path` and `arch_dir` in `engine/archive.c` with names inside a held folder, so every archive read, write, copy, promotion, seen mark and listing (`helio show`, `seen`, `archive`, `landed`, `log --all`, `check`), including the listings of `archive/` itself in `archive_every` and `archive_check`, goes through it. Done when: `grep -nw 'arch_path\|arch_dir' engine/archive.c` prints nothing, `archive/` is listed only through `p_dir_list`, a missing month folder is made with `p_dir_folder` in the held `archive/` folder, and the build passes.
- [x] Add the pauses where the engine holds and uses a folder, named in `tests/racer.c`: `arch-held` after `p_dir_open` of the month folder and before its first read, `arch-root-held` after `p_dir_open` of `archive/` and before it is listed, `before-arch-copy`, `before-landed-promote` and `before-arch-seen` before those writes, and `before-make-month` between making `archive/` and the month folder in `ensure_archive_month`. Done when: each is reached by the command that uses it, shown by its `paused.<stage>` file, in the tests below.
- [x] Test the read (`tests/test_helio.py`, `ArchiveSwapLinux`): archive a job, pause `show` of it at `arch-held`, swap `archive/` (and, separately, `archive/<month>/`) for a symlink to an outside folder holding a valid post of the same id with another subject, resume, with Linux inotify (`OutsideWatch`) on the outside folders. Done when: no open, listing or read of anything outside is seen, `show` exits 0 or 3 and never prints the planted subject, and a fresh `show` after the swap is undone prints the original. Cheaper substitute: a swap of `archive/<month>/` only, or exit 3 without the watch.
  **Corrected 2026-11-27 (build):** required exit 0. A folder is held for one operation (each `arch_*` call in `engine/archive.c`), so after a swap the next operation of the same command is refused and `show` exits 3; containment (**Scope**) promises refusal, not completion. The watch proves what exit 0 was meant to: the planted post is never opened, which exit 3 alone could not show (plan review finding 6).
- [x] Test the root listings: pause `log --all` and `check` at `arch-root-held`, swap `archive/` for a symlink to an outside folder holding a distinguishable month with a valid job, resume; and the same commands with no swap as a control; on Windows a junction in place of `archive/` with a month only the outside folder has. Done when: no listing outside is seen by the watch, neither prints the planted job or month, both control runs succeed (`log --all` listing the real job), and `check`'s root listing reverted to a path listing fails on Windows.
  **Corrected 2026-11-27 (build):** said each listing reverted alone fails its test. On Linux a path listing of `archive` already opens its one folder below the store with `O_NOFOLLOW` and refuses the link, so no Linux test can tell the two apart; on Windows `check` names the months it lists and fails on the outside month, but `log --all` only counts the months it then refuses, so its path listing learns outside names without printing any and no test observes it. That one stays proved by the code (`arch_list` in `archive_every`) and the review.
- [x] Test the writes: pause `helio archive` at `before-arch-copy`, `helio landed` at `before-landed-promote`, `seen` of an archived message at `before-arch-seen`, swap the month folder for a symlink to an outside folder, resume. Done when: no file or folder outside the store is read, written, listed or created, each command either exits 0 with its file read back in a fresh command from the held folder (the folder renamed away) or exits 3 with the live source kept, and a retry after the symlink is removed succeeds (for a refused landed promotion, the next `helio archive`, which carries a live landed mark of an archived job across; `landed` itself refuses an archived job).
- [x] Test the month creation: archive the first job of a month with no folder yet, pause at `before-make-month`, swap `archive/` for a symlink to an outside folder, resume. Done when: the month folder never appears outside the store, and the job is in the store, live or in the held folder.
- [x] Prove each test catches its bug: build with the held opens replaced by the old path opens (the same pauses kept) and run the tests. Done when: each fails on its behavioral assertion after reaching its pause and resuming, and passes on the section's commit, both results in the commit body.
  **Corrected 2026-11-27:** said each test "fails on the parent commit", which has none of the pauses and would only time out (plan review finding 6); the read test now requires the original message (exit 0) instead of "or exits 3", which could not show the planted post was never read; root listings and `seen` are covered (findings 2 and 4); the `p_dir_same` item added earlier today is struck, out of scope under **Scope** (finding 1).
- [x] Prove the Windows hold on a local NTFS store (the store in the Windows temporary folder): a junction already in place of `archive/` or `archive/<month>/`, and a file symlink in place of the post, are refused; a rename of the held folder attempted while a command is paused gets a sharing violation, the command resumes and exits 0, and the rename succeeds after it ends. Done when: these tests pass on Windows through `prove.py` run on the committed candidate and on `windows-latest` in the `Build` run of that same pushed commit.
  **Corrected 2026-11-27:** said "through `prove.py` with a junction in place of `archive/`"; `prove.py` runs Windows against the WSL disk, where a junction cannot be made, and builds only committed work, so the tests use an NTFS temporary store and run after the commit (findings 5 and 7).
- [x] Commit: `archive: a folder swapped after its check is never followed`

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0 on the section's commit; each swap test fails with the held opens replaced by path opens; `python3 ../SDK/tools/prove.py .` prints `proved on Windows` for that commit and its `Build` run is green on both hosts.

> **Verified:** 2026-11-27 | §19 | 4116baf0aaeb58a5fbfcdb669150c3922f7e362a
> **Review:** `docs/reviews/d01-t01-s19.json`

## 20. Windows launches from WSL that survive an interop timeout

> **Started:** 2026-11-27T11:50:00Z

Filed 2026-11-27 from the `D01 T01 §18` and `§19` gate runs: 3 of 6 `python3 ../SDK/tools/app_build.py .` runs that day failed only because a Windows program started from WSL never ran, its stderr `<3>WSL (<pid> - ) ERROR: UtilAcceptVsock:281: accept4 failed 110` (WSL's interop relay timing out, `ETIMEDOUT`, under host load with several agent runs). The launches are `_run` and `_run_win_cli` in `tools/phase_install.py`, `run_windows_from_wsl` in `tools/install.py`, and the `python.exe` calls in `tests/test_install.py`. The same failure hits `tools/install.py --phase` on the operator's machine, where it rolls a phase back for a reason that is not the build. The fix owns one launcher that recognizes exactly that failure and starts the program again, and only for a call that is safe to repeat.

**Phase:** 2
**Needs:** wsl-windows-interop
**Surface:** no

**Decision 2026-11-27 (plan review finding 4):** the launcher is Heliograph's (`tools/win_launch.py`), because which calls may be repeated is decided by the idempotence of Heliograph's own installer steps, and no Impossible SDK tool has failed on the relay (every `prove.py` run on 2026-11-27 passed). `relay_timed_out` is the part a second program could use; it moves to Impossible SDK when one needs it (cost: low, one module and its tests). Coverage is the WSL-to-Windows boundary: a Windows program started by Python running in WSL. A child started by a Windows Python (`cmd.exe` inside a `-c` helper) never crosses the relay and is out of scope.

**Build order:**

1. One launcher for Windows programs from WSL. Done when: every launch in `tools/` goes through it.
2. Repeat only the interop failure. Done when: a launch whose stderr is the relay timeout and nothing else is started again, at most 3 times, and any other failure is returned at once.
3. The tests use it too. Done when: no Windows program started from WSL in `tests/test_install.py` bypasses it, and one-shot fixture steps stay single-start.

- [x] Add `tools/win_launch.py`: `relay_timed_out(result)` true only when the exit code is 1, stdout is empty and stderr, decoded when bytes, is exactly one line matching `^<3>WSL \(\d+ - \S*\) ERROR: UtilAcceptVsock:\d+: accept4 failed 110\s*$`; and `run_windows(argv, *, repeatable, check=False, forward=False, run=subprocess.run, sleep=time.sleep, **run_args)`, which always captures both streams, starts the program again after 0.5 s, 1 s and 2 s only while `repeatable` and the result matches, then applies `check` (raising `CalledProcessError` with the last output) and, with `forward`, writes the last stdout and stderr to its own streams once. Done when: it is loaded by file path (`importlib.util.spec_from_file_location` from `Path(__file__)`) by `tools/install.py` and `tools/phase_install.py`, works from an unrelated working directory, and keeps bytes or text as `run_args` ask. Cheaper substitute: repeating every failed launch.
- [x] Test the predicate and the timing in `tests/test_install.py` with an injected `run` and `sleep`. Done when: two relay results then success give 3 starts and sleeps `[0.5, 1]`; four relay results give 4 starts, sleeps `[0.5, 1, 2]` and the last exit code and both streams; one start each for a relay line with nonempty stdout, with exit code 2, with text before or after the line, for any other stderr, and with `repeatable=False`; no sleep after success, refusal or exhaustion; `check=True` raises only after the repeats with the last output; matching works for bytes and text.
- [x] Route the Windows launches of `tools/phase_install.py` through `run_windows` with `repeatable` given: true for `_probe_win_state`, every `_win_phase_marker_call` action (`begin`, `end`, `recover`, `snapshot_prev`, `restore_prev`, each idempotent for one transaction id in `tools/win_install.py`, the same stdin passed on each start), and `helio check` and `helio show` through `_run_clean_win_helio`; false for the smoke `helio ask`, `_run_win_cli` installs, `--back`, `--remove` and the Windows unittest run against the snapshot (a whole suite sets up and changes state). Done when: no `subprocess.run` in `tools/phase_install.py` starts `python.exe` outside `run_windows`.
- [x] Route `run_windows_from_wsl` in `tools/install.py` the same way: `--status` repeatable with `forward=True` so the operator sees its report once, on the stream it was written to, and its exit code; `--back`, `--remove` and the install single-start, also forwarded. Done when: an operator's `install.py --windows --status` prints the same as before.
- [x] Test the policy through the real call paths in `tests/test_install.py`: inject the relay result at the `run` seam for `_probe_win_state`, each marker action, `_run_clean_win_helio` (`check`, `show`, the smoke `ask`), `_run_win_cli` (install, back, remove) and `run_windows_from_wsl` (`--status`, install). Done when: each repeatable call starts the number of times its injected results require and each other call starts once and returns the relay failure, a retried `restore_prev` gets the identical stdin each time, and `--status` prints its final report exactly once; reversing any one call's `repeatable` fails a test.
- [x] Route the direct Windows launches from WSL of `tests/test_install.py` through `run_windows` (not those of `TestWindowsInstaller`, which runs on Windows, where a child never crosses the relay): probes and reads repeatable; fixture steps with a stable identity (the Windows temporary folder named from the test's id, made with `exist_ok`) repeatable; every other state-changing step (crash injection, planted files, installs) `repeatable=False`. The `Popen` launches that hold a Windows file or a held program alive (`test_held_windows_exe_concurrent_caller_during_publication`) keep their own lifetime and readiness checks and are listed in a comment as single-start. Done when: a scan of the file's `subprocess.run` and `check_output` calls finds no Windows program started from WSL outside `run_windows`, and each call names `repeatable`.
- [x] Ship the module with the installer: every fixture that copies `tools/install.py` into a standalone checkout copies `tools/win_launch.py` too. Done when: the standalone installer tests pass and a fixture without the module fails with a message that names it.
  **Corrected 2026-11-27 (build):** the repeat policy lives in the launching functions, not at their call sites: `_run_clean_win_helio` repeats `check` and `show` only, `_run_win_cli` `--status` only, and the snapshot unittest run is `_run_snapshot_proof`, started once, so each policy is one place a test can reach. The two Windows temporary folders the tests made with `mkdtemp` now take a name made in WSL and `exist_ok`, so they are repeatable.
  **Corrected 2026-11-27 (plan review, `docs/reviews/01-heliograph/D01-T01-s20.md`):** findings 1 to 9 folded in: forwarding for `--status` (1), `check` applied after the repeats and bytes or text kept (2), one-shot test fixtures single-start unless given a stable identity (3), the SDK decision and the snapshot proof classified (4), coverage defined at the WSL-to-Windows boundary with `Popen` named (5), policy tested through the real call paths (6), the exact predicate tested (7), loading by file path and shipping with fixtures (8), delays recorded (9).
- [x] Commit: `install: a Windows launch the WSL relay timed out is started again`

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0; the predicate, timing and policy tests fail with the repeat removed and with any one call's `repeatable` reversed.

> **Verified:** 2026-11-27 | §20 | 12e35c188b362c11a192602b8895bb447eacad13
> **Review:** `docs/reviews/d01-t01-s20.json`

## 21. JSON for the inbox, log, show and check

Filed 2026-11-27 from the `D01 T01 §10` plan review (findings 2, 3, 4 for `check`, 8 for the job fields, 11). A script reading the inbox must see what a person sees, no more and no less, and learn what it did not get.

**Phase:** 3
**Needs:** none
**Surface:** no

**Build order:**

1. A message object. Done when: every stored field and the job fields read back as stored.
2. The four reading commands. Done when: each returns exactly the rows a person would see and acknowledges the same way.

- [ ] Write a message as `{"id", "time", "from", "to", "for_session", "repository", "kind", "about", "urgency", "re", "due", "subject", "body", "archived"}` in `engine/helio.c` (`due` the stored time text or `null`), and for a request or handoff add `"state"`, `"taker"` (the lane that took it, `null` when open or refused without a take), `"closed_by"` (the lane that ended it, or `null`), `"session"` and `"landed"` (every commit, normalized). Done when: a test reads each field equal to the stored post and steps, for open, taken, released, done, refused-without-take and archived jobs with three landed commits. Cheaper substitute: the inbox line as a string.
- [ ] Give the reader's state of a message that is not a job: `"seen"` (whether this reader marked it) in the inbox, `show` and `log`, and in the inbox `"overdue"` for a job and `"silent"` for a job whose taker stopped or a session-addressed heads-up whose target went silent (`"silent_target"` naming it). Done when: a target and an owner each see their own `seen` and `silent` before and after the target reads it.
- [ ] Print the inbox as `{"messages": [...], "held": <count>, "more": <bool>}` under the same display budget and urgent-first order as the text inbox, a body over the budget cut with `"body_cut": true` and read whole with `show`, and `--all` without the budget. Done when: six waiting messages give the budgeted ones and `held` for the rest, one 16 KiB body comes back cut and whole through `show`, and an inbox with messages never exits 1.
- [ ] Print `log` as `{"messages": [...]}` and `show` as one message through the same visibility and seen-mark paths as the text forms. Done when: with two projects in one store and two sessions in one lane, the exact ids returned match the text forms, a message outside the reader's view is refused the same as a missing one, an id live and archived at once appears once, and `show` marks it seen for the next inbox.
- [ ] Print `check` as `{"ok": <bool>, "problems": [<text>...]}`: exit 0 and an empty list for a clean store, 3 with every problem for a damaged one. Done when: a test damages two files and gets both problems and exit 3, with stderr empty.
- [ ] Commit: `output: JSON for reading`

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0 and the tests fail with any field, the budget or the visibility path removed.

## 22. JSON for sending, job changes, archive and working

Filed 2026-11-27 from the `D01 T01 §10` plan review (findings 4 for `archive`, 5 for `working`, 8). Every change a script makes is answered with what the store now holds, not an echo of its words.

**Phase:** 3
**Needs:** none
**Surface:** no

**Build order:**

1. The send and job-change replies. Done when: each value is read back from the store after the change.
2. `archive` and `working`. Done when: their partial and overlap results are one value.

- [ ] Reply `{"id": <id>}` for `ask`, `hand`, `tell`, `find` and `answer` in `engine/helio.c`, the id of the stored message. Done when: a keyed retry returns the first id and stores nothing new, and an answer's stored `re` is the asked id.
- [ ] Reply `{"id", "state", "taker", "closed_by"}` for `take`, `release`, `done` and `refuse`, read from the job after the change. Done when: release reads back `open` with `taker` `null`, a refused transition exits 2 with the earlier state left and shown in the error value, and each reply equals a following `show --json`.
- [ ] Reply `{"id", "commit", "landed"}` for `landed`, the normalized commit and every commit now on the job. Done when: an uppercase commit comes back lowercase and a third commit returns all three, live and archived.
- [ ] Reply `{"agent", "session"}` for `hello` and `{"store"}` for `init`, from what was stored. Done when: each equals the stored session record and the store folder.
- [ ] Reply `{"lane", "paths", "overlaps": [{"lane", "path"}...]}` for `working`, keeping the note written to the other lane. Done when: two lanes declaring the same path both learn of it, and the second's reply names the first.
- [ ] Reply `{"archived": <count>, "problems": [<text>...]}` for `archive`: exit 0 with moves, 1 with none, 3 with any problem and the count of what did move. Done when: a batch with one damaged job reports the others' count equal to the files moved on disk, the problem, exit 3 and empty stderr.
- [ ] Commit: `output: JSON for changes`

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0 and the tests fail with any reply replaced by an echo of the call's words.

## 23. Summary command read from the day's snapshot

Filed 2026-11-27 from the `D01 T01 §10` plan review (findings 5 for the overlap count, 6). The design lists `helio summary` and a daily overlap count (design, "How does it watch?"); `summary/<day>.md` is written by the first inbox of the day and has no overlap count.

**Phase:** 3
**Needs:** none
**Surface:** no

**Build order:**

1. The overlap count in the daily file. Done when: a known overlap is counted.
2. The command. Done when: it prints the day's saved snapshot, never a recount.

- [ ] Add an "Overlaps" part to `watch_summary` in `engine/watch.c`, from the `working` records live that day. Done when: two lanes on one path give an overlap count of 1 in `summary/<day>.md`.
- [ ] Add `summary` to the commands in `engine/helio.c`: it writes the day's file when it is the first call of the day, as the inbox does, then prints the saved file as text, or with `--json` `{"day", "at", "overdue", "silent", "open", "taken", "done", "refused", "overlaps", "damaged"}` parsed from it. Done when: after a job closes later in the day, `summary` still prints the morning's counts; two first calls racing leave one file and both print it.
- [ ] Refuse a snapshot that cannot be read or is cut short with exit 3 and the error value, and report a day whose file was deferred (a liveness read failed) as exit 1 `{"day", "deferred": true}`. Done when: a truncated file gives exit 3 and a deferred day exit 1.
- [ ] List `summary` and `--json` in the help of `engine/helio.c` and the command reference in `README.md`. Done when: both name them.
- [ ] Commit: `summary: the day's snapshot as a command`

**Test checkpoint:** `python3 ../SDK/tools/app_build.py .` exits 0 and the tests fail when `summary` recounts the store instead of reading the file.

## Verification

- A job is sent on Windows, taken in WSL with `helio take --kind request --lane neon-a --json`, marked landed, done, archived and shown again by its id.
- `python3 ../SDK/tools/prove.py .` exits 0 after the last commit.
- `python3 scripts/todo-graph.py validate` and `python3 scripts/todo-graph.py plan --check` pass.
