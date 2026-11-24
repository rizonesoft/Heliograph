## Plan review: astra (`gpt-6.2-astra` via `codex`)

- Writer: agy
- Run: 2026-11-24T01:49:50Z
- Candidate: dd1f1a4c799a4c60ee93b7ffddcc358b34ae5e88
- Effort: medium
- Lenses: plan
- Lines: 24
- Digest: dd57c420f3a11f51

1. **P1, §16 lines 394, 404, 407–408, 411.** Installing `helio.exe` beside `helio.cmd` leaves the default Windows command search free to select the EXE first. That bypasses both the shared-store default and the WSL diagnostic. **Path:** A user opens a fresh Windows terminal with `HELIO_STORE` unset and types `helio`. **Evidence:** traced `engine/helio.c:50`, `:55` and `:267`. The executable reads `HELIO_STORE` or `--store` and otherwise exits 2. Make bare `helio` reach the store-selection logic unambiguously. Test it by name from `cmd.exe`, PowerShell and the configured hook shell, outside the install directory, with overrides cleared. Read back a unique message sent from WSL. Calling `helio.cmd` explicitly would miss this failure.

2. **P1, §16 lines 396, 409–410.** “`PATH` is what it was” can erase later user changes or remove an entry the installer never owned. The journal records the old value but is deleted after commit. **Path:** The destination was already on `Path`, or another installer adds an entry between installation and removal or crash recovery. **Evidence:** traced `todo/01-heliograph/TODO-01-heliograph.md:396`, `:409` and `:410`. Persist whether Heliograph inserted the entry. On removal or recovery, reread the current value and undo only the owned change. Test a pre-existing equivalent entry, an initially absent value, and an intervening edit. Assert the raw registry value, existence and kind after each operation.

3. **P1, §16 lines 396, 407, 410, 414.** The ownership rule covers the published programs but omits the lock, manifest, journal and recovery files. A wrapper marker alone also survives user edits. **Path:** Installation encounters an occupied destination, or a user edits the managed wrapper before upgrade or removal. **Evidence:** traced `todo/01-heliograph/TODO-01-heliograph.md:396` against the reserved-path refusal in `:376` and content verification in `:382`. Define ownership for every path the Windows installer writes, renames, restores or deletes. Refuse foreign files, redirected destinations and modified managed content before mutation. Test refusal with byte-for-byte preservation, including an unrelated file that must keep the destination directory alive.

4. **P1, §16 lines 396–397, 405, 414.** No checkpoint exercises Windows transaction recovery. A successful install and remove can pass with locking disabled or with a registry change left behind after an interrupted install. **Path:** Two installers overlap, a process stops after updating `Path`, or a disk or sharing error interrupts publication or removal. **Evidence:** traced `todo/01-heliograph/TODO-01-heliograph.md:396`, `:405` and `:414`. Specify the commit point and retry result for install, upgrade and remove. Interrupt each distinct file, registry and manifest publication boundary, retry through the public command, and verify the complete file set, hashes, raw registry state and journal cleanup. Include concurrent operations that share the registry value.

5. **P2, §16 line 407.** “As §15 replaces it” and “one in use is renamed away” specify conflicting behavior. Renaming first creates an interval with no executable, and Windows may refuse the rename itself. **Path:** A hook starts while an upgrade is replacing a program still held by another process. **Evidence:** traced `todo/01-heliograph/TODO-01-heliograph.md:377` and `:407`. Specify atomic publication or a reported deferral that leaves the old installation callable. Hold the real Windows executable open, invoke a second caller during replacement, then release it and verify retry and cleanup.

6. **P1, §16 lines 394, 404, 407.** The plan does not bind the Windows copy to a tested build. A stale `helio.exe` can pass `helio check`. **Path:** A user runs `--windows` from a fresh checkout, after changing the source, or after a failed build with an older executable still on disk. **Evidence:** traced `tools/install.py:956`, `:981` and `:1081`, where §15 builds and snapshots its inputs. §16 line 407 only calls for copying the EXE. Define the WSL command that obtains the tested artifact and hands its path, hash and store location to Windows Python. Test a clean install, a failed build that leaves the live installation untouched, and a hash comparison against the installed bytes.

7. **P2, §16 lines 396, 407, 410.** The Windows manifest has no specified read-back or usable previous-generation contract. `--remove` cannot restore the prior program, and deleting retired copies on the next install does not establish which tested version can be restored. **Path:** A user checks or rolls back the Windows installation after an upgrade. **Evidence:** traced the promised outcome at `todo/01-heliograph/TODO-01-heliograph.md:27`, §17’s rollback caller at `:432`, and the existing WSL-only status and back dispatch at `tools/install.py:1689` and `:1694`. Define Windows status and rollback, or assign them explicitly to a named section. Test two distinguishable generations, live and backup hash read-back, and refusal of a corrupt or absent backup without changing the live installation.

8. **P2, §16 lines 408, 411.** Showing one default-store message does not prove store precedence, argument forwarding, or the unavailable-distro path. An unconditional WSL check can also reject a valid local override. **Path:** A caller supplies `--store`, sets `HELIO_STORE`, uses a path containing spaces, or runs from a non-default WSL distro. **Evidence:** traced `engine/helio.c:50`, `:55` and `todo/01-heliograph/TODO-01-heliograph.md:379`. Derive the default UNC location from the actual installing distro and configured store. Test `--store` over environment over default using distinct messages, including a reachable override when the default is unavailable. For an unreachable selected default, assert the diagnostic identifies the failing dependency, exits 3 and creates no alternate store. Verify argument, stdin and exit-status forwarding through the installed command.

9. **P2, §16 lines 404–405, 409, 414.** The Windows proof can be green with every new installer test skipped. It can also pass with environment-change notification removed, since the stated `Path` assertion only needs a registry read. **Path:** The implementer adds tests to the existing installer test class and runs the prescribed proof. **Evidence:** traced `tests/test_install.py:102`, which skips that class on Windows, and `../ImpossibleSDK/tools/prove.py:44` and `:50`, which build with `--no-test` and then run Windows unittest discovery. Require a Windows-native class whose required cases execute, plus a WSL-side test of the public `--windows` handoff. Give the Windows destination and registry key explicit isolated inputs. Assert the environment-change notification and command discovery separately. A child given a manually patched `PATH` cannot prove notification.

10. **P3, §16 lines 404, 410–411.** The operator-facing instructions stop at the WSL installer. `README.md:74` and `:80` document WSL installation and management, and §16 assigns no update. Add the exact Windows install and remove commands, where to run them, when to open a fresh terminal, and how to check that it reaches the shared store. Use those documented commands in the acceptance test.

LENS plan: findings  
HIGHEST: P1  
PLAN: gaps  
OVERALL: needs-attention

## Round 1: astra (`gpt-6.2-astra` via `codex`)

