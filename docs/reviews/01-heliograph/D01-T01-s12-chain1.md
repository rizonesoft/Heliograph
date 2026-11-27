## Plan review: astra (`gpt-6.2-astra` via `codex`)

- Writer: claude
- Run: 2026-11-27T18:13:38Z
- Candidate: 1043f0c9c9066ad4732784c74dcec46284efe7cd
- Effort: medium
- Lenses: plan
- Lines: 13
- Digest: 8c949602315aef94

1. **P1. §12 lines 381–385 omit the failure path for an incomplete audience or unreadable seen mark.** Path: One lane has read a broadcast, but a sharing violation prevents the archiver from reading another lane’s heartbeat. Treating the partial audience as complete removes the message from the second lane’s inbox. Evidence: traced `engine/place.c:1035` and `engine/place.c:1060`, where enumeration can deliver records and still return `P_FAILED`. `engine/store.c:364` also distinguishes a missing mark from an unreadable or damaged one. Require both archive and summary to validate their inputs before calling the pure predicate. Test that the affected post stays live, archive reports damage, the daily snapshot is deferred on a liveness read failure, and retry after recovery uses the full audience.
2. **P1. §12 lines 369 and 382 lack a production test for repository-specific heartbeat history.** Path: A session works in repository A after a broadcast scoped to A, then announces itself in B without reading the message. Another lane in A has read it. Evidence: traced `engine/helio.c:733`, which creates repository-scoped broadcasts, and `engine/place.c:1108`, where the general session listing merges a named session into one row. The repository-aware enumeration at `engine/place.c:1041` can retrieve its record in A. Require the archive and summary callers to use that history. Test that the unread lane still holds the message after moving to B, while a lane active only in B does not hold it.
3. **P1. §12 lines 378 and 384 do not define recovery when the broadcast audience changes during an interrupted move.** Path: An archiver stops after copying the post. Before retry, a previously inactive lane announces itself and becomes an unread recipient while the post is still live. Evidence: traced `engine/archive.c:940` for the post-copy pause and `engine/archive.c:723` and `engine/archive.c:821` for the existing-copy path that can proceed directly to unlink. A valid archive post alone cannot establish that the current audience has read it. Define when the expiry decision becomes final and test this interleaving through `hello`, `archive`, and the returning lane’s inbox. The recovery test must distinguish a partial copy from a completed move followed by a killed keyed retry.
4. **P1. §12 lines 381–383 allow the reader-identity tests to pass with the wrong recipient.** Path: An owner opens an alert for a silent named session, or one named session reads a lane-wide message. The owner acknowledgement must leave the targeted message live, while the named reader must satisfy the lane-wide delivery. Evidence: traced `engine/helio.c:596` and `engine/helio.c:652` for the owner alert, and `engine/store.c:332` for the distinct bare-lane, session-hash and alert filenames. Require paired held/ready assertions on the same message using real persisted marks. Cover the exact target session, a sibling or bare-lane mark, the owner alert, and a named reader of a lane-wide message. Getting three different answers across three unrelated racer fixtures does not prove those distinctions.
5. **P2. §12 lines 381, 383 and 385 leave the exact expiry boundary untested and disagree about the summary at that boundary.** Path: A daily archive runs exactly 30 days after sending, or the host clock steps behind the message timestamp. Evidence: traced `engine/archive.c:1020`, where the sweep samples its clock. The day-before/day-after proof accepts a strict `> 30 days` implementation, and unsigned subtraction can age a future post immediately. Test a fixed clock one tick before, exactly at, and one tick after 30 days, plus a future ID. At exactly 30 days, a read message must move and an unread one must appear in `Held unseen`. Exercise heads-ups, findings and answers so a kind-specific omission fails.
6. **P2. §12 lines 378 and 383–388 do not prove the final store contents after recovery or a racing read.** Path: A reader creates a second seen mark while an archiver is copying marks, or a keyed sender retries while the post is moving. Evidence: traced `engine/archive.c:1080` for the late-mark sweep, `engine/helio.c:635` for the reader’s archive fallback, and `engine/store.c:753` for keyed publication. `helio check` validates the files that remain. It can pass after a mark disappears from both locations. Require a non-job race and restart test that compares the expected post and every acknowledged mark byte for byte, finds no live duplicate, and verifies the original keyed ID and exactly one `log --all` row. Exercise the daily inbox trigger as well as manual `archive`.
7. **P2. §12 lines 379 and 385 prove one listed item, but never prove the held count or its read-back.** Path: The first inbox of the day encounters held, ready and young messages together. An implementation that lists all three or always prints a count of one passes the stated example. Evidence: traced `engine/watch.c:290` for the separate count and bounded detail list, and `engine/watch.c:367` for snapshot serialization. Require exact IDs and counts for zero and multiple held items, exclusion of ready and young items, and read-back of the same snapshot after a later acknowledgement. Add the held count to the JSON snapshot contract in §23 line 701, whose explicit field list currently omits it.
8. **P3. §12 line 384 needs a negative recovery rule for job-only events.** An archived non-job can justify sweeping a valid late seen mark. It cannot justify sweeping a job step or landed mark. The current remnant check at `engine/archive.c:245` accepts a step index at or beyond the archived link count, which includes every index for a non-job with zero links. Require `check` and `archive` to report those events as damage and preserve them. The proposed orphan test covers only a missing archived post.

