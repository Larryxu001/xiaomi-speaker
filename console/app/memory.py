"""Long-term memory: what the assistant has learned about the user.

Same idea as Mem0 / Letta, sized for one household speaker: after a
conversation goes quiet, the chat model reads it together with the current
memories and decides what to add, update or delete. The speaker fetches the
memories when it wakes and keeps them in context for the session.
"""

from __future__ import annotations

import json
import logging
import re

from .db import get_conn, get_settings, now_local

log = logging.getLogger("console.memory")

MAX_MEMORIES = 80          # sent to the speaker, most recently touched first
QUIET_SECONDS = 90         # learn once a conversation has been quiet this long
CATEGORIES = ("身份", "偏好", "作息", "家人朋友", "工作学习", "计划", "其他")


def list_memories() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM memories ORDER BY updated_at DESC, id DESC").fetchall()]


def add_memory(content: str, category: str = "其他", source: str = "手动") -> int:
    content = " ".join((content or "").split())[:200]
    if not content:
        raise ValueError("记忆内容不能为空。")
    category = category if category in CATEGORIES else "其他"
    now = now_local().isoformat(timespec="seconds")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO memories (content, category, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (content, category, source, now, now))
        conn.commit()
        return cur.lastrowid


def update_memory(mem_id: int, content: str, category: str | None = None) -> None:
    content = " ".join((content or "").split())[:200]
    if not content:
        delete_memory(mem_id)
        return
    now = now_local().isoformat(timespec="seconds")
    with get_conn() as conn:
        if category in CATEGORIES:
            conn.execute("UPDATE memories SET content=?, category=?, updated_at=? WHERE id=?",
                         (content, category, now, mem_id))
        else:
            conn.execute("UPDATE memories SET content=?, updated_at=? WHERE id=?", (content, now, mem_id))
        conn.commit()


def delete_memory(mem_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
        conn.commit()


def find_memories(query: str) -> list[dict]:
    words = [w for w in re.split(r"[\s，,。、]+", query or "") if w]
    return [m for m in list_memories() if words and all(w in m["content"] for w in words)] or \
           [m for m in list_memories() if any(w in m["content"] for w in words)]


def speaker_text() -> str:
    """Compact memory list for the speaker's context."""
    mems = list_memories()[:MAX_MEMORIES]
    return "\n".join(f"- {m['content']}" for m in mems)


# ------------------------------------------------------------- learning ----

def record_exchange(user: str, assistant: str) -> None:
    if get_settings().get("memory_enabled", "1") != "1":
        return
    with get_conn() as conn:
        conn.execute("INSERT INTO memory_inbox (user_text, assistant_text, created_at) VALUES (?, ?, ?)",
                     (user[:500], assistant[:500], now_local().isoformat(timespec="seconds")))
        conn.commit()


_SYSTEM = (
    "你负责维护一个私人语音助理对用户的长期记忆。给你现有记忆和一段新对话，"
    "找出值得长期记住的关于用户的事实：称呼、身份、偏好与厌恶、作息习惯、家人朋友、工作学习、长期计划和约定、"
    "用户纠正过助理的地方、用户明确要求记住的事。不要记：一次性的请求（查天气、放首歌）、闲聊内容、助理自己说的话、"
    "临时状态、可以随时查到的公共信息。新信息和旧记忆冲突时更新旧的；用户说不对或要求忘掉时删除。"
    "每条记忆用第三人称写成简短的一句话，如「用户叫阿浪」「用户不吃香菜」「用户每天早上七点起床」。"
    "只输出 JSON，不要别的文字，格式："
    '{"add":[{"content":"...","category":"偏好"}],"update":[{"id":1,"content":"..."}],"delete":[2]}。'
    "category 只能是：" + "、".join(CATEGORIES) + "。没有要改的就输出 {\"add\":[],\"update\":[],\"delete\":[]}。"
)


def _parse_ops(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def learn_pending(force: bool = False) -> str:
    """Digest the inbox once the conversation has been quiet for a while."""
    from datetime import datetime
    from .scheduler import _llm_complete

    settings = get_settings()
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM memory_inbox ORDER BY id").fetchall()]
    if not rows:
        return "没有新对话。"
    last = datetime.fromisoformat(rows[-1]["created_at"])
    if not force and (now_local(settings) - last).total_seconds() < QUIET_SECONDS:
        return "对话还在进行。"
    existing = list_memories()
    memo = "\n".join(f"[{m['id']}] ({m['category']}) {m['content']}" for m in existing) or "（还没有记忆）"
    convo = "\n".join(f"用户：{r['user_text']}\n助理：{r['assistant_text']}" for r in rows)
    user = f"现有记忆：\n{memo}\n\n新对话：\n{convo}"
    try:
        reply = await _llm_complete(settings, _SYSTEM, user, max_tokens=600)
    except Exception as exc:
        log.warning("记忆学习失败，稍后重试：%s", exc)
        return "学习失败，稍后重试。"
    ops = _parse_ops(reply)
    ids = {m["id"] for m in existing}
    added = updated = deleted = 0
    for item in ops.get("add") or []:
        if isinstance(item, dict) and item.get("content"):
            add_memory(item["content"], item.get("category", "其他"), "对话")
            added += 1
    for item in ops.get("update") or []:
        if isinstance(item, dict) and item.get("id") in ids and item.get("content"):
            update_memory(int(item["id"]), item["content"], item.get("category"))
            updated += 1
    for mem_id in ops.get("delete") or []:
        if mem_id in ids:
            delete_memory(int(mem_id))
            deleted += 1
    with get_conn() as conn:
        conn.execute("DELETE FROM memory_inbox WHERE id <= ?", (rows[-1]["id"],))
        conn.commit()
    summary = f"从 {len(rows)} 轮对话里新增 {added} 条、更新 {updated} 条、删除 {deleted} 条记忆。"
    log.info("记忆学习：%s", summary)
    return summary