- Writer: agy
- Run: 2026-11-24T02:03:06Z
- Candidate: 3ecb53c042278da63d0d85e5a264c2d0d8038e96
- Effort: medium
- Lenses: adversarial, integration, source-defect, consistency, quality, optimization
- Lines: 59
- Digest: 60ab364a1d88375b

## adversarial

**needs-attention**

1. **P1** `tools/win_install.py:556` deletes a file created by someone else after an interrupted install. **Path:** A first install stops after writing the publishing journal. Before retry, the user restores their own `helio.cmd` into the destination. **Evidence:** traced. Line 681 recorded only that the file was absent before installation. Recovery at lines 539-557 unlinks whatever now occupies that name, without checking whether its bytes match the wrapper this transaction intended to publish. The same loop overwrites intervening edits when a snapshot exists.
2. **P1** `tools/win_install.py:684` publishes bytes that were never checked against the WSL handoff hash. **Path:** The source file is replaced or changed between validation and staging, including while the installer waits for its registry lock. **Evidence:** traced. Lines 621-626 hash the source, line 684 reopens it, line 714 records the earlier hash, and line 752 publishes the staged copy without hashing it. The install can report success with a different executable on disk.
3. **P1** `tools/win_install.py:586` can delete a pre-existing user file named `.install.lock`. **Path:** Install into a custom destination containing only an unrelated `.install.lock`, then remove. **Evidence:** traced. Line 217 opens the existing file without establishing ownership. Lines 583-588 exclude it from the directory contents check and unlink it once the program files are gone.
4. **P2** `tools/win_install.py:178` sends caller arguments through a second `cmd.exe` expansion via `call :scan_args %*`. **Path:** A hook or shell caller passes a quoted subject containing literal percent-delimited text or escaped shell characters. **Evidence:** traced. Lines 178 and 191-193 reparse every argument as batch input merely to detect `--store`. Quoting for the original invocation does not protect the argument through the extra `CALL` expansion. The native parser at `engine/helio.c:55` already processes argument values.
5. **P3** `tools/win_install.py:480` accepts rollback and removal targets outside the destination in a hand-edited internal journal. A read-only Python probe of `_validate_win_journal` returned `out-of-root key accepted: True` for an `expected_hashes` key of `../outside`. Lines 507-518 use those keys as deletion targets, and lines 541-557 similarly trust `saved_files` and `rec_name`. The sibling validator at `tools/install.py:537` constrains these fields.

## integration

**needs-attention**

1. **P1** `tools/win_install.py:359` allows an existing top-level `helio.exe` to shadow the installed wrapper. **Path:** Select a `--to` folder that already contains `helio.exe`, as can happen when reusing a program folder. **Evidence:** traced. The ownership check inspects `helio.cmd` and the two executables under `bin`, but never the root executable. Install and status can succeed while bare `helio` resolves to the existing `.exe` before `.cmd` and bypasses the configured WSL store.
2. **P1** `tools/win_install.py:1045` applies Path ownership to the registry key supplied on removal, even when the manifest records a different key. **Path:** Install using an isolated `--reg-key`, then remove the same `--to` without repeating that option. **Evidence:** traced. Line 722 records the installation key. Lines 1022-1045 combine its ownership flags with the new invocation's key. Removal leaves the original entry behind and can delete a pre-existing equivalent entry from the real user Path.
3. **P2** `tools/install.py:1725` skips store initialization whenever `--store` is explicit. **Path:** Run `--windows --store` with a new WSL directory, including the same path that an install without the flag would initialize. **Evidence:** traced. Lines 1716-1726 translate the explicit path but skip `_ensure_store_ready`. Installation succeeds, then the generated wrapper exits 3 at `tools/win_install.py:182` because the directory is missing.
4. **P2** `tools/install.py:1706` lets WSL path-conversion failures escape the CLI error handler. **Path:** `wslpath` is present but fails to translate the checkout or store. **Evidence:** ran `install.main(['--windows', '--status'])` under `python3 -B` with `subprocess.run` returning a failed conversion. It raised `uncaught WinInstallError` instead of returning 3. The handler at line 1865 catches `InstallError`, a different class.

## source-defect

**needs-attention**

1. **P1** `tools/win_install.py:698` destroys the previous distinct rollback generation on an identical reinstall. **Path:** Install A, upgrade to B, repeat the B install, then run `--back`. **Evidence:** traced. Every existing installation sets `rotate_old = True`. Lines 745-748 replace backup A with current B, so rollback restores B. Section 16 requires the second install to change nothing. The test at `tests/test_install.py:2084` reinstalls A and B before checking rollback, masking this sequence.
2. **P2** `tools/win_install.py:1057` loses ownership of a still-busy retired executable. **Path:** Keep A running, upgrade to B, then remove B before A exits. **Evidence:** traced. The sweep returns locked names at lines 400-407. Removal ignores that result, deletes the manifest and journal, and reports success. After A exits, another remove takes the no-manifest return at line 1013 and cannot sweep the file. Interrupted upgrades also lose retired names when recovery restores the previous manifest at line 564.
3. **P2** `tools/win_install.py:248` rewrites unrelated Path entries. **Path:** A user's existing Path contains empty fields or a trailing delimiter. **Evidence:** ran `python3 -B` against `_split_path_entries("A;;B;")` and observed `['A', 'B']`. Lines 298-302 serialize the shortened list on install, so remove cannot restore the original value. Line 317 also removes every matching occurrence if another tool appended an equivalent entry after installation.
4. **P2** `tools/win_install.py:244` fails to recognize a pre-existing expandable Path entry for the default destination. **Path:** The user already has `%LOCALAPPDATA%\Heliograph` in a `REG_EXPAND_SZ` Path. **Evidence:** traced. Lines 292-299 compare the literal percent expression to the resolved absolute folder using only case, quote and trailing-slash normalization. Installation appends a duplicate and records `path_added=true`, contrary to the equivalent-entry requirement at `todo/01-heliograph/TODO-01-heliograph.md:409`.

## consistency

**needs-attention**

1. **P1** `tools/win_install.py:845` reports a healthy installation for an empty store directory. **Path:** The configured directory survives but its store contents are removed, or the user selects an existing empty directory. **Evidence:** traced. Lines 844-852 return `status: ok` based solely on `is_dir()`. The sibling status command at `tools/install.py:1368` checks for the `messages` directory, and the initializer at `tools/install.py:1012` uses the same criterion.
2. **P2** `tools/win_install.py:192` implements a different option grammar from the executable. **Path:** Run `helio check --key --store` with a reachable default store and no `HELIO_STORE`. **Evidence:** traced. `engine/helio.c:55-58` consumes `--store` as the value of `--key`. The wrapper scans it as an independent flag and suppresses the default store. Its `/I` comparison also accepts case variants the native parser rejects.

## quality

**needs-attention**

