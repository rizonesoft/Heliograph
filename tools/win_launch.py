"""Start a Windows program from WSL, again when WSL's interop relay timed out before it ran.

WSL starts a Windows program by relaying its standard streams over vsock. Under host load the relay can time out
before the program reports anything: the start then exits 1 with stdout empty and stderr exactly
`<3>WSL (<pid> - ) ERROR: UtilAcceptVsock:<line>: accept4 failed 110`. Whether the Windows program ran in that case
cannot be told from WSL, so only a call that is safe to run twice may be started again (`repeatable=True`).

WORKAROUND: WSL interop relay timeout (ETIMEDOUT in UtilAcceptVsock), a WSL platform fault Heliograph cannot fix.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from typing import Any, Callable, Sequence

RELAY_LINE = re.compile(r"^<3>WSL \(\d+ - \S*\) ERROR: UtilAcceptVsock:\d+: accept4 failed 110\s*$")
DELAYS = (0.5, 1.0, 2.0)


def _text(stream: Any) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return str(stream)


def relay_timed_out(result: subprocess.CompletedProcess) -> bool:
    """True only for a start the relay timed out: exit 1, nothing on stdout, and stderr that one relay line."""
    if result.returncode != 1 or _text(result.stdout):
        return False
    err = _text(result.stderr)
    for end in ("\r\n", "\n"):  # one line ending at most; any other line, blank ones too, is not the relay alone
        if err.endswith(end):
            err = err[: -len(end)]
            break
    return "\n" not in err and "\r" not in err and bool(RELAY_LINE.match(err))


def run_windows(
    argv: Sequence[str],
    *,
    repeatable: bool,
    check: bool = False,
    forward: bool = False,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    **run_args: Any,
) -> subprocess.CompletedProcess:
    """Starts argv with both streams captured; while repeatable and the relay timed out, starts it again after 0.5 s,
    1 s and 2 s. Then applies check (CalledProcessError with the last output) and, with forward, writes the last
    stdout and stderr to this process's streams once."""
    run_args.pop("capture_output", None)
    run_args["stdout"] = subprocess.PIPE
    run_args["stderr"] = subprocess.PIPE
    if "input" not in run_args and "stdin" not in run_args:
        run_args["stdin"] = subprocess.DEVNULL
    result = run(list(argv), **run_args)
    for delay in DELAYS:
        if not repeatable or not relay_timed_out(result):
            break
        sleep(delay)
        result = run(list(argv), **run_args)
    if forward:
        for stream, data in ((sys.stdout, result.stdout), (sys.stderr, result.stderr)):
            if not data:
                continue
            if isinstance(data, bytes) and hasattr(stream, "buffer"):
                stream.flush()
                stream.buffer.write(data)  # the bytes as the program wrote them, whatever its code page
                stream.buffer.flush()
            else:
                stream.write(_text(data))
                stream.flush()
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
    return result

