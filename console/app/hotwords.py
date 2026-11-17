"""Words the speech recogniser is told to expect.

Short commands are the easiest to mishear ("停止" came back as "天资"), and
names it has never seen — a station, a singer, a shortcut the user invented —
come back as something that sounds alike. The recogniser takes a list of
likely words, so the console keeps that list up to date from what this
household actually says and listens to.
"""

from __future__ import annotations

from .db import get_conn, get_settings

# What every household says to a speaker. Ordered by how often it is said and
# how badly a mistake hurts: stopping comes first.
COMMANDS = [
    "停止", "停下", "别放了", "关掉", "暂停", "继续", "安静",
    "下一首", "下一曲", "下一个", "上一首", "上一曲", "换一首", "换一曲",
    "换一个", "换个台", "换一台", "切歌", "跳过", "再换一个", "换首歌",
    "大点声", "小点声", "音量调到", "静音",
    "放首歌", "来点音乐", "放个电台", "听广播", "讲个故事", "放新闻",
    "今天天气", "明天天气", "现在几点", "今天几号", "星期几",
    "打开空调", "关闭空调", "打开灯", "关掉灯", "调高温度", "调低温度",
    "设置闹钟", "提醒我", "定时播报", "早报", "新闻",
    "小爱同学", "你会做什么", "再见",
]
LIMIT = 500  # keep the hint short: a long list dilutes every word in it


def build(settings: dict[str, str] | None = None) -> str:
    """Commands first, then the names this speaker actually hears."""
    settings = settings or get_settings()
    words: list[str] = list(COMMANDS)

    def add(items) -> None:
        for w in items:
            w = (w or "").strip()
            if 1 < len(w) <= 20 and w not in words:
                words.append(w)

    from . import shortcuts
    add(s["say"] for s in shortcuts.load(settings))
    with get_conn() as conn:
        add(r[0] for r in conn.execute("SELECT name FROM radio_favorites ORDER BY added_at DESC LIMIT 10"))
        add(r[0] for r in conn.execute(
            "SELECT name FROM radio_history ORDER BY plays DESC, last_played DESC LIMIT 10"))
        add(r[0] for r in conn.execute("SELECT name FROM tasks WHERE enabled = 1"))
        add(r[0] for r in conn.execute("SELECT name FROM sources ORDER BY id DESC LIMIT 12"))
        add(r[0] for r in conn.execute("SELECT name FROM model_profiles"))
    out: list[str] = []
    for w in words:
        if sum(len(x) + 1 for x in out) + len(w) > LIMIT:
            break
        out.append(w)
    return "、".join(out)