1. **P2** `tests/test_install.py:2195` does not exercise crash recovery for upgrade, rollback or removal. **Path:** The required Windows proof runs `test_windows_crash_recovery_every_boundary_and_concurrent_registry_lock`. **Evidence:** traced. Every iteration creates a fresh destination and interrupts only `win_install`. The retry at line 2214 installs again before checking state, which can conceal a faulty recovery. No iteration interrupts backup rotation, busy rename-away, `win_back` or `win_remove`.
2. **P2** `tests/test_install.py:1973` claims to test stdin forwarding but supplies no stdin and accepts any nonzero exit code. **Path:** The installed wrapper drops a `tell` body or maps every failure to exit 1. **Evidence:** traced. Lines 1974-1981 run `show` without input and assert only `returncode != 0`. The message bodies elsewhere in this test are sent directly to `helio.exe` at line 1898.
3. **P2** `tests/test_install.py:2134` allows a broken Windows environment notification to pass. **Path:** `SendMessageTimeoutW` fails, or the broadcast helper becomes a no-op. **Evidence:** traced. The test counts Python calls. Production ignores the helper's Boolean result at `tools/win_install.py:303`, and the shell discovery test injects the destination into the child Path at `tests/test_install.py:1922`. Both assertions can pass without any consumer receiving the change.
4. **P2** `tests/test_install.py:1867` can record a successful Windows test run with the entire required class skipped. **Path:** The Windows build artifact is missing or discovery points at the wrong build directory. **Evidence:** traced. `setUpClass` raises `SkipTest` for that condition, and line 1937 silently omits the required PowerShell probe if discovery fails. Neither condition fails the proof that section 16 says must exercise those cases.
5. **P2** `tests/test_install.py:1784` bypasses the successful production build handoff. **Path:** The WSL acceptance test runs with a real built executable. **Evidence:** traced. It passes `prebuilt_exe` at line 1790, selecting the branch at `tools/install.py:1727` that skips both the tested snapshot and default-store initialization. The only invocation using the real build branch is deliberately failed. A regression in the successful handoff can therefore pass this test.

## optimization

**advisory**

No missed measured performance target was established. The redundant publication on identical reinstall is covered by the rollback finding.

LENS adversarial: findings  
LENS integration: findings  
LENS source-defect: findings  
LENS consistency: findings  
LENS quality: findings  
LENS optimization: clean  
HIGHEST: P1  
OVERALL: needs-attention


- Implemented: fixed adversarial #1-#5 (recover_win_if_needed verifies live target hashes against saved/desired before unlink/overwrite; win_install stages source_exe first and verifies PE identity + SHA-256 on the staged bytes; destination lock moved to %LOCALAPPDATA%\Heliograph-locks so a user .install.lock is never touched; helio.cmd forwards %* directly without call double-expansion; _validate_win_journal restricts expected_hashes/saved_files/rec_name/temp_files/retired to known in-root paths) in 1ce7b9f
- Implemented: fixed integration #1-#4 (refuse top-level helio.exe shadowing helio.cmd in install and status; enforce recorded manifest reg_key across install/status/back/remove; run_windows_from_wsl initializes explicit WSL --store paths; convert WinInstallError from wslpath into InstallError in main) in 1ce7b9f
- Implemented: fixed source-defect #1-#4 (preserve previous distinct rollback generation on identical reinstall; retain still-busy retired executables in journal on remove and in restored manifest on recovery; preserve exact raw Path delimiters/empty fields and remove only one matching entry; expand %VAR% when checking REG_EXPAND_SZ Path equivalence) in 1ce7b9f
- Implemented: fixed consistency #1-#2 (win_status requires messages/ directory in store; helio.cmd delegates all option parsing to helio.exe so --key --store works identically) in 1ce7b9f
- Implemented: fixed quality #1-#5 (added upgrade/back/remove/external-file/out-of-root crash recovery tests, stdin body and exact exit-code forwarding tests, WM_SETTINGCHANGE failure check, non-skipping Windows setUpClass/PowerShell requirements, and real build+store-init WSL handoff test; verified prove.py passed on b77ca62 with 72 tests in 38.340s OK and all 4 WIN_REVERT_CHECK guard reversions returned rc=1) in b77ca62

## Completion review: astra (`gpt-6.2-astra` via `codex`)

- Writer: agy
- Run: 2026-11-24T02:14:33Z
- Candidate: b77ca623316f65dcdb6951b5a685609cce197a66
- Effort: low
- Lenses: plan-delivery, record
- Lines: 34
- Digest: 49ee065fa3d7936b

## Part 1. Completeness

1. **present**. The four user commands are documented together, including the expected install locations and the fresh-terminal `helio check` command. The section declares `Surface: no`, so its controls are the CLI commands. Evidence: `README.md:87`, `README.md:96`, `todo/01-heliograph/TODO-01-heliograph.md:400`.
2. **present**. The WSL handoff has a build-failure and shared-store test, and Windows verifies the staged executable against the supplied SHA-256 before publishing it. Evidence: `tools/install.py:1690`, `tools/win_install.py:745`, `tests/test_install.py:1745`.
3. **present**. Status reads back the executable, wrapper, backup, registry entry and store. Rollback checks the backup, and the native tests exercise rollback and removal. Evidence: `tools/win_install.py:880`, `tools/win_install.py:993`, `tests/test_install.py:2091`, `tests/test_install.py:2213`.
4. **missing-in-scope, P1**. A failed command against a reachable explicit store can report the wrong exit code and blame WSL. **Path:** With `HELIO_STORE` unset and the default distro offline, a user supplies `--store` and gets an ordinary command error. **Evidence: traced** `tools/win_install.py:179` marks the default as selected before the executable parses `--store`. At `tools/win_install.py:185`, any nonzero result then becomes exit 3 if the default directory is absent. The override test at `tests/test_install.py:2080` only exercises success.
5. **missing-in-scope, P2**. Install followed by remove loses a trailing empty `Path` entry. **Path:** A user's existing registry value is `C:\Tools;`. **Evidence: traced** `tools/win_install.py:303` appends the destination directly after that semicolon. Removal at `tools/win_install.py:333` reconstructs `C:\Tools`. The test at `tests/test_install.py:2248` uses an interior empty entry and misses this case.
6. **missing-in-scope, P2**. An identical reinstall republishes the executable and wrapper and resets the recorded installation time. **Path:** A user repeats the documented install command with the same binary, provenance and store. **Evidence: traced** `tools/win_install.py:778` preserves the previous generation, but `tools/win_install.py:810`, `:850`, `:859` and `:868` still timestamp and publish the install. The “changes nothing” assertion at `tests/test_install.py:2234` checks only the broadcast count.
7. **missing-in-scope, P2**. A busy executable can become an untracked `.retired` file. **Path:** Windows permits a running executable to be renamed and the installer stops before persisting its name. A user can also have the backup executable open during the next upgrade. **Evidence: traced** `tools/win_install.py:438` renames first, while `tools/win_install.py:855` records the live file later. For a busy backup, `tools/win_install.py:846` and `:1077` discard the returned name altogether. The crash hook at `:857` fires after the vulnerable interval.
8. **missing-in-scope, P2**. Cleanup deletes a replacement file at a tracked retired name without verifying its contents. **Path:** After a busy upgrade, a user restores or replaces that file before the next install or remove. **Evidence: traced** `tools/win_install.py:412` checks its name and file type, then `:419` unlinks it. Other installer-owned binaries receive a hash check at `:388` and `:404`. The section expressly includes tracked retired copies in its ownership contract.
9. **missing-in-scope, P2**. The shell tests do not establish the promised fresh-terminal discovery and cross-WSL read in both shells. **Path:** The user's effective `Path` already contains an earlier `helio`. **Evidence: traced** production appends at `tools/win_install.py:306`, while both shell tests prepend the destination at `tests/test_install.py:1960`. The shared-WSL-message test also prepends it and invokes only `cmd.exe` at `tests/test_install.py:1832`. The PowerShell test reads a Windows-created fixture at `tests/test_install.py:1932` and `:1977`. These tests can pass while the user's bare command launches another program.
10. **missing-in-scope, P2**. The evidence available for the checked test item does not establish a passing WSL test run on this candidate. **Path:** The section is stamped using the reported native proof while the WSL-only handoff test could still fail. **Evidence: traced** `todo/01-heliograph/TODO-01-heliograph.md:411` requires both runs. The working-tree review note at `docs/reviews/01-heliograph/D01-T01-s16.md:111` reports native proof and four guard reversions. `../ImpossibleSDK/tools/prove.py:44` explicitly builds with `--no-test` before running discovery under Windows Python. I did not rerun either build during this read-only review. The open stamp boxes are expected.

