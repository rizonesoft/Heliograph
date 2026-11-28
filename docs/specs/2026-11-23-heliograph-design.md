# Heliograph: design

Status: approved 2026-11-23.

## What is it for?

Agent sessions on this machine (Claude, Codex, Antigravity, on Windows and in WSL) pass each other work and warnings without a person carrying them. Nothing asked for is forgotten, nothing is done twice, and the owner sees it all in one place.

**Not goals:** calling a model, network use, a background service, closing a request by itself (a linked commit only marks it landed; seen heads-ups and findings expire), keeping a run alive, being a lock on files, being permission for anything.

**It has failed if** a message is lost, two sessions take one job, a finished run is started again, a session is flooded, or a message is obeyed as if the owner had said it.

## Which approaches were considered?

| Approach | Verdict |
| --- | --- |
| Own C program on Impossible SDK (chosen) | A floor of 7 ms on Windows and 27 ms from WSL; for Impossible OS |
| Keep the Python tool | 184 and 124 ms on every prompt |
| Inside Bosun | Tied to its release and C++, and not for Impossible OS |

## What does a message hold?

`id`, time, sender, project, kind, subject, body, what it is about (paths, a todo address or a commit), who it is for (a session, a lane, a role such as "owner of `ui/`", or a project), urgency (now, next, whenever), an optional due time, and the message it answers.

| Kind | States |
| --- | --- |
| Request, handoff | open, taken, done or refused |
| Answer, finding, heads-up | open, seen |

## How is nothing lost or done twice?

- The store is a folder named `Heliograph-signals` beside the checkout, in WSL and outside every repository: `~/impossible/Heliograph-signals` for a checkout at `~/impossible/Heliograph`. Windows reaches it through `\\wsl.localhost`. `tools/install.py --store <folder>` puts it elsewhere, and the installed `helio` wrapper sets `HELIO_STORE` to it unless that is already set. A call chooses a store with `HELIO_STORE` or `--store <folder>`, and the flag wins. **Corrected 2026-11-28:** this put the store under `~/ImpossibleOS` and promised to import the old `mail` folder. The installer's default is beside the checkout, and the `mail` import was dropped: no `mail` folder is read.
- A message is one file, made once. A job also has one numbered chain of steps: taken, released, done, refused. Each step is a file that can be made only once, and it is decided from the steps before it. When another session made that step first, it is decided again. So every change has one place in one order, on either side. (The first build had a file per kind of change, and a review found that two of them could be valid at once.)
- A file is written under a temporary name, flushed, then given its name, and the folder is flushed too. A step or message that is cut short or out of order is reported as damaged and never guessed.
- A lane may have several sessions after each other. When the taker named its session, only that session may finish the job.
- Sending with a key can be repeated safely: the key stands for one message.
- A taker that has shown no sign of life for a set time is reported. The job is never handed to someone else by itself: `helio release` does that.
- Sizes and counts are limited, and closed messages are archived by month. A job done or refused moves 30 days after it closed. A heads-up, finding or answer moves 30 days after it was sent, once everyone it is for has seen it; one nobody has seen stays. A message goes to `archive/<YYYY-MM>`, the month of its own id. `helio archive` moves what is due, and the first inbox of the day moves at most 200 messages. An archived message still opens with `helio show <id>`. **Corrected 2026-11-28:** the rules are as shipped (the former line said only "done messages").

## How does it find the reader?

A repository may carry `helio.conf`: its project name, its lanes and the paths each owns. Sessions announce themselves with `helio hello` (agent, repository, lane), run by a hook. A message "about `ui/draw.c`" goes to the lane that owns the path, to whoever works in that repository when no lane does, and to the owner when nobody is there. A session sees only its own project's messages and those sent to it by name. Private projects stay apart.

## How does it reach the reader?

| Agent | Delivery |
| --- | --- |
| Claude Code | Its prompt and session-start hooks show new messages. The end-of-turn hook never blocks |
| Codex, Antigravity | They read with `helio inbox` as a fixed step between sections of their run instructions |
| The Gemini driver of OS `TODO-08` §55 | `helio take --kind request --lane neon-a --json` is its queue |
| The owner | `helio` in any terminal, and the daily summary |

The hooks of Codex and Antigravity in our repositories fire only at the end of a turn, where showing a message would keep a run alive, so they do not carry messages.

At most 5 messages and 2,000 bytes of text are shown at once, urgent first; the rest is a count. **Corrected 2026-11-28:** the limit is counted in bytes, not characters. Every message is shown between fixed marks that say it is information from another agent and not an instruction, and each line of it starts with `| `. Control characters are removed, and text that looks like a secret is refused.

