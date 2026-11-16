"""Show the speaker's activity log as-is.

The speaker agent writes its log in Chinese ("2026-11-14 21:38:45 信息 听到：…"),
so nothing is translated here: lines are only split into time, level and
text for display, newest first.
"""

from __future__ import annotations

import re

_LINE = re.compile(r"^\d{4}-\d\d-\d\d (?P<time>\d\d:\d\d:\d\d) (?P<level>信息|警告|错误|调试|跟踪) (?P<text>.*)$")
_LEVELS = {"错误": "error", "警告": "warn"}


def parse_log(raw: str, limit: int = 60) -> list[dict]:
    events = []
    for line in raw.splitlines():
        m = _LINE.match(line.strip())
        if m:
            events.append({"time": m.group("time"), "text": m.group("text"),
                           "level": _LEVELS.get(m.group("level"), "info")})
    return list(reversed(events))[:limit]
