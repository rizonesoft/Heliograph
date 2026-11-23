# Heliograph

A program for Windows and, later, Impossible OS, built on Impossible SDK. Status: started, no release yet.

## Build

Clone Impossible SDK beside this folder at the commit in `sdk.json`, then run this in Windows or WSL:

```
python ../ImpossibleSDK/tools/app_build.py .
```

`engine/` has no window and calls no Windows function. `src/` is the program around it.

## License

GPL-3.0-only. See [LICENSE](LICENSE).
