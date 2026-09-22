"""Out-of-process symlink resolver for the sensitive-path gate.

Why a separate PROCESS rather than the ``mc-pathres`` thread pool alone:
``os.path.realpath`` is pure Python that issues one ``lstat`` and one
``readlink`` per path component, and every one of those syscalls releases and
then re-acquires the GIL. Inside a gateway running 100+ threads each
re-acquisition can wait a full switch interval, so a ~130-call anchor rebuild
that costs 2 ms in an idle interpreter costs seconds beside a few busy threads
and expires its budget on a healthy local disk. A helper process has its own
GIL: the pool worker pays ONE pipe round-trip per request, and a helper genuinely
wedged on a dead mount can be KILLED -- which a timed-out thread cannot be.

This module imports nothing from ``kiro_crew``. :mod:`pathres_client` reads its
SOURCE once at import and runs the child as ``python -I -S -c <source>``, never
the file by path (see there for why).

Protocol: newline-delimited JSON. Request ``{"i": n, "p": "<path>"}`` answers
``{"i": n, "r": [realpath|null, resolve|null]}``; request ``{"i": n, "p": [...]}``
answers ``{"i": n, "r": [realpath|null, ...]}`` in order (anchors need one
spelling, and on Windows each spelling is a handle open per path component).
``ensure_ascii`` on both sides keeps surrogate-escaped bytes intact. A malformed
line ends the process, so a corrupted helper never answers the wrong request.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _resolve_one(path: str, *, both: bool = True) -> list[str | None]:
    """``[realpath, resolve]`` for *path*; ``None`` where that spelling raised."""
    try:
        real: str | None = os.path.realpath(path)
    except (OSError, ValueError):
        real = None
    if not both:
        return [real, None]
    try:
        resolved: str | None = str(Path(path).resolve())
    except (OSError, ValueError, RuntimeError):
        resolved = None
    return [real, resolved]


def serve(stdin, stdout) -> None:
    """Answer requests until EOF or a malformed line."""
    for raw in stdin:
        try:
            request = json.loads(raw)
            paths = request["p"]
            if isinstance(paths, str):
                answer: object = _resolve_one(paths)
            elif isinstance(paths, list) and all(isinstance(p, str) for p in paths):
                answer = [_resolve_one(p, both=False)[0] for p in paths]
            else:
                return
            reply = {"i": request["i"], "r": answer}
        except (ValueError, KeyError, TypeError):
            return
        stdout.write(json.dumps(reply, ensure_ascii=True))
        stdout.write("\n")
        stdout.flush()


if __name__ == "__main__":
    serve(
        open(sys.stdin.fileno(), "r", encoding="ascii", errors="surrogateescape"),
        open(sys.stdout.fileno(), "w", encoding="ascii", errors="surrogateescape"),
    )