## Part 2. Comparison

The strongest directly comparable alternative is to invoke the same native `helio.exe` by its full path with an explicit shared store. It is available to this project's users, avoids shell command-discovery ambiguity, and supplies the executable's own exit result. The candidate's tests use this invocation as their reference at `tests/test_install.py:2033`.

1. **in-scope, P1**. Match the direct executable's exit code and diagnostic for a failing command against a reachable explicit store, even with the default distro offline. Target: identical exit code and no default-distro diagnostic in that case. Path and evidence are in item 4.
2. **in-scope, P2**. Establish which program a fresh shell will run. Target: `cmd.exe` and PowerShell both resolve the installed wrapper and read the same WSL-created message using registry-derived path ordering, or installation reports the earlier conflicting command. Path and evidence are in item 9.

## Consolidated gaps

1. `missing-in-scope` / `in-scope` **P1**. A failed explicit-store command can return the default-distro diagnostic and exit 3.
2. `missing-in-scope` **P2**. Removal loses a pre-existing trailing empty `Path` entry.
3. `missing-in-scope` **P2**. Identical reinstall republishes files and changes the installation timestamp.
4. `missing-in-scope` **P2**. Busy-file retirement can leave an executable outside the journal and manifest.
5. `missing-in-scope` **P2**. Retired-file cleanup can delete externally replaced contents.
6. `missing-in-scope` / `in-scope` **P2**. The shell tests force command precedence and do not prove the shared WSL read in PowerShell.
7. `missing-in-scope` **P2**. The checked test item lacks evidence for the required WSL test run on the candidate.

LENS plan-delivery: findings  
LENS record: findings  
HIGHEST: P1  
COMPLETION: gaps

- Implemented: `fed59fc` closes all 7 completion gaps from `b77ca62`: (1) `render_win_cmd` scans argv with `goto :scan_args` + `shift /1` (no `call`, exact `engine/helio.c` option grammar) so failing explicit `--store` commands with an offline default WSL distro return `helio.exe`'s exact exit code without WSL diagnostic; (2) `ensure_reg_path_entry` preserves trailing `;` entries (`C:\Tools;` roundtrip tested); (3) identical reinstall with `Path` present is a true no-op preserving `installed_at` and file bytes/mtimes; (4) `win_install` and `win_back` pre-record potential `.retired` names and SHA-256 hashes in the publishing journal BEFORE renaming busy `helio.exe` or `helio.exe.old`, and capture both returned retired names; (5) `_verify_win_ownership` and `_sweep_retired_exes` verify tracked `.retired` SHA-256 hashes before unlinking (refusing modified files with exit 2); (6) WSL and Windows shell tests derive `PATH` from `HKCU\<reg_key>\Path` appended after system `PATH` and verify BOTH `cmd.exe` and `powershell.exe` read back the WSL-created message; (7) both WSL `app_build.py` (72 tests OK, skipped=4) and Windows `prove.py` (`proved on Windows: Heliograph fed59fc`, 72 tests OK, skipped=14, all 4 `TestWindowsInstaller` green) plus 4 Windows guard-reversion checks (`rc=1`) ran on `fed59fc`.

## Reconciliation review: astra (`gpt-6.2-astra` via `codex`)

- Writer: agy
- Run: 2026-11-24T02:30:43Z
- Candidate: fed59fc9265fb6812354f649a99ec97adff10ae9
- Effort: low
- Lenses: adversarial, integration, plan-delivery, record
- Lines: 21
- Digest: 42fb61d4792a3923

Three of the seven completion gaps remain open on `fed59fc`. The section contract is unchanged. The writer’s `- Implemented:` line is present in the working findings file at `docs/reviews/01-heliograph/D01-T01-s16.md:158`. The supplied WSL and Windows gates passed.

