"""Response speed: the speaker's log already says how long each sentence
took (recognition, time to the first spoken word); every status report is
scanned and the numbers are kept, so the overview can show how fast the
speaker has been answering and how often recognition failed.
"""

from __future__ import annotations

import re
from datetime import timedelta

from .db import get_conn, now_local

SCHEMA = """
CREATE TABLE IF NOT EXISTS speed_events (
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,          -- llm | fast | asr_fail | no_speech | llm_fail
    asr REAL,                    -- seconds to recognise the sentence
    act REAL,                    -- seconds from recognised to speaking / done
    total REAL,                  -- seconds until the whole answer was written
    detail TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (ts, kind, detail)
);
"""
_TS = r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \S+ "
_PATTERNS = [
    ("llm", re.compile(_TS + r"回答：(.*)（识别 ([\d.]+) 秒，思考到开口 ([\d.]+) 秒，写完 ([\d.]+) 秒）")),
    ("fast", re.compile(_TS + r"快速指令 (.*?) → .*（识别 ([\d.]+) 秒，执行 ([\d.]+) 秒）")),
    ("asr_fail", re.compile(_TS + r"语音识别服务「(.*?)」不可用")),
    ("no_speech", re.compile(_TS + r"(没等到说话)")),
    ("llm_fail", re.compile(_TS + r"(大模型调用失败)")),
]


def init() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def ingest(log_text: str) -> None:
    rows = []
    for line in log_text.splitlines():
        for kind, pat in _PATTERNS:
            m = pat.search(line)
            if not m:
                continue
            g = m.groups()
            if kind == "llm":
                if "【忽略】" in g[1]:
                    break  # not addressed to the speaker
                rows.append((g[0], kind, float(g[2]), float(g[3]), float(g[4]), g[1][:60]))
            elif kind == "fast":
                rows.append((g[0], kind, float(g[2]), float(g[3]), None, g[1][:60]))
            else:
                rows.append((g[0], kind, None, None, None, g[1][:60]))
            break
    if not rows:
        return
    with get_conn() as conn:
        conn.executemany("INSERT OR IGNORE INTO speed_events VALUES (?, ?, ?, ?, ?, ?)", rows)
        conn.execute("DELETE FROM speed_events WHERE ts < ?",
                     ((now_local() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),))
        conn.commit()


def _window(hours: int) -> dict:
    since = (now_local() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM speed_events WHERE ts >= ?", (since,))]
    llm = [r for r in rows if r["kind"] == "llm"]
    fast = [r for r in rows if r["kind"] == "fast"]
    heard = llm + fast

    def avg(values):
        values = [v for v in values if v is not None]
        return round(sum(values) / len(values), 1) if values else None

    slow = max(llm, key=lambda r: r["asr"] + r["act"], default=None)
    return {
        "sentences": len(heard),
        "asr": avg(r["asr"] for r in heard),
        "llm_first": avg(r["asr"] + r["act"] for r in llm),   # heard -> first spoken word
        "fast_done": avg(r["asr"] + r["act"] for r in fast),  # heard -> done
        "asr_fail": sum(r["kind"] == "asr_fail" for r in rows),
        "no_speech": sum(r["kind"] == "no_speech" for r in rows),
        "llm_fail": sum(r["kind"] == "llm_fail" for r in rows),
        "slowest": {"secs": round(slow["asr"] + slow["act"], 1), "what": slow["detail"], "ts": slow["ts"][5:16]} if slow else None,
    }


def summary() -> dict:
    return {"day": _window(24), "week": _window(24 * 7)}