LENS plan: findings  
HIGHEST: P1  
PLAN: gaps  
OVERALL: needs-attention

- Planned: incomplete audience or unreadable seen mark is damage, not a decision (gap 1) in e25cf64
- Planned: repository-scoped heartbeats for a repository message (gap 2) in e25cf64
- Planned: decision re-made on each attempt, final only when the live post is removed (gap 3) in e25cf64
- Planned: reader identity proven with real marks on one message (gap 4) in e25cf64
- Planned: exact 30-day boundary, future id and every kind (gap 5) in e25cf64
- Planned: race and restart contents compared byte for byte, keyed id and one log row, daily trigger (gap 6) in e25cf64
- Planned: exact held ids and counts with read-back, and held_unseen in D01 T01 §23 (gap 7) in e25cf64
- Planned: a step or landed mark beside an archived non-job is damage (gap 8) in e25cf64

## Round 1: astra (`gpt-6.2-astra` via `codex`)

- Writer: claude
- Run: 2026-11-27T18:54:47Z
- Candidate: 195afb8f28996946eccf914a8fb5d0f401266a97
- Effort: medium
- Lenses: adversarial, integration, source-defect, consistency, quality, optimization
- Lines: 35
- Digest: 1fe1468dd3d4860f

### adversarial: needs-attention

1. **P1** `engine/archive.c:871` removes a broadcast after its audience has changed. **Path:** A lane returns while another process is archiving a 30-day-old `os/all` message. **Evidence:** Traced. Pause `archive` at `before-remove-post` after `os/apps` has read the message. Run `hello codex --as os/ui` to completion, then resume the same archiver. It unlinks the post using the heartbeat decision made at `engine/archive.c:832`. The unread message disappears from `os/ui`’s inbox. The test at `tests/test_helio.py:2810` starts a fresh archive attempt, which hides this interleaving.

### integration: needs-attention

2. **P2** `engine/archive.c:832` can resurrect a completed keyed message as permanently held unseen. **Path:** A sender retries a keyed message during archival and its process stops after publishing a replacement live post. **Evidence:** Traced. Pause the retry at `post-write` (`engine/store.c:767`), let the archive finish, resume the retry, and stop it at `after-post-write` (`engine/store.c:771`). The original post and its acknowledgements are archived, but the replacement live post has zero live marks. Every subsequent archive attempt examines only live marks and returns held at `engine/archive.c:838`. The daily summary counts a message its recipient already read.
3. **P2** `engine/watch.c:314` can freeze a successfully archived message into the day's `Held unseen` count. **Path:** The first inbox of the day overlaps a manual archive. **Evidence:** Traced. Let the summary load an eligible live post at `engine/watch.c:375`, then let the archiver move the post and its only seen mark before the summary reads that mark. `P_MISSING` drops the mark, the empty set yields held, and `engine/watch.c:431` saves the incorrect count for the day.

### source-defect: needs-attention

4. **P2** `engine/watch.c:406` omits held IDs after the hundredth. **Path:** A backlog contains 101 lane-addressed messages older than 30 days, all unread. **Evidence:** Traced. Run the first inbox of the day. The heading reports 101, but `engine/watch.c:340` emits only 100 IDs and `engine/watch.c:426` substitutes `helio log` for the rest. That command does not identify which messages were held in the saved snapshot. The section explicitly requires the held IDs and their count at `todo/01-heliograph/TODO-01-heliograph.md:389`.

### consistency: needs-attention

