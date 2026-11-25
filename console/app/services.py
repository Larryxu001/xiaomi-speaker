"""Console actions shared by the web pages and the voice (MCP) tools.

Anything the user can do by clicking in the console and also by saying
"小爱同学，..." lives here once, so both paths validate the same way.
Functions raise ServiceError with a message that can be shown or spoken.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from urllib.parse import urlparse

from .db import get_conn, get_settings, set_settings

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
RECURRENCES = ("daily", "weekdays", "weekly", "once")
KINDS = ("announcement", "news_analysis")
ANSWER_LENGTHS = {"短": 300, "中": 600, "长": 1200}

_HHMM = re.compile(r"^([01]?\d|2[0-3])[:：]([0-5]\d)$")
_MCP_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class ServiceError(Exception):
    pass


# ----------------------------------------------------------------- tasks ----


def normalize_run_at(recurrence: str, run_at: str) -> str | None:
    run_at = run_at.strip().replace("：", ":")
    if recurrence == "once":
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y/%m/%d %H:%M"):
            try:
                return datetime.strptime(run_at, fmt).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
        return None
    m = _HHMM.match(run_at)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def parse_weekday(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int) or str(value).isdigit():
        n = int(value)
        return n if 0 <= n <= 6 else None
    text = str(value).strip().replace("星期", "周").replace("礼拜", "周")
    for i, name in enumerate(WEEKDAYS):
        if text == name:
            return i
    if text in ("周天", "周七"):
        return 6
    return None


def create_task(name: str, kind: str, recurrence: str, run_at: str,
                payload: str, weekday=None, source_ids: list[int] | None = None, length: str = "standard") -> int:
    name, payload = (name or "").strip(), (payload or "").strip()
    if kind not in KINDS:
        raise ServiceError("任务类型只能是固定提醒或 AI 分析播报。")
    if recurrence not in RECURRENCES:
        raise ServiceError("重复方式只能是每天、工作日、每周或只一次。")
    normalized = normalize_run_at(recurrence, run_at)
    if not normalized:
        hint = "日期和时间，例如 2026-11-20 08:00" if recurrence == "once" else "时间，例如 07:30"
        raise ServiceError(f"时间格式不对，请填写{hint}。")
    source_ids = source_ids or []
    if kind != "news_analysis":
        source_ids = []
    if not payload and source_ids:
        payload = "挑出最重要的内容播报"
    if not payload:
        raise ServiceError("请填写要播报的内容或分析话题。")
    wd = parse_weekday(weekday) if recurrence == "weekly" else None
    if recurrence == "weekly" and wd is None:
        raise ServiceError("每周任务需要指定星期几。")
    if not name:
        name = payload[:12]
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (name, kind, recurrence, run_at, weekday, payload, source_ids, length) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name[:40], kind, recurrence, normalized, wd, payload[:500], ",".join(map(str, source_ids)),
             length if length in ("short", "standard", "long", "full") else "standard"),
        )
        conn.commit()
        return cur.lastrowid


def list_tasks() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM tasks ORDER BY enabled DESC, run_at ASC, id DESC").fetchall()]


def find_task(query: str) -> dict:
    """Find one task by id or by (part of) its name or content."""
    query = (query or "").strip()
    tasks = list_tasks()
    if query.isdigit():
        for t in tasks:
            if t["id"] == int(query):
                return t
    exact = [t for t in tasks if t["name"] == query]
    if len(exact) == 1:
        return exact[0]
    partial = [t for t in tasks if query and (query in t["name"] or query in t["payload"])]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise ServiceError(f"没有找到叫「{query}」的任务。")
    names = "、".join(t["name"] for t in partial[:5])
    raise ServiceError(f"有好几个任务都匹配「{query}」：{names}。请说得更具体一些。")


def set_task_enabled(task_id: int, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET enabled = ?, skip_until = CASE WHEN ? THEN '' ELSE skip_until END "
            "WHERE id = ?",
            (1 if enabled else 0, 1 if enabled else 0, task_id),
        )
        conn.commit()


def set_task_skip_until(task_id: int, day: str) -> None:
    """Hold one task quiet up to and including `day` ('YYYY-MM-DD'), or clear
    the hold with an empty string. The task stays enabled, so it comes back
    by itself and never disappears from the list without the user noticing."""
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET skip_until = ? WHERE id = ?", ((day or "").strip(), task_id))
        conn.commit()


def delete_task(task_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()


def describe_schedule(task: dict) -> str:
    rec, at = task["recurrence"], task["run_at"]
    if rec == "daily":
        return f"每天 {at}"
    if rec == "weekdays":
        return f"工作日 {at}"
    if rec == "weekly":
        return f"每{WEEKDAYS[task['weekday']]} {at}" if task["weekday"] is not None else f"每周 {at}"
    return f"仅一次 {at}"


# -------------------------------------------------------- model profiles ----


# Chat models (llm) and speech recognition (asr) each have their own saved
# profiles; switching applies one to the live settings.
_PROFILE = {
    "llm": {"table": "model_profiles", "prefix": "llm_", "label": "模型", "what": "对话模型"},
    "asr": {"table": "asr_profiles", "prefix": "asr_", "label": "语音识别", "what": "语音识别"},
}


def list_profiles(kind: str = "llm") -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {_PROFILE[kind]['table']} ORDER BY id").fetchall()]


def current_profile_name(settings: dict | None = None, kind: str = "llm") -> str | None:
    s, pre = settings or get_settings(), _PROFILE[kind]["prefix"]
    for p in list_profiles(kind):
        if (p["base_url"].rstrip("/") == s.get(pre + "base_url", "").rstrip("/")
                and p["model"] == s.get(pre + "model") and p["api_key"] == s.get(pre + "api_key")):
            return p["name"]
    return None


def save_current_as_profile(name: str, kind: str = "llm") -> None:
    name = (name or "").strip()
    meta, s = _PROFILE[kind], get_settings()
    pre = meta["prefix"]
    if not name:
        raise ServiceError(f"请给这个{meta['label']}档案起个名字，比如「DeepSeek」。")
    if not s.get(pre + "api_key"):
        raise ServiceError(f"当前{meta['what']}还没有保存 API Key，先保存设置再另存为档案。")
    with get_conn() as conn:
        if kind == "llm":
            conn.execute(
                "INSERT INTO model_profiles (name, protocol, base_url, api_key, model) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET protocol=excluded.protocol, base_url=excluded.base_url, "
                "api_key=excluded.api_key, model=excluded.model",
                (name[:30], s["llm_protocol"], s["llm_base_url"], s["llm_api_key"], s["llm_model"]))
        else:
            conn.execute(
                "INSERT INTO asr_profiles (name, base_url, api_key, model) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET base_url=excluded.base_url, api_key=excluded.api_key, model=excluded.model",
                (name[:30], s["asr_base_url"], s["asr_api_key"], s["asr_model"]))
        conn.commit()


def delete_profile(profile_id: int, kind: str = "llm") -> None:
    with get_conn() as conn:
        conn.execute(f"DELETE FROM {_PROFILE[kind]['table']} WHERE id = ?", (profile_id,))
        conn.commit()


def find_profile(query: str, kind: str = "llm") -> dict:
    said = (query or "").strip()
    query = said.lower()
    profiles, label = list_profiles(kind), _PROFILE[kind]["label"]
    if not profiles:
        raise ServiceError(f"控制台里还没有保存任何{label}档案，请先在网页的「模型与密钥」里另存为档案。")
    for p in profiles:
        if p["name"].lower() == query:
            return p
    hits = [p for p in profiles if query and (query in p["name"].lower() or query in p["model"].lower()
                                              or p["name"].lower() in query)]
    if len(hits) == 1:
        return hits[0]
    names = "、".join(p["name"] for p in profiles)
    if not hits:
        raise ServiceError(f"没有叫「{said}」的{label}档案。现有的是：{names}。")
    raise ServiceError(f"「{said}」能匹配好几个档案：{'、'.join(p['name'] for p in hits)}。")


def apply_profile(profile_id: int, kind: str = "llm") -> dict:
    with get_conn() as conn:
        row = conn.execute(f"SELECT * FROM {_PROFILE[kind]['table']} WHERE id = ?", (profile_id,)).fetchone()
    if not row:
        raise ServiceError("这个档案不存在。")
    pre = _PROFILE[kind]["prefix"]
    values = {pre + "base_url": row["base_url"], pre + "api_key": row["api_key"], pre + "model": row["model"]}
    if kind == "llm":
        values["llm_protocol"] = row["protocol"]
    set_settings(values)
    return dict(row)


def _profile_name_for(base_url: str) -> str:
    host = urlparse(base_url or "").hostname or "默认"
    known = {"deepseek": "DeepSeek", "dashscope": "通义千问", "openai": "OpenAI", "anthropic": "Claude",
             "moonshot": "Kimi", "siliconflow": "硅基流动", "bigmodel": "智谱"}
    return next((v for k, v in known.items() if k in host), host)


def seed_profile_from_current() -> None:
    """First run: turn the working settings into profiles so the lists aren't empty."""
    s = get_settings()
    for kind in ("llm", "asr"):
        pre = _PROFILE[kind]["prefix"]
        if not list_profiles(kind) and s.get(pre + "api_key"):
            save_current_as_profile(_profile_name_for(s.get(pre + "base_url", "")), kind)


