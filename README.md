# Heliograph

<a href="https://github.com/rizonesoft/ImpossibleSDK"><picture><source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/rizonesoft/ImpossibleSDK/main/brand/badge-dark.png"><img alt="Built with Impossible SDK" src="https://raw.githubusercontent.com/rizonesoft/ImpossibleSDK/main/brand/badge-light.png" height="34"></picture></a>

Passes messages and jobs between agent sessions on one machine, on Windows and in WSL. A job is taken once, however many sessions try. No service, no network, no model calls. Design: [docs/specs/2026-11-23-heliograph-design.md](docs/specs/2026-11-23-heliograph-design.md).

Status: messages, jobs, routing by path, the Claude hook, overdue jobs, overlap warnings and the daily summary work. Codex and Antigravity read with `helio` between sections. Nothing runs between calls, so a reminder appears when someone next works.

## Use

```
helio help
```

Say who you are with `--as project/lane` or `HELIO_AS`, and where the store is with `--store` or `HELIO_STORE`. On this machine it is `~/ImpossibleOS/Heliograph-signals` in WSL. Make a store with `helio init`.

Exit codes: 0 done, 1 nothing, 2 refused, 3 something failed or is damaged, and what was asked may have happened. Send with `--key` to repeat a send safely. `helio check` examines the store.

## Build

```
python3 ../ImpossibleSDK/tools/app_build.py .
```

It uses the SDK commit in `sdk.json`, also when the SDK has moved on. From WSL it builds `helio.exe` for Windows and `linux/helio` for WSL. `engine/` calls neither system; `src/plat_*.c` do.

## Limits

The sender name is not proof: any program running as you can write to the store.

## License

GPL-3.0-only. See [LICENSE](LICENSE).

Made for the sessions that build [Impossible OS](https://github.com/rizonesoft/impossible-os), with [Impossible SDK](https://github.com/rizonesoft/ImpossibleSDK).