5. **P3** `README.md:61` exceeds the repository's 120-character Markdown table-row limit. Reading the row and counting its characters returned 209.
6. **P3** `engine/watch.c:418` attributes an unreadable seen mark to a session or threshold failure. Give an aged direct message a damaged seen mark and open the first inbox of the day. The read fails at `engine/watch.c:315`, but the diagnostic directs the operator to the wrong files.

### quality: needs-attention

7. **P3** `tests/racer.c:569` silently discards the rest of the fixture when either array reaches 64 entries. **Evidence:** Ran `build/linux/linux/racer --expiry headsup os/main os/all - 1800000000000000 1802592000000000` with repeated `seen.os+apps` arguments followed by `life:os/ui@1800000000000001`. With 63 marks it printed `held`. With 64 it printed `ready` because it skipped the unread lane. Both calls exited successfully.
8. **P3** `tests/test_helio.py:2842` runs a second archive before checking the late mark. Remove the first run's post-unlink sweep and the test can still pass because the second run repairs the store. Check the files immediately after the first process exits to exercise the sweep the test claims to cover.

### optimization: advisory

9. **P3** `engine/archive.c:861` reads each seen file solely to discard its contents before `copy_durable` reads it again at `engine/archive.c:345`. For a successful move with 100 marks, that probe adds 100 file reads between the audience decision and post removal. This is a count of redundant operations from the call path. No wall-clock target was verified.

LENS adversarial: findings  
LENS integration: findings  
LENS source-defect: findings  
LENS consistency: findings  
LENS quality: findings  
LENS optimization: findings  
HIGHEST: P1  
OVERALL: needs-attention

## Completion review: astra (`gpt-6.2-astra` via `codex`)

- Writer: claude
- Run: 2026-11-27T19:12:08Z
- Candidate: 4d576fc47ddedcc96178ebd242982a21424ccf69
- Effort: low
- Lenses: plan-delivery, record
- Lines: 29
- Digest: 39a1276f44d5df7c

## Part 1. Completeness

1. **present**. The expiry tests cover heads-ups, findings and answers one tick before, exactly at and one tick after 30 days, plus a future timestamp. `tests/test_helio.py:2623`
2. **present**. The reader and audience tests distinguish an owner alert, a sibling session, the intended session and a lane-wide read. They also cover an unread broadcast and a session that moves repositories. `tests/test_helio.py:2643`, `:2673`, `:2689`
3. **missing-in-scope, P2**. An old broadcast with no qualifying seen mark skips heartbeat validation. **Path:** The first inbox encounters an unread broadcast while a heartbeat file is locked. **Evidence:** Traced `engine/archive.c:844`. Enumeration runs only when the marks alone yield `EXPIRY_READY`. With zero reads, the function returns held without discovering the failure, so the summary can be saved when §12 requires deferral. The existing failure test gives the broadcast a reader first at `tests/test_helio.py:2717`.
4. **present**. Manual archive and the daily inbox trigger move eligible messages. The ordinary move test compares the archived bytes and exercises `show`, `answer` and keyed retry. `tests/test_helio.py:2740`, `:2761`
5. **missing-in-scope, P1**. A returning lane can still lose an unread broadcast from its inbox. **Path:** After the archiver finishes its final audience read, the scheduler runs another lane's `hello` to completion while the post is still live. The archiver resumes and removes it. **Evidence:** Traced `engine/archive.c:934` and `:941`. The removal claims only the post at `engine/archive.c:751`. The regression pauses at `:930`, before the final read, so it cannot detect a heartbeat published after that read. §12 explicitly makes live-post removal the decision boundary.
6. **present**. The interruption test stops the mover at seven stages, runs `check` before retry and compares the recovered files. The orphan test verifies that damage is named and the invalid step survives inspection. `tests/test_helio.py:2785`, `:2898`
7. **missing-in-scope, P2**. The concurrent read and keyed-retry test omits the promised byte comparison for the post. **Path:** A named reader acknowledges a post and its sender retries the key while archival is paused. **Evidence:** Traced `tests/test_helio.py:2866` and `:2890`. The test checks both mark payloads, the keyed ID, the empty live store and one log row. A changed but parseable post body can satisfy every assertion. The byte comparison at `:2749` exercises a different, unraced move.
8. **present**. The daily summary tests verify zero, two and 101 held IDs, exclusion of young and ready messages, continued inbox visibility and byte-identical snapshot read-back after acknowledgement. `tests/test_helio.py:2858`, `:2939`
9. **present**. Commit `195afb8` records the production-revert checks, and `4d576fc` records three regressions failing on its parent. The two 171-test gates are supplied exact-tip evidence. I inspected the candidate read-only and did not rerun those gates. The open stamp items are expected. The record finding concerns the checked-off guarantees in items 3, 5 and 7.

