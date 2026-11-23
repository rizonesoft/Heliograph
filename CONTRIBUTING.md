# Contributing

Thank you for looking at Heliograph. It is early, so a good bug report or a small, proved fix helps most.

## Build and test

Put [Impossible SDK](https://github.com/rizonesoft/ImpossibleSDK) beside this folder, then:

```
python ../ImpossibleSDK/tools/bootstrap.py
python ../ImpossibleSDK/tools/app_build.py .
```

Use Windows or WSL (there the command is `python3`). In WSL, `python3 ../ImpossibleSDK/tools/prove.py .` lets Windows run the tests against the committed programs; a change counts when that passes.

## What a change needs

- A test that fails before it and passes after it.
- `engine/` calls no system function and uses no floating point. What a system must give is in `engine/plat.h`.
- A change to the store keeps it readable by the program before it, and nothing is lost when the program is killed at any moment.
- A job still has one taker when Windows and Linux race for it.
- No new Windows function: the program is tier 0.

A feature that two programs need belongs in Impossible SDK. The design is in [`docs/specs`](docs/specs/2026-11-23-heliograph-design.md), and [`todo/`](todo/implementation-plan.md) is the work list.