1. **open, P1. Explicit-store exit status.** **Path:** With the default distro offline, a caller runs `helio show missing --about "" --store C:\reachable` with `HELIO_STORE` unset. **Evidence:** Traced. The native parser accepts the empty `--about` value and continues to `--store` at `engine/helio.c:55`. The wrapper treats the empty argument as the end of the command line at `tools/win_install.py:181` and exits 3 at `tools/win_install.py:202` before invoking the executable. The new comparison at `tests/test_install.py:2105` covers only a nonempty argument sequence.
2. **closed. Trailing empty `Path` entry.** The installer appends a new separator at `tools/win_install.py:322`. The native regression at `tests/test_install.py:2294` checks that install followed by remove restores both `C:\Tools;` and its registry kind.
3. **closed. Identical reinstall.** The identity check includes the wrapper hash at `tools/win_install.py:814`. With the registry entry present, the installer returns before publishing at `tools/win_install.py:849`. The native test checks the timestamp, wrapper modification time and broadcast count at `tests/test_install.py:2281`.
4. **open, P2. Retired executable loses ownership across upgrade.** **Path:** A user upgrades from the reviewed installer after a running executable was retired. That installer wrote the retired names as a list at `tools/win_install.py:820` on `b77ca62`. **Evidence:** Ran `python3 -B` against the repaired `_normalize_retired_map` with a valid one-name list. It returned `{}`. The installer then sweeps that empty map at `tools/win_install.py:757` and publishes only its contents at `tools/win_install.py:873`. Pre-recording names protects new publications, but an existing retired executable can still be left on disk with its ownership record erased.
5. **closed. Replaced contents at a tracked retired name.** The sweep verifies the recorded digest before unlinking at `tools/win_install.py:453`. The regression at `tests/test_install.py:2166` replaces the bytes, exercises both install and remove, and verifies that each refuses and preserves the replacement.
6. **open, P2. Fresh-shell command identity.** **Path:** An earlier `helio.cmd` on the user’s `Path` points to the same WSL store, as can happen with a second installation. Both shells can return the expected message through that older wrapper. **Evidence:** Traced. The probes append the registry value after the inherited `Path` at `tests/test_install.py:1843`, then check the message in both outputs at `tests/test_install.py:1860`. Neither identifies the resolved command. The target at `docs/reviews/01-heliograph/D01-T01-s16.md:141` expressly requires both shells to resolve the installed wrapper or the installer to report the conflict. The cross-WSL read itself is now covered.
7. **closed. WSL gate evidence.** The supplied candidate gate reports 72 tests, four Windows-only skips and a pass for the WSL handoff test at `tests/test_install.py:1774`. The separate Windows proof names `fed59fc`. Together these satisfy the run requirement at `todo/01-heliograph/TODO-01-heliograph.md:411`.

**New findings from the repair**

8. **P1. Recovery rejects a genuine journal from the previous installer.** **Path:** Windows interrupts publication under `b77ca62`, and the user retries with `fed59fc`. Even an installation with no busy files writes `retired: []` at `tools/win_install.py:836` on `b77ca62`. **Evidence:** Ran the repaired validator with a schema-2 publishing journal containing that empty list. It returned `2 Windows install journal retired map contains invalid entries`. Recovery calls the validator at `tools/win_install.py:585`, so it stops before restoring the interrupted installation.
9. **P2. Store-independent help fails when the default distro is offline.** **Path:** A Windows user runs `helio help` to inspect the available commands while WSL is unavailable. **Evidence:** Traced. The engine returns help before checking for a store at `engine/helio.c:266`. The repaired wrapper now checks reachability at `tools/win_install.py:200` and exits before launching the engine at `tools/win_install.py:205`. The previously successful command returns 3.

LENS adversarial: findings  
LENS integration: findings  
LENS plan-delivery: findings  
LENS record: findings  
HIGHEST: P1  
RECONCILIATION: gaps

- Implemented: `95fd1d1` and `b0fa145` close all 5 findings from Reconciliation 1 (`fed59fc`): (1) `render_win_cmd` splits the empty-arg check (`if not "%~1"=="" goto :check_arg` / `if "%1"=="" goto :after_scan`) and matches `engine/helio.c`'s exact value-taking flags (`--as`, `--session`, `--key`, `--due`, `--about`, `--store`), so `helio show missing --about "" --store <reachable>` with default WSL distro offline returns `helio.exe`'s exact exit code without WSL diagnostic; (4) `_normalize_retired_map`, `_verify_win_ownership`, and `_sweep_retired_exes` accept legacy `list[str]` retired entries from `b77ca62` manifests, verifying `_is_heliograph_pe` and sweeping or upgrading busy ones to SHA-256 dict entries; (6) both WSL handoff and Windows native shell tests verify resolved command identity via `where helio` (cmd.exe) and `(Get-Command helio).Source` (PowerShell) matching `<win_to>\helio.cmd`; (8) `_validate_win_journal` accepts legacy `list[str]` `retired` fields in schema-2 journals so recovery from `b77ca62` interrupted installs succeeds cleanly; (9) `render_win_cmd` runs `helio.exe` first and checks default-store reachability only when `helio.exe` exits non-zero using the default store, so `helio help` succeeds (exit 0) even when the default WSL distro is offline. Both WSL `app_build.py` (72 tests OK, skipped=4) and Windows `prove.py` (`proved on Windows: Heliograph b0fa145`, 72 tests OK, skipped=14, all 4 `TestWindowsInstaller` green) ran on `b0fa145`.

## Reconciliation review: astra (`gpt-6.2-astra` via `codex`)

- Writer: agy
- Run: 2026-11-24T02:39:33Z
- Candidate: b0fa14563df797a7e30e003bd246dd8927c15252
- Effort: low
- Lenses: adversarial, integration, plan-delivery, record
- Lines: 22
- Digest: 80497c4cb7ee0bca

Three obligations remain open on `b0fa145`. The section contract is unchanged. The writer’s repair claims are recorded at `docs/reviews/01-heliograph/D01-T01-s16.md:158` and `docs/reviews/01-heliograph/D01-T01-s16.md:192`.

1. **open, P1. Explicit-store exit status.** **Path:** With the default distro offline, a cmd.exe caller runs `helio show missing --about ref,--key --store C:\reachable`. **Evidence:** Traced. The native parser consumes `ref,--key` as one `--about` value and then selects the explicit store at `engine/helio.c:55`. The batch scanner uses `shift /1`, for which an unquoted comma separates parameters. It consumes `ref`, treats `--key` as another option, and skips `--store` at `tools/win_install.py:185` and `tools/win_install.py:196`. The executable receives the original `%*`, but the wrapper marks the default as selected and replaces the command failure with exit 3 at `tools/win_install.py:202` and `tools/win_install.py:212`. The empty-value regression at `tests/test_install.py:2120` closes the earlier example only.
2. **closed. Trailing empty `Path` entry.** The installer appends a separate delimiter at `tools/win_install.py:329`. The native test verifies that removing the installation restores both `C:\Tools;` and its registry kind at `tests/test_install.py:2335`.
3. **closed. Identical reinstall.** The identity comparison covers the executable, wrapper, store and provenance at `tools/win_install.py:841`. The matching installation returns before publication at `tools/win_install.py:875`. The native regression checks broadcast count, installation time and wrapper modification time at `tests/test_install.py:2320`.
4. **closed. Retirement tracking across publication and upgrade.** Install and rollback journal the possible live and backup retirement names before replacement at `tools/win_install.py:903` and `tools/win_install.py:1146`. Legacy list entries survive normalization at `tools/win_install.py:451`, and a busy copy remains tracked at `tools/win_install.py:480`. The legacy manifest regression is at `tests/test_install.py:2213`. Verification of the contents at those names remains a separate open obligation below.
5. **open, P2. Cleanup can delete a replacement at a legacy retired name.** **Path:** A user restores a different Heliograph build over a retired copy tracked by the previous installer, then runs install or remove. **Evidence:** Traced. A legacy entry has no digest at `tools/win_install.py:451`. The fallback accepts any `MZ` file containing either Heliograph marker at `tools/win_install.py:97`, hashes the replacement itself, then unlinks it at `tools/win_install.py:474` and `tools/win_install.py:478`. The tampering test uses a digest-backed entry at `tests/test_install.py:2194`. The legacy test expects deletion of a genuine executable without establishing its original digest at `tests/test_install.py:2213`.
6. **closed. Fresh-shell identity and shared message.** The WSL handoff probe appends the isolated registry `Path`, identifies the first cmd.exe match and PowerShell’s resolved command, and runs both inbox commands at `tests/test_install.py:1843`. It checks each resolved path against the installed wrapper and each shell’s output for the WSL-created subject at `tests/test_install.py:1865`.
7. **closed. Candidate gate evidence.** The supplied WSL run passed 72 tests with four Windows-only skips, including a passing handoff test. The separate Windows proof names `b0fa145`. The writer records both runs at `docs/reviews/01-heliograph/D01-T01-s16.md:192`, matching the requirement at `todo/01-heliograph/TODO-01-heliograph.md:411`.
8. **closed. Legacy journal acceptance.** The validator accepts schema-2 retired-name lists at `tools/win_install.py:581`. The regression supplies the previously rejected empty list, runs recovery and checks the restored live digest at `tests/test_install.py:2486`.
9. **closed. Offline help.** The wrapper runs the engine before checking the default store and enters the diagnostic branch only on failure at `tools/win_install.py:206`. The native test checks successful help output with the default distro offline at `tests/test_install.py:2139`.

