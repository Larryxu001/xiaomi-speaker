"""Ideas: every evening the assistant thinks of a few things the speaker
could do for the user, from what it knows (memories, recent conversations,
tasks, listening). They wait on the console's 「点子」 page; the user adopts
or drops each one, and those decisions steer the next round.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from .db import get_conn, get_settings, local_tz, now_local

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'new',   -- new | adopted | dropped
    result TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS conversation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_text TEXT NOT NULL,
    assistant_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""
EVENING_HOUR = 22  # think after the day's conversations, before morning tasks

CAPABILITIES = (
    "音箱能做的事（点子只能落在这些上面）：定时播报（固定提醒，或从内容来源生成口播：NewsNow 各平台热榜、RSS、RSSHub、网页，"
    "长度可选简短/标准/详细/完整），添加 NewsNow 来源，一次性提醒，放音乐（网易云/QQ/酷狗/曲库）和网络电台、收藏电台，"
    "控制米家设备（空调、灯等，通过语音），联网搜索回答问题，换音色，记住用户的偏好。"
)

_SYSTEM = (
    "你是用户家里智能音箱上的私人助理，正在晚上复盘今天，想一想明天起能主动为用户多做点什么。\n"
    + CAPABILITIES + "\n"
    "要求：提 3 到 5 个具体点子，每个都必须是这台音箱真能做到、并且贴合这个用户的，不要泛泛而谈，不要提音箱做不到的事。"
    "点子要是具体的「做什么」，不要是对助理自身功能的介绍或使用说明（比如「试试说一句模糊需求」这种不算）。可以大胆一点，宁可新颖也不要重复。不要再提用户已经拒绝过的方向，也不要重复已有的任务和已采纳的点子。\n"
    "能一键落地的点子给出 action：\n"
    '  建定时任务：{"type":"create_task","name":"任务名","content":"播什么/怎么播","time":"07:30","repeat":"daily|weekdays|weekly|once",'
    '"weekday":"周一（weekly 才填）","sources":"来源名，逗号分隔，可空","length":"short|standard|long|full"}\n'
    '  加 NewsNow 来源：{"type":"add_newsnow_source","platform":"平台中文名"}\n'
    '  只是建议、需要用户自己去做：{"type":"none"}\n'
    '只输出 JSON 数组，例如 [{"title":"一句话点子","detail":"为什么适合你、具体会怎样，两三句","action":{...}}]，不要别的文字。'
)


def init() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def log_exchange(user: str, assistant: str) -> None:
    """Keep recent conversations (the memory inbox is emptied after learning)."""
    with get_conn() as conn:
        conn.execute("INSERT INTO conversation_log (user_text, assistant_text, created_at) VALUES (?, ?, ?)",
                     (user[:300], assistant[:300], now_local().isoformat(timespec="seconds")))
        conn.execute("DELETE FROM conversation_log WHERE id NOT IN (SELECT id FROM conversation_log ORDER BY id DESC LIMIT 300)")
        conn.commit()


def list_ideas() -> list[dict]:
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM ideas ORDER BY status = 'new' DESC, id DESC LIMIT 100")]
    for r in rows:
        r["action"] = json.loads(r["action"] or "{}")
    return rows


def _context() -> str:
    from . import radio, services, sources
    with get_conn() as conn:
        mems = [r[0] for r in conn.execute("SELECT content FROM memories ORDER BY updated_at DESC LIMIT 60")]
        talk = [f"用户：{u} / 助理：{a[:80]}" for u, a in conn.execute(
            "SELECT user_text, assistant_text FROM conversation_log ORDER BY id DESC LIMIT 60")]
        past = [(t, s) for t, s in conn.execute(
            "SELECT title, status FROM ideas WHERE status != 'new' ORDER BY id DESC LIMIT 40")]
        pending = [r[0] for r in conn.execute("SELECT title FROM ideas WHERE status = 'new'")]
    tasks = [f"{t['name']}（{services.describe_schedule(t)}）" for t in services.list_tasks()]
    srcs = [s["name"] for s in sources.list_sources()]
    stations = [h["name"] for h in radio.history(15)]
    parts = [
        "关于用户的记忆：\n" + ("\n".join(f"- {m}" for m in mems) or "（还没有）"),
        "最近的对话（新的在前）：\n" + ("\n".join(talk) or "（还没有）"),
        "现有定时任务：" + ("；".join(tasks) or "无"),
        "现有内容来源：" + ("、".join(srcs) or "无"),
        "最近听的电台：" + ("、".join(stations) or "无"),
        "以前的点子和用户的决定：\n" + ("\n".join(f"- {t}：{'采纳' if s == 'adopted' else '不要'}" for t, s in past) or "（还没有）"),
        "还没处理的点子（不要重复）：" + ("；".join(pending) or "无"),
    ]
    profile = get_settings().get("profile_notes", "").strip()
    if profile:
        parts.insert(0, "用户自己写的介绍：" + profile)
    return "\n\n".join(parts)


async def think() -> int:
    """One round of ideas; returns how many were added."""
    from .scheduler import _llm_complete
    settings = get_settings()
    raw = await _llm_complete(settings, _SYSTEM, _context(), max_tokens=1800)
    raw = raw.strip()
    raw = raw[raw.find("["): raw.rfind("]") + 1] if "[" in raw else "[]"
    try:
        items = json.loads(raw)
    except ValueError:
        log.warning("ideas: model did not return JSON: %s", raw[:200])
        return 0
    added = 0
    with get_conn() as conn:
        for it in items[:5]:
            title = str(it.get("title", "")).strip()
            if not title:
                continue
            action = it.get("action") if isinstance(it.get("action"), dict) else {"type": "none"}
            conn.execute("INSERT INTO ideas (title, detail, action, created_at) VALUES (?, ?, ?, ?)",
                         (title[:80], str(it.get("detail", ""))[:400], json.dumps(action, ensure_ascii=False),
                          now_local().isoformat(timespec="seconds")))
            added += 1
        conn.commit()
    log.info("ideas: %d new", added)
    return added


async def maybe_think() -> None:
    """Called from the scheduler loop: once a day, in the evening."""
    settings = get_settings()
    now = datetime.now(local_tz(settings))
    today = now.strftime("%Y-%m-%d")
    if now.hour < EVENING_HOUR or settings.get("ideas_last_date") == today:
        return
    from .db import set_settings
    set_settings({"ideas_last_date": today})
    try:
        await think()
    except Exception as exc:  # pragma: no cover - model/network
        log.error("ideas round failed: %s", exc)


def decide(idea_id: int, adopt: bool) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
    if not row:
        return "这个点子不存在。"
    result = ""
    if adopt:
        result = _apply(json.loads(row["action"] or "{}"))
    with get_conn() as conn:
        conn.execute("UPDATE ideas SET status = ?, result = ?, decided_at = ? WHERE id = ?",
                     ("adopted" if adopt else "dropped", result, now_local().isoformat(timespec="seconds"), idea_id))
        conn.commit()
    return result or ("已采纳。" if adopt else "好，以后少提这类。")


def _apply(action: dict) -> str:
    from . import services
    from . import sources as srcmod
    kind = action.get("type")
    try:
        if kind == "create_task":
            sids = srcmod.resolve_names(action.get("sources", "")) if str(action.get("sources", "")).strip() else []
            task_kind = "news_analysis" if sids or action.get("length") in ("long", "full") else "announcement"
            tid = services.create_task(action.get("name", ""), task_kind, action.get("repeat", "daily"),
                                       action.get("time", "08:00"), action.get("content", ""),
                                       action.get("weekday") or None, sids, action.get("length", "standard"))
            t = next(t for t in services.list_tasks() if t["id"] == tid)
            return f"已建好任务「{t['name']}」，{services.describe_schedule(t)}。"
        if kind == "add_newsnow_source":
            said = str(action.get("platform", "")).strip()
            hit = next((pid for pid, n in srcmod.NEWSNOW_NAMES.items() if said and (said in n or n in said)), None)
            if not hit:
                return f"没找到 NewsNow 平台「{said}」，请在定时任务页手动添加。"
            srcmod.save_source("", "newsnow", hit)
            return f"已添加来源「{srcmod.NEWSNOW_NAMES[hit]}」。"
    except Exception as exc:  # ServiceError / SourceError carry Chinese messages
        return f"没能自动完成：{exc}"
    return "已采纳，这条需要你自己动手。"
