"""Path glob matching with `**` support (case-insensitive)."""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=1024)
def _compile(pattern: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile("^" + out + "$", re.IGNORECASE)


def match(path: str, pattern: str) -> bool:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    rx = _compile(pattern)
    # A bare module path such as "app/domain" should also match "**/domain/**".
    return bool(rx.match(path) or rx.match(path + "/_"))


def match_any(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    return any(match(path, p) for p in patterns)