**New finding**

10. **open, P2. An interrupted recovery can refuse its own restored manifest.** **Path:** An upgrade from a legacy installation is interrupted. Recovery restores the old installation, then the process stops after the manifest write but before journal removal. **Evidence:** Traced. Recovery normalizes the previous manifest’s retired list, including `[]`, to a dictionary and writes the result at `tools/win_install.py:706` and `tools/win_install.py:709`. The journal remains until `tools/win_install.py:717`. On retry, the restored manifest differs from the journal’s previous manifest (`{}` versus `[]`) and from the desired upgrade manifest. The equality check at `tools/win_install.py:662` refuses it as externally modified. The compatibility test changes only the journal’s `retired` field at `tests/test_install.py:2486` and exercises a single recovery.

LENS adversarial: findings  
LENS integration: findings  
LENS plan-delivery: findings  
LENS record: findings  
HIGHEST: P1  
RECONCILIATION: gaps

- Implemented: `f84b8ce` closes all 3 remaining findings from Reconciliation 2 (`b0fa145`): (1) moved the unreachable `\\wsl.localhost\<distro>` / `\\wsl$\<distro>` store diagnostic to the owning layer `engine/helio.c` (`fail_wsl_store`, exit 3) and simplified `render_win_cmd` to 6 lines with zero batch argv scanning (`if not defined HELIO_STORE set "HELIO_STORE=..."` + `"%~dp0bin\helio.exe" %*`), so `helio show missing --about ref,--key --store <reachable>` and `helio show missing --about "" --store <reachable>` return `helio.exe`'s exact exit code without WSL diagnostic; (5) `_verify_win_ownership` and `_sweep_retired_exes` refuse with exit 2 (never unlink) any existing unhashed legacy `list[str]` retired file, while empty legacy `retired: []` upgrades cleanly; (10) added `_manifests_recovery_match` in `recover_win_if_needed` so retrying recovery after an interrupted recovery wrote the normalized restored manifest (`retired: {}` vs legacy `[]`) succeeds cleanly. Both WSL `app_build.py` (72 tests OK, skipped=4) and Windows `prove.py` (`proved on Windows: Heliograph f84b8ce`, 72 tests OK, skipped=14, all 4 `TestWindowsInstaller` green) ran on `f84b8ce`.

## Reconciliation review: astra (`gpt-6.2-astra` via `codex`)

- Writer: agy
- Run: 2026-11-24T02:45:07Z
- Candidate: f84b8ce3db6c2f30d2a40b103a3c6969875e06ed
- Effort: low
- Lenses: adversarial, integration, plan-delivery, record
- Lines: 28
- Digest: 749802c898306edf

The repair closes the batch argument-parsing defect, but recovery can still reject a manifest it wrote itself. It also weakens compatibility with existing Windows installations. The section contract at `todo/01-heliograph/TODO-01-heliograph.md:396` is unchanged between the failed and repaired candidates.

1. **Closed. Explicit-store exit status.** The wrapper forwards the original arguments at `tools/win_install.py:174`, and the executable selects `--store` at `engine/helio.c:55`. The native test compares direct and wrapped failures for both the empty argument and the comma case at `tests/test_install.py:2119`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:227`.
2. **Closed. Trailing empty `Path` entry.** Installation preserves the existing delimiter at `tools/win_install.py:292`. The native round-trip test checks the exact restored value and registry kind at `tests/test_install.py:2352`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:158`.
3. **Closed. Identical reinstall.** A matching installation returns before republication at `tools/win_install.py:853`. The regression checks broadcast count, timestamp and wrapper modification time at `tests/test_install.py:2333`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:158`.
4. **Closed. Publication tracks busy live and backup executables.** Install and rollback record potential retirement names and hashes before replacement at `tools/win_install.py:881` and `tools/win_install.py:1124`. Legacy names survive normalization at `tools/win_install.py:411`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:158` and `docs/reviews/01-heliograph/D01-T01-s16.md:192`. Recovery’s treatment of a busy replacement is covered in item 10.
5. **Closed. Cleanup preserves externally replaced contents.** Cleanup verifies the recorded digest and refuses an existing unhashed legacy entry at `tools/win_install.py:429`. The native tests exercise both forms at `tests/test_install.py:2195` and `tests/test_install.py:2214`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:227`. The refusal also affects genuine legacy files, as described in item 11.
6. **Closed. Fresh-shell identity and shared-store read.** The handoff test appends the isolated registry value, verifies the resolved wrapper in each shell, and checks each shell’s output independently for the WSL-created subject at `tests/test_install.py:1843` and `tests/test_install.py:1865`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:192`.
7. **Closed. Candidate gate evidence.** The supplied WSL run passed 72 tests with four Windows-only skips and a passing WSL handoff test. The separate Windows proof names `f84b8ce`. This satisfies `todo/01-heliograph/TODO-01-heliograph.md:411` and matches the run record at `docs/reviews/01-heliograph/D01-T01-s16.md:227`.
8. **Closed. Legacy journal format.** The validator accepts schema-2 retired-name lists at `tools/win_install.py:537`. The regression restores the prior executable from a journal with `retired: []` at `tests/test_install.py:2500`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:192`.
9. **Closed. Offline help.** Help returns before store access at `engine/helio.c:293`. The native regression checks exit zero and help output at `tests/test_install.py:2140`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:192`.
10. **Open, P2. Interrupted recovery can still refuse its own restored manifest.** **Path:** An upgrade from A to B stops after publishing B. A caller holds B open while recovery restores A. Recovery then stops after writing the restored manifest. **Evidence:** Traced. The journal records A’s digest for the potential retired live executable at `tools/win_install.py:888`. During recovery, the rename retires B under that same transaction name, and B’s digest is recorded only in memory at `tools/win_install.py:672`. If B remains busy, recovery writes its digest into the restored manifest at `tools/win_install.py:686`. On retry, the matcher requires the journal’s digest A at `tools/win_install.py:571` and refuses the manifest at `tools/win_install.py:641`. The repair recorded at `docs/reviews/01-heliograph/D01-T01-s16.md:227` covers the empty-list case. Its regression explicitly empties the retirement record at `tests/test_install.py:2500`.

