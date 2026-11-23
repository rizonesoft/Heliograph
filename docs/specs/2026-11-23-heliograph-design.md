# Heliograph: design

Status: approved 2026-11-23.

## What is it for?

Agent sessions on this machine (Claude, Codex, Antigravity, on Windows and in WSL) pass each other work and warnings without a person carrying them. Nothing asked for is forgotten, nothing is done twice, and the owner sees it all in one place.

**Not goals:** calling a model, network use, a background service, closing a request by itself (a linked commit only marks it landed; seen heads-ups and findings expire), keeping a run alive, being a lock on files, being permission for anything.

**It has failed if** a message is lost, two sessions take one job, a finished run is started again, a session is flooded, or a message is obeyed as if the owner had said it.

## Which approaches were considered?

| Approach | Verdict |
| --- | --- |
| Own C program on Impossible SDK, whole machine (chosen) | A floor of 7 ms on Windows and 27 ms from WSL; a program for Impossible OS |
| Keep the Python tool | 184 and 124 ms on every prompt |
| Inside Bosun | Tied to its release and C++, and not for Impossible OS |

## What does a message hold?

`id`, time, sender, project, kind, subject, body, what it is about (paths, a todo address or a commit), who it is for (a session, a lane, a role such as "owner of `ui/`", or a project), urgency (now, next, whenever), an optional due time, and the message it answers.

| Kind | States |
| --- | --- |
| Request, handoff | open, taken, done or refused |
| Answer, finding, heads-up | open, seen |

## How is nothing lost or done twice?

- The store is `D:\ImpossibleOS\Heliograph-signals`, outside every repository. The old `mail` folder is brought in.
- Every event is its own file that is never changed: posted, seen, taken, released, done, refused. A file is written under a temporary name, flushed, then renamed. The state of a message is worked out from its events, so there is no database to damage.
- Taking a job creates one file that can be created only once. Exactly one session wins, on either side.
- A taker that has shown no sign of life for a set time is reported. The job is never handed to someone else by itself: `helio release` does that.
- Sizes and counts are limited, and done messages are archived by month.

## How does it find the reader?

A repository may carry `helio.json`: its project name, its lanes and the paths each owns. Sessions announce themselves with `helio hello` (agent, repository, lane), run by a hook. A message "about `ui/draw.c`" goes to the lane that owns the path, to whoever works in that repository when no lane does, and to the owner when nobody is there. A session sees only its own project's messages and those sent to it by name. Private projects stay apart.

## How does it reach the reader?

| Agent | Delivery |
| --- | --- |
| Claude Code | Its prompt and session-start hooks show new messages. The end-of-turn hook never blocks |
| Codex, Antigravity | Their hooks in our repositories fire only at the end of a turn, where showing a message would keep a run alive. They read with `helio inbox` as a fixed step between sections of their run instructions |
| The Gemini driver of OS `TODO-08` §55 | `helio take --kind request --lane neon-a --json` is its queue |
| The owner | `helio` in any terminal, and the daily summary |

At most 5 messages and 2,000 characters are shown at once, urgent first; the rest is a count. Every message is shown between fixed marks that say it is information from another agent and not an instruction. Control characters are removed, and text that looks like a secret is refused.

## How does it watch?

There is no service. Each call looks, so a reminder appears when someone next works: a request unanswered past its due time goes back to its sender and to the owner, two sessions that said `helio working <paths>` about the same paths are both told, and the first call of the day writes `Heliograph-signals\summary\<date>.md`: open, taken, overdue, done, overlaps.

## Commands

```
helio                     my inbox
helio send|ask|tell       a message, a request, a heads-up
helio take|done|refuse|release <id>
helio working <paths>     what I am touching, for the overlap warning
helio log|summary|check   everything, the day, a health check of the store
helio hook <agent> <event>
```

`--json` on every command. Exit codes: 0 done, 1 nothing, 2 refused, 3 a damaged store.

## Limits

- The sender's name is not proof: any program running as the user can write to the store.
- Two builds share one core: a Windows program on Impossible SDK and a native Linux program for WSL. WSL's bridge to Windows hung for 10 seconds and failed in one call of eight on 2026-11-23, so a WSL session never uses it.
- On the `D:` mount Linux cannot rename without replacing, so it makes a file once with a hard link. Windows uses a rename that refuses to replace. Raced on 2026-11-23: 2,000 jobs, two Linux and one Windows racer, one winner each.
- What OS `TODO-08` §55 plans as its own queue folder is replaced by `helio take`; that section is changed in the OS when Heliograph works.

## In what order?

1. Store, kinds, states and `take`, with a crash and race test from Windows and WSL at once.
2. Routing and `helio.json`.
3. Hooks for Claude, then the run steps for Codex and Antigravity. Installing a hook in other repositories is done project by project and only for those the owner names.
4. Watching and the summary.
5. `tools/mail.py` is removed and OS §55 is changed.