# ------------------------------------------------------------- settings ----


def set_web_search(enabled: bool) -> None:
    set_settings({"web_search_enabled": "1" if enabled else "0"})


SEARCH_PROVIDERS = {"tavily": "Tavily", "firecrawl": "Firecrawl"}


def set_search_provider(name: str) -> str:
    said = (name or "").strip().lower()
    provider = next((k for k, v in SEARCH_PROVIDERS.items() if said in (k, v.lower()) or k in said), None)
    if not provider:
        raise ServiceError("联网搜索可以用 Tavily 或 Firecrawl。")
    s = get_settings()
    key_field = {"tavily": "web_search_api_key", "firecrawl": "firecrawl_api_key"}[provider]
    if not s.get(key_field):
        raise ServiceError(f"{SEARCH_PROVIDERS[provider]} 还没有填 API Key，先在网页的「模型与密钥」里填上。")
    set_settings({"search_provider": provider})
    return SEARCH_PROVIDERS[provider]


def set_answer_length(level: str) -> int:
    tokens = ANSWER_LENGTHS.get(level)
    if tokens is None:
        raise ServiceError("回答长度只能是短、中、长。")
    set_settings({"llm_max_tokens": str(tokens)})
    return tokens


# ----------------------------------------------------------- MCP servers ----