**New findings**

11. **Open, P2. A genuine legacy retirement now blocks install and removal indefinitely.** **Path:** The previous installer retired a running executable and recorded its name. The process has since exited, and the user upgrades or removes Heliograph. **Evidence:** Traced. The ownership check refuses the existing file solely because the legacy record lacks a digest at `tools/win_install.py:398`. The revised test expects both public operations to fail and manually deletes the file at `tests/test_install.py:2230`. This preserves unknown bytes but narrows the automatic cleanup required at `todo/01-heliograph/TODO-01-heliograph.md:407`, including cases where the retained rollback generation could supply independent ownership evidence.
12. **Open, P2. Status reports an intact earlier installation as modified.** **Path:** A user updates the checkout and runs the documented Windows status command before upgrading the installation. **Evidence:** Traced. Status regenerates the wrapper using the current template at `tools/win_install.py:997`. The template changed at `tools/win_install.py:173`, so the installed earlier wrapper fails the byte comparison even when it matches its recorded hash.
13. **Open, P2. Rollback loses the offline-distro diagnostic across this upgrade.** **Path:** A user upgrades from the previous candidate, rolls back, then invokes `helio check` while the configured distro is unavailable. **Evidence:** Traced. Rollback restores the previous executable but generates the current wrapper at `tools/win_install.py:1065` and records that combination at `tools/win_install.py:1108`. The earlier executable lacks the diagnostic introduced at `engine/helio.c:274`, and the new wrapper only forwards at `tools/win_install.py:174`. Neither component in the restored pair supplies the promised diagnostic.
14. **Open, P2. A reachable WSL store can be misreported as an offline distro.** **Path:** The configured WSL directory is reachable, but its `messages` directory is missing or the store has reached its capacity. An explicit UNC override can take the same path. **Evidence:** Traced. Store opening distinguishes those conditions at `engine/store.c:84` and `engine/store.c:85`. The new branch replaces either error solely on the UNC prefix at `engine/helio.c:310`, losing the actual remediation. It does not establish that the default was selected or that the distro is unavailable.
15. **Open, P2. Offline `helio init` loses the required diagnostic.** **Path:** A Windows user runs `helio init` through the installed wrapper with the default distro offline. **Evidence:** Traced. Init returns before the new diagnostic branch at `engine/helio.c:295`. Its failure reports only “the store cannot be made” at `engine/helio.c:261`. The wrapper at `tools/win_install.py:174` now passes that result straight through.
16. **Open, P2. The new diagnostic emits raw terminal controls from the distro component.** **Path:** A caller supplies a malformed UNC store through the supported `--store` argument. **Evidence:** Ran the candidate Linux executable with `check --store` and a `//wsl.localhost/` path containing byte `0x1b` in the distro component. It exited 3 and emitted a literal `0x1b` followed by `[31m` on stderr. The first interpolation cleans the path at `engine/helio.c:282`, but the second copies the same component byte-for-byte at `engine/helio.c:284`.

LENS adversarial: findings  
LENS integration: findings  
LENS plan-delivery: findings  
LENS record: findings  
HIGHEST: P2  
RECONCILIATION: gaps

- Implemented: `c2b0def` and `a5b9b28` close all 7 remaining P2 findings from Reconciliation 3 (`f84b8ce`): (10) `_manifests_recovery_match` accepts desired-generation digests recorded in restored manifests and `recover_win_if_needed` preserves already-verified retired digests from `current_manifest` on retry so interrupted recovery after retiring busy B succeeds on retry; (11) `_verify_win_ownership` and `_sweep_retired_exes` allow sweeping unhashed legacy `list[str]` retired files whose SHA-256 matches `current`/`previous` manifest generations while refusing unknown/replaced binaries with exit 2; (12) `win_status` checks `helio.cmd` against its recorded `helio_cmd_sha256` so intact earlier-template installations remain healthy; (13) `win_install` and `win_back` preserve raw CRLF `helio_cmd_text` (verified against `helio_cmd_sha256`) so rollback restores the exact prior generation's wrapper bytes; (14) `fail_wsl_store` in `engine/helio.c` probes reachability of the WSL distro root via `p_list` so a reachable uninitialized/full WSL store reports its real store error instead of an offline distro; (15) `init()` in `engine/helio.c` calls `fail_wsl_store` on failure so offline `helio init` reports WSL and distro with exit 3; (16) `fail_wsl_store` passes the distro component through `clean(..., true)` so malformed UNC paths never emit raw control bytes on stderr. Both WSL `app_build.py` (72 tests OK, skipped=4) and Windows `prove.py` (`proved on Windows: Heliograph a5b9b28`, 72 tests OK, skipped=14, all 4 `TestWindowsInstaller` green) ran on `a5b9b28`.

## Reconciliation review: astra (`gpt-6.2-astra` via `codex`)

- Writer: agy
- Run: 2026-11-24T02:56:11Z
- Candidate: a5b9b28db02de6185c4f402497eab2ace41207ea
- Effort: low
- Lenses: adversarial, integration, plan-delivery, record
- Lines: 31
- Digest: 215eef86bf321142

**Reconciliation returns gaps on `a5b9b28`.** The section contract is unchanged. The supplied WSL gate passed 72 tests with four Windows-only skips, and the separate Windows proof names the repaired candidate. The remaining defects concern ownership, crash recovery, rollback compatibility and selection of the offline-WSL diagnostic.