## Part 2. Excellence

The section names **age-only expiry** as its alternative. It removes a message at the deadline even if a recipient still needs it. Heliograph improves on that default, but the scheduled delivery and record lenses expose two cases where its promised advantage is incomplete.

1. **in-scope, P1**. A heartbeat completed while the broadcast is live must preserve inbox delivery. Target a regression that completes `hello` between the final audience read and removal, then finds the post in that lane's inbox.
2. **in-scope, P2**. An unread broadcast must produce the promised recovery signal when audience data is unavailable. Target archive exit 3 naming the ID, no saved daily summary and a successful decision after repair, including when there are zero qualifying reads.

## Gap list

1. **missing-in-scope / in-scope, P1**. A heartbeat published after the final audience read can leave the returning lane without its unread broadcast.
2. **missing-in-scope / in-scope, P2**. Zero qualifying reads bypass heartbeat validation and allow a daily snapshot despite an unreadable audience.
3. **missing-in-scope, P2**. The race test does not compare the archived post bytes with the original, leaving the checked-off race proof partial.

LENS plan-delivery: findings  
LENS record: findings  
HIGHEST: P1  
COMPLETION: gaps

- Implemented: repaired the returning lane losing its broadcast after the last read before the removal (gap 1) in 9427c06
- Implemented: repaired the unread broadcast skipping the heartbeat read (gap 2) in 9427c06
- Implemented: repaired the race test to compare the archived post bytes (gap 3) in 9427c06

## Reconciliation review: astra (`gpt-6.2-astra` via `codex`)

- Writer: claude
- Run: 2026-11-27T19:27:06Z
- Candidate: ed484581b8eff130f261640da183f183b4422d1d
- Effort: low
- Lenses: adversarial, integration, plan-delivery, record
- Lines: 12
- Digest: 1cce7cdde2806482

1. **open, P1. A lane that returns before removal can still lose its unread broadcast.** Path: The lane completes `hello` while the post is live. The archiver removes the post and is interrupted before it can restore it. Evidence: traced. Removal happens at `engine/archive.c:960`, and the `after-remove-post` pause at `engine/archive.c:966` precedes both the audience check and restoration at `engine/archive.c:970`. On restart, recovery accepts the archived copy at `engine/archive.c:1008` and marks the post removed at `engine/archive.c:1028` without checking the returning lane. The regression waits for the mover to finish at `tests/test_helio.py:2859` before checking delivery. It proves restoration by a surviving mover. The implementation claim at `docs/reviews/01-heliograph/D01-T01-s12.md:120` therefore remains partial.
2. **closed. An unread broadcast now defers the summary when its audience cannot be read.** Audience enumeration runs with zero qualifying marks at `engine/archive.c:844`, and a failed listing returns damage at `engine/archive.c:852`. The regression at `tests/test_helio.py:2872` checks summary deferral, archive exit 3 with the message ID, and the saved held count after heartbeat repair. This supports the implementation record at `docs/reviews/01-heliograph/D01-T01-s12.md:121`.
3. **closed. The concurrent read and keyed-retry test now compares the post bytes.** It captures the live bytes before the race at `tests/test_helio.py:2916` and compares the archived post directly against them at `tests/test_helio.py:2939`. The mark and log checks remain in place. This supports the implementation record at `docs/reviews/01-heliograph/D01-T01-s12.md:122`.

**New finding, P2. A lane that first returns after removal can resurrect a completed broadcast.** Path: An eligible broadcast is removed, then another lane completes `hello` before the archiver’s next audience check. Evidence: traced. The interval starts at `engine/archive.c:966`. The read at `engine/archive.c:967` includes that new heartbeat, and `engine/archive.c:970` restores the post to the live store. This moves the decision past removal, contrary to the unchanged contract at `todo/01-heliograph/TODO-01-heliograph.md:371`. Both findings arise because the post-removal check cannot distinguish which side of removal the heartbeat was published on.

LENS adversarial: findings  
LENS integration: findings  
LENS plan-delivery: findings  
LENS record: findings  
HIGHEST: P1  
RECONCILIATION: gaps