def mcp_transport(url: str) -> str:
    """Mirrors the speaker agent: a URL ending in /sse uses legacy SSE."""
    return "sse" if url.strip().rstrip("/").endswith("/sse") else "http"


def is_private_address(url: str) -> bool:
    """True for addresses only a device on the home network can reach."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith((".local", ".lan", ".home", ".internal")) or "." not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


def list_mcp_servers() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM mcp_servers ORDER BY id").fetchall()]


def _clean_tools(tools: str) -> str:
    items = [t.strip() for t in re.split(r"[,，\s]+", tools or "") if t.strip()]
    return ",".join(dict.fromkeys(items))


def save_mcp_server(name: str, url: str, token: str, tools: str, timeout_s: str,
                    note: str = "", server_id: int | None = None) -> None:
    name, url = (name or "").strip().lower(), (url or "").strip()
    if not _MCP_NAME.match(name):
        raise ServiceError("名称只能用小写字母、数字、- 和 _，例如 home-assistant。")
    if name == "console":
        raise ServiceError("console 是控制台自带服务的名字，请换一个。")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ServiceError("地址要以 http:// 或 https:// 开头。")
    try:
        timeout = float(timeout_s or 10)
        if not 1 <= timeout <= 120:
            raise ValueError
    except ValueError:
        raise ServiceError("超时时间要在 1 到 120 秒之间。")
    with get_conn() as conn:
        clash = conn.execute("SELECT id FROM mcp_servers WHERE name = ? AND id IS NOT ?",
                             (name, server_id)).fetchone()
        if clash:
            raise ServiceError(f"已经有一个叫 {name} 的 MCP 服务了。")
        if server_id:
            existing = conn.execute("SELECT token FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
            if not existing:
                raise ServiceError("这个 MCP 服务不存在。")
            conn.execute(
                "UPDATE mcp_servers SET name=?, url=?, token=?, tools=?, timeout_s=?, note=? WHERE id=?",
                (name, url, token.strip() or existing["token"], _clean_tools(tools), timeout, note.strip(), server_id),
            )
        else:
            conn.execute(
                "INSERT INTO mcp_servers (name, url, token, tools, timeout_s, note) VALUES (?, ?, ?, ?, ?, ?)",
                (name, url, token.strip(), _clean_tools(tools), timeout, note.strip()),
            )
        conn.commit()


def set_mcp_enabled(server_id: int, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE mcp_servers SET enabled = ? WHERE id = ?", (1 if enabled else 0, server_id))
        conn.commit()


def delete_mcp_server(server_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM mcp_servers WHERE id = ?", (server_id,))
        conn.commit()