1. **closed. Explicit reachable store preserves exit status.** The wrapper forwards the arguments at `tools/win_install.py:174`. The native test compares direct and wrapped failures for both an empty argument and a comma-containing value at `tests/test_install.py:2135`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:227`.
2. **closed. Trailing empty `Path` entry.** The native round trip verifies the exact restored `C:\Tools;` value and registry kind at `tests/test_install.py:2401`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:158`.
3. **closed. Identical reinstall.** The matching installation returns before publication at `tools/win_install.py:902`. The regression checks broadcast count, installation timestamp and wrapper modification time at `tests/test_install.py:2387`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:158`.
4. **closed. Publication records busy live and backup executables.** Install and rollback journal both potential retirement names and their hashes before replacement at `tools/win_install.py:931` and `tools/win_install.py:1191`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:158`. Recovery has a separate interruption window in finding 17.
5. **open, P2. Legacy cleanup can delete a replacement.** **Path:** A user restores the current executable B over a legacy retired copy of A before removal. **Evidence:** Traced. The repair accepts either generation’s digest at `tools/win_install.py:404` and `tools/win_install.py:416`, then unlinks the replacement at `tools/win_install.py:458` and `tools/win_install.py:463`. The regression rejects an unknown third build at `tests/test_install.py:2255`. It misses a replacement drawn from the other known generation. The implementation claim at `docs/reviews/01-heliograph/D01-T01-s16.md:268` therefore establishes generation membership but cannot establish which bytes belonged at that retired name.
6. **closed. Fresh-shell identity and shared-store read.** The WSL handoff test uses registry-derived ordering, checks the resolved wrapper in both shells, and independently checks both outputs for the WSL-created subject at `tests/test_install.py:1843` and `tests/test_install.py:1864`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:192`.
7. **closed. Candidate gate evidence.** The supplied WSL run includes the passing handoff test. The separate Windows proof names `a5b9b28`. This matches the requirement at `todo/01-heliograph/TODO-01-heliograph.md:411` and the run record at `docs/reviews/01-heliograph/D01-T01-s16.md:268`.
8. **closed. Legacy journal format.** The validator accepts schema-2 retired-name lists at `tools/win_install.py:566`. The regression restores the live digest and retries with the legacy journal at `tests/test_install.py:2554`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:192`. Finding 18 concerns the removal recovery operation after validation.
9. **closed. Offline help.** Help returns before store access at `engine/helio.c:305`. The native test checks exit zero and help output at `tests/test_install.py:2154`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:192`.
10. **closed. Retry after writing the restored manifest.** The matcher accepts the desired-generation digest at `tools/win_install.py:605`, and recovery carries the recorded digest forward at `tools/win_install.py:723`. The regression recreates that interruption state and checks cleanup at `tests/test_install.py:2579`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:268`.
11. **open, P2. A genuine legacy retirement can still block install and removal.** **Path:** A remains running across two upgrades by the previous installer. After A exits, its retired name remains recorded, while the retained generations are B and C. **Evidence:** Traced. The previous installer retained busy names at `tools/win_install.py:422` on `b77ca62`. The repair derives acceptable legacy digests from only the retained generations at `tools/win_install.py:348` and refuses A at `tools/win_install.py:416`. The successful migration at `tests/test_install.py:2269` covers a retained generation only. The claim at `docs/reviews/01-heliograph/D01-T01-s16.md:268` leaves the older genuine file blocking both operations.
12. **closed. Status recognizes an intact earlier wrapper.** Status compares installed bytes with the recorded digest at `tools/win_install.py:1046`. The earlier-template regression checks successful status at `tests/test_install.py:2283`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:268`.
13. **closed. Rollback after upgrading an earlier installation.** Upgrade captures the previous wrapper bytes at `tools/win_install.py:876`. Rollback verifies and restores them at `tools/win_install.py:1113`. The upgrade-then-rollback test checks exact bytes at `tests/test_install.py:2287`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:268`. Direct rollback of an existing installation is a distinct path in finding 19.
14. **open, P2. The offline diagnostic still applies to explicit UNC overrides.** **Path:** A Windows caller selects an unavailable distro with `--store` or `HELIO_STORE`. The section reserves this diagnostic for the installed default at `todo/01-heliograph/TODO-01-heliograph.md:396`. **Evidence:** Ran `build/linux/linux/helio check --store '//wsl.localhost/MissingReviewDistro/store'`. It exited 3 and printed `cannot reach WSL store` with the distro name. The wrapper puts its default into the same environment variable at `tools/win_install.py:173`, and the engine passes only the folder to the diagnostic at `engine/helio.c:322`. The reachability repair claimed at `docs/reviews/01-heliograph/D01-T01-s16.md:268` fixes the reachable-directory case but still loses the source of the selection.
15. **closed. Offline `init`.** Failed initialization reaches the WSL diagnostic at `engine/helio.c:299`. The native test checks exit 3, WSL and the configured distro at `tests/test_install.py:2164`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:268`.
16. **closed. Terminal controls in the distro component.** Both path and distro are cleaned at `engine/helio.c:283` and `engine/helio.c:285`. The native regression checks stderr for a literal escape byte at `tests/test_install.py:2177`. Implemented at `docs/reviews/01-heliograph/D01-T01-s16.md:268`.

**Additional repair findings**

17. **open, P2. A crash before the restored manifest is written strands a retired executable.** **Path:** During recovery of an interrupted A-to-B upgrade, a caller holds B open. Recovery renames B aside and restores A, then stops before writing the manifest. **Evidence:** Traced. B’s digest exists only in memory at `tools/win_install.py:711`. On retry, recovery reloads A’s journal digest at `tools/win_install.py:694`, sees A already restored and skips the replacement at `tools/win_install.py:700`. The sweep then rejects retired B against A’s digest at `tools/win_install.py:455`. The test starts from a manifest already containing B’s digest at `tests/test_install.py:2579`, past the failing boundary.
18. **open, P2. Interrupted removal loses the evidence needed to sweep a verified legacy file.** **Path:** A user removes a legacy installation with a retired copy matching the rollback generation. Removal stops after writing its journal and before sweeping. **Evidence:** Traced. The journal persists the normalized legacy entry with a null digest at `tools/win_install.py:1290` and `tools/win_install.py:1304`. Ordinary removal supplies the generation hashes at `tools/win_install.py:1325`. Recovery deletes the owned files and calls the sweep without those hashes at `tools/win_install.py:643` and `tools/win_install.py:647`. The sweep refuses the same genuine copy at `tools/win_install.py:461`, leaving removal incomplete on retry.
19. **open, P2. Direct rollback of an earlier installation can lose the offline diagnostic.** **Path:** A user with two generations installed by the previous installer updates the checkout and runs the documented `--windows --back` before running another install. **Evidence:** Traced. The previous installer recorded the old wrapper hash without its text at `tools/win_install.py:782` on `b77ca62`. On that manifest, rollback generates today’s forwarding wrapper at `tools/win_install.py:1121` and pairs it with the earlier executable. Neither component supplies the earlier wrapper’s offline diagnostic. The regression at `tests/test_install.py:2285` first performs an upgrade that captures the wrapper text, so it cannot exercise direct rollback.

LENS adversarial: findings  
LENS integration: findings  
LENS plan-delivery: findings  
LENS record: findings  
HIGHEST: P2  
RECONCILIATION: gaps