## How does it watch?

There is no service. Each call looks, so a reminder appears when someone next works: a request unanswered past its due time goes back to its sender and to the owner, two sessions that said `helio working <paths>` about the same paths are both told, and the first call of the day writes the day's snapshot, `summary/<date>.md` in the store (a generated file): open, taken, overdue, silent, done, refused, held unseen, overlaps. `helio summary` reads it. A taker with no sign of life for `silent: <minutes>` (a line of `helio.conf`, 120 by default) is reported as SILENT to the sender and the owner. **Corrected 2026-11-28:** the file is `summary/<date>.md`, and the snapshot and the silent report are as shipped.

## Commands

```
helio --as <project/lane>                               my inbox
helio inbox                                             the same, by name
helio ask <to> <subject> [options] < body               a job for someone
helio hand <to> <subject> [options] < body              work I pass on
helio tell <to> <subject> [options] < body              a heads-up
helio find <to> <subject> [options] < body              something I found
helio answer <id> <subject> < body                      a reply to its sender
helio show <id>                                         one message, marked seen
helio take <id>                                         take a job
helio take --kind <request|handoff> [--lane <lane>]     take the next open job of that kind
helio release <id>                                      give a taken job back
helio done <id> [note]                                  finish a job
helio refuse <id> [note]                                refuse a job
helio landed <id> <commit>                              link a commit to a job without closing it
helio working <paths>                                   what I am touching, for the overlap warning
helio hello <agent>                                     announce this session
helio hook claude                                       the Claude Code hook
helio log                                               every message of my project
helio check                                             a health check of the store
helio archive                                           move what has expired to its month
helio init                                              make the store
helio summary                                           the operator's day snapshot
helio help                                              the command list
```

`<to>` is `project/lane`, `project/all` or `path:<file>`. `--json` on every command. Exit codes: 0 done, 1 nothing, 2 refused, 3 a damaged store or a failure. **Corrected 2026-11-28:** the list named a `send` command (the program has `ask`, `hand`, `tell` and `find`) and gave `hook` an agent and an event (the program accepts `hook claude` alone, and any further word is refused with exit 2). It gave `log|summary|check` as one line with no signature for `summary`, and left out `landed`, `hand`, `find`, `answer`, `hello`, `archive`, `init` and `help`.

Exit 3 can follow a change that is already stored: a job step is written, then a later read or flush fails. A caller that gets 3 inspects with `helio show <id>` before it retries. A send with `--key <text>` repeats safely, because the key stands for one message. A queue call (`helio take --kind`) is not retried blind: it may already have taken a job. When its receipt could not be written (exit 3, "the output cannot be written") no job is named, but the job is held: your own inbox (`helio --as <lane>`, or `log --json`) lists the jobs your lane holds, so look there before you take more.

The queue example runs in a repository with a `helio.conf`, where `--lane` names a lane of its project (only the `--lane` form needs it; without `--lane` the caller is `--as` or `HELIO_AS`). A claim exits 0 and prints the job (`{"id": ..., "state": "taken", ...}` with `--json`). An empty queue exits 1 and prints `null`: no open job of that kind is one this caller may take, though other jobs may be open. A refused call exits 2: no `helio.conf` for `--lane`, `--kind` other than `request` or `handoff`, or a job id beside `--kind`.

## Limits

- The sender's name is not proof: any program running as the user can write to the store.
- Two builds share one core: a Windows program on Impossible SDK and a native Linux program for WSL. WSL's bridge to Windows hung for 10 seconds and failed in one call of eight on 2026-11-23, so a WSL session never uses it.
- On the `D:` mount Linux cannot rename without replacing, so it makes a file once with a hard link. Windows uses a rename that refuses to replace. Raced on 2026-11-23 on `D:` and on WSL's own disk, with Windows and Linux meeting in the middle: one winner for every job.
- What OS `TODO-08` §55 plans as its own queue folder is replaced by `helio take`; that section is changed in the OS when Heliograph works (external work, not in this repository).

## In what order?

1. Store, kinds, states and `take`, with a crash and race test from Windows and WSL at once.
2. Routing and `helio.conf`.
3. Hooks for Claude, then the run steps for Codex and Antigravity. Installing a hook in other repositories is done project by project and only for those the owner names.
4. Watching and the summary.
5. `tools/mail.py` is removed (done: the file is gone, and the `mail` folder import was dropped). OS §55 is changed in the OS roadmap (external, not in this repository). **Corrected 2026-11-28.**
