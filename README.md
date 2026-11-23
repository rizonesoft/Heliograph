# Heliograph

Passes messages and jobs between agent sessions on one machine, on Windows and in WSL. A job is taken once, however many sessions try. No service, no network, no model calls. Design: [docs/specs/2026-11-23-heliograph-design.md](docs/specs/2026-11-23-heliograph-design.md).

Status: step 1 works (messages, jobs, the store). Routing by path, delivery through hooks, reminders and the summary are not built.

## Use

```
helio help
```

Say who you are with `--as project/lane` or `HELIO_AS`, and where the store is with `--store` or `HELIO_STORE`. On this machine it is `D:\ImpossibleOS\Heliograph-signals`. Make a store with `helio init`.

Exit codes: 0 done, 1 nothing, 2 refused, 3 a damaged store.

## Build

```
python3 ../ImpossibleSDK/tools/app_build.py .
```

From WSL it builds `helio.exe` for Windows and `linux/helio` for WSL. `engine/` calls neither system; `src/plat_*.c` do.

## Limits

The sender name is not proof: any program running as you can write to the store.

## License

GPL-3.0-only. See [LICENSE](LICENSE).
