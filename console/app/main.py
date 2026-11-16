"""XiaoAI Console: the one web page for managing the speaker.

Two audiences hit this app:
  - a human, through the browser, logged in with one admin password
  - the speaker itself, through deploy/console-poller.sh, authenticated
    with a long random device token and speaking plain text, not JSON
    (BusyBox on the speaker has no jq/python/base64)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac

import httpx
import json
import time
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth
from .config_render import render_agent_yaml
from .db import get_conn, get_settings, init_db, now_local, set_settings
from .providers import ProviderError, list_models, test_asr, test_llm, test_search, web_search
from . import apps_service, ideas, installer_client, shortcuts, speed
from .log_view import parse_log
from .mcp_probe import list_remote_tools
from .mcp_server import DeviceTokenAuth, build_mcp_app, mcp
from .scheduler import queue_task_now, run_scheduler_forever
from . import commands, memory, music, radio, services, sources, tts, wake
from .services import ServiceError, seed_profile_from_current

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("console")

BASE_DIR = Path(__file__).resolve().parent
init_db()  # must run before reading session_secret below
sources.seed_defaults()
music.seed_defaults()
ideas.init()
speed.init()

# Hosts the console's MCP endpoint answers to (DNS-rebinding protection).
ALLOWED_HOSTS = [h.strip() for h in os.environ.get(
    "CONSOLE_ALLOWED_HOSTS", "xiaomi.speaker.xulangli.com.cn,127.0.0.1:*,localhost:*,testserver"
).split(",") if h.strip()]
mcp_app = build_mcp_app(ALLOWED_HOSTS)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    seed_profile_from_current()
    scheduler = asyncio.create_task(run_scheduler_forever())
    async with mcp.session_manager.run():
        yield
    scheduler.cancel()


app = FastAPI(title="XiaoAI Console", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=get_settings()["session_secret"])
# The speaker reaches the console's own tools here (see mcp_server.py).
app.mount("/mcp", DeviceTokenAuth(mcp_app))
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

ONLINE_WINDOW_S = 60  # poller checks in every ~20s; three missed beats = offline
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    if ts.tzinfo is None:  # rows written before timestamps became zone-aware
        ts = ts.replace(tzinfo=now_local().tzinfo)
    return ts


def fmt_time(value: str | None) -> str:
    ts = _parse_ts(value)
    if not ts:
        return ""
    now = now_local()
    ts = ts.astimezone(now.tzinfo)
    if ts.date() == now.date():
        return ts.strftime("今天 %H:%M")
    return f"{ts.month}月{ts.day}日 {ts:%H:%M}"


def fmt_ago(value: str | None) -> str:
    ts = _parse_ts(value)
    if not ts:
        return ""
    seconds = int((now_local() - ts).total_seconds())
    if seconds < 60:
        return f"{max(seconds, 1)} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return fmt_time(value)


def fmt_schedule(task) -> str:
    rec, at = task["recurrence"], task["run_at"]
    if rec == "daily":
        return f"每天 {at}"
    if rec == "weekdays":
        return f"工作日 {at}"
    if rec == "weekly":
        if task["weekday"] is None:
            return f"每周 {at}"
        return f"每{WEEKDAYS[task['weekday']]} {at}"
    return f"仅一次 {at}"


def key_tail(value: str | None) -> str:
    value = (value or "").strip()
    if not value or value == "EMPTY":
        return ""
    return value[-4:]


MASK = "••••••••"


def key_mask(value: str | None) -> str:
    """sk-u••••••••f895: enough to recognise a saved key, never the key."""
    value = (value or "").strip()
    if not value or value == "EMPTY":
        return ""
    if len(value) <= 10:
        return MASK + value[-2:]
    return value[:4] + MASK + value[-4:]


def submitted_key(value: str, stored: str) -> str:
    """A field still showing the mask (or left empty) keeps the stored key."""
    value = (value or "").strip()
    return stored if (not value or MASK in value) else value


templates.env.filters["fmt_time"] = fmt_time
templates.env.filters["fmt_ago"] = fmt_ago
templates.env.globals["fmt_schedule"] = fmt_schedule




# ---------------------------------------------------------------------------
# Human-facing pages (require login, except /login and /setup)
# ---------------------------------------------------------------------------


def _require_login(request: Request) -> RedirectResponse | None:
    if auth.is_first_run():
        return RedirectResponse("/setup", status_code=303)
    if not request.session.get("logged_in"):
        return RedirectResponse("/login", status_code=303)
    return None


@app.get("/setup", response_class=HTMLResponse)
async def setup_get(request: Request):
    if not auth.is_first_run():
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {})


@app.post("/setup")
async def setup_post(request: Request, password: str = Form(...), confirm: str = Form(...)):
    if not auth.is_first_run():
        return RedirectResponse("/", status_code=303)
    if len(password) < 8 or password != confirm:
        return templates.TemplateResponse(
            request, "setup.html", {"error": "两次密码要一致，且至少 8 位"}, status_code=400
        )
    auth.set_admin_password(password)
    request.session["logged_in"] = True
    return RedirectResponse("/", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    if auth.is_first_run():
        return RedirectResponse("/setup", status_code=303)
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/login")
async def login_post(request: Request, password: str = Form(...)):
    if not auth.check_admin_password(password):
        return templates.TemplateResponse(
            request, "login.html", {"error": "密码不对"}, status_code=401
        )
    request.session["logged_in"] = True
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    settings = get_settings()
    with get_conn() as conn:
        status = conn.execute("SELECT * FROM device_status WHERE id = 1").fetchone()
        pending = conn.execute(
            "SELECT * FROM announcements WHERE delivered_at IS NULL ORDER BY id ASC"
        ).fetchall()
        recent = conn.execute(
            "SELECT * FROM announcements WHERE delivered_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 8"
        ).fetchall()
        task_count = conn.execute("SELECT COUNT(*) FROM tasks WHERE enabled = 1").fetchone()[0]
    last_seen = _parse_ts(status["last_seen_at"]) if status else None
    online = bool(last_seen) and (now_local() - last_seen).total_seconds() < ONLINE_WINDOW_S
    report = (status["last_report"] or "") if status else ""
    agent_line = report.splitlines()[0] if report else ""
    agent_running = "running with PID" in agent_line
    log_text = report.split("--- recent log ---", 1)[-1].strip() if report else ""
    log_text = re.sub(r"\x1b\[[0-9;]*m", "", log_text)  # agent logs carry ANSI colours
    log_events = parse_log(log_text)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "status": status,
            "online": online,
            "agent_running": agent_running,
            "log_text": log_text,
            "log_events": log_events,
            "pending": pending,
            "recent": recent,
            "task_count": task_count,
            "speed": speed.summary(),
            "s": settings,
        },
    )


def _profile_ctx(request: Request) -> dict:
    return {"profiles": services.list_profiles("llm"), "current_profile": services.current_profile_name(kind="llm"),
            "asr_profiles": services.list_profiles("asr"), "current_asr_profile": services.current_profile_name(kind="asr"),
            "profile_error": request.query_params.get("perr", "")}


@app.get("/config", response_class=HTMLResponse)
async def config_get(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    s = get_settings()
    tails = {
        "llm": key_mask(s.get("llm_api_key")),
        "asr": key_mask(s.get("asr_api_key")),
        "search": key_mask(s.get("web_search_api_key")),
        "firecrawl": key_mask(s.get("firecrawl_api_key")),
    }
    return templates.TemplateResponse(request, "config.html", {"s": s, "tails": tails, "voices": tts.VOICES,
                                                               "voice_label": tts.LABELS.get(s.get("tts_voice"), "小爱同学"),
                                                               **_profile_ctx(request)})


@app.get("/api/tts/preview")
async def tts_preview(request: Request, voice: str = Query(...), rate: str = Query("normal")):
    if not request.session.get("logged_in"):
        return JSONResponse({"ok": False, "message": "请先登录。"}, status_code=401)
    if voice not in tts.LABELS or voice == tts.NATIVE:
        return JSONResponse({"ok": False, "message": "小爱原声只能在音箱上试听。"}, status_code=400)
    settings = {**get_settings(), "tts_rate": rate if rate in tts.RATES else "normal"}
    try:
        audio = await tts.synthesize(f"你好，我是{tts.LABELS[voice].split(' · ')[0]}，以后由我来回答你的问题。", voice, settings)
    except tts.TtsError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=502)
    return Response(audio, media_type=tts.media_type(voice))


@app.post("/api/tts/speaker-test")
async def tts_speaker_test(request: Request):
    if not request.session.get("logged_in"):
        return JSONResponse({"ok": False}, status_code=401)
    with get_conn() as conn:
        conn.execute("INSERT INTO announcements (task_id, text) VALUES (NULL, ?)", ("你好，这是现在的音色，听起来怎么样？",))
        conn.commit()
    return JSONResponse({"ok": True})


@app.post("/config")
async def config_post(
    request: Request,
    llm_protocol: str = Form(...),
    llm_base_url: str = Form(...),
    llm_api_key: str = Form(""),
    llm_model: str = Form(...),
    llm_max_tokens: str = Form("600"),
    llm_temperature: str = Form("0.5"),
    asr_base_url: str = Form(...),
    asr_api_key: str = Form(""),
    asr_model: str = Form(...),
    web_search_enabled: str = Form("0"),
    web_search_api_key: str = Form(""),
    search_provider: str = Form("tavily"),
    firecrawl_api_key: str = Form(""),
    profile_notes: str = Form(""),
    timezone: str = Form("Asia/Shanghai"),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    current = get_settings()
    new_values = {
        "llm_protocol": llm_protocol,
        "llm_base_url": llm_base_url.strip().rstrip("/"),
        "llm_model": llm_model.strip(),
        "llm_max_tokens": llm_max_tokens.strip(),
        "llm_temperature": llm_temperature.strip(),
        "asr_base_url": asr_base_url.strip().rstrip("/"),
        "asr_model": asr_model.strip(),
        "web_search_enabled": "1" if web_search_enabled == "1" else "0",
        "search_provider": search_provider if search_provider in ("tavily", "firecrawl") else "tavily",
        "profile_notes": profile_notes.strip(),
        "timezone": timezone.strip() or "Asia/Shanghai",
    }

    def same_host(a: str, b: str) -> bool:
        return (a or "").rstrip("/") == (b or "").rstrip("/")

    errors = []
    if llm_protocol not in ("open_ai_chat", "anthropic"):
        errors.append("接口协议无效。")
    try:
        if not 50 <= int(new_values["llm_max_tokens"]) <= 4000:
            raise ValueError
    except ValueError:
        errors.append("回答长度上限要在 50 到 4000 之间。")
    try:
        if not 0 <= float(new_values["llm_temperature"]) <= 1:
            raise ValueError
    except ValueError:
        errors.append("随机程度要在 0 到 1 之间。")
    try:
        ZoneInfo(new_values["timezone"])
    except Exception:
        errors.append("时区名称无效，例如 Asia/Shanghai。")
    # A saved key must never be sent to a different provider: switching the
    # address requires pasting the new provider's key.
    if MASK in llm_api_key:
        llm_api_key = ""
    if MASK in asr_api_key:
        asr_api_key = ""
    if MASK in web_search_api_key:
        web_search_api_key = ""
    if MASK in firecrawl_api_key:
        firecrawl_api_key = ""
    if not llm_api_key and not same_host(new_values["llm_base_url"], current.get("llm_base_url")):
        errors.append("对话模型换了接口地址，请填写对应的 API Key。")
    if not asr_api_key and not same_host(new_values["asr_base_url"], current.get("asr_base_url")):
        errors.append("语音识别换了接口地址，请填写对应的 API Key。")
    if errors:
        shown = {**current, **new_values}
        tails = {
            "llm": key_mask(current.get("llm_api_key")),
            "asr": key_mask(current.get("asr_api_key")),
            "search": key_mask(current.get("web_search_api_key")),
            "firecrawl": key_mask(current.get("firecrawl_api_key")),
        }
        return templates.TemplateResponse(
            request, "config.html", {"s": shown, "tails": tails, "errors": errors, "voices": tts.VOICES,
                                     **_profile_ctx(request)}, status_code=400
        )

    # Blank key fields keep the stored key, so the page never has to send a
    # saved secret back to the browser.
    new_values["llm_api_key"] = submitted_key(llm_api_key, current.get("llm_api_key", ""))
    new_values["asr_api_key"] = submitted_key(asr_api_key, current.get("asr_api_key", ""))
    new_values["web_search_api_key"] = submitted_key(web_search_api_key, current.get("web_search_api_key", ""))
    new_values["firecrawl_api_key"] = submitted_key(firecrawl_api_key, current.get("firecrawl_api_key", ""))
    form = await request.form()
    voice, rate = str(form.get("tts_voice", "")), str(form.get("tts_rate", "normal"))
    if voice in tts.LABELS:
        new_values["tts_voice"] = voice
    if rate in tts.RATES:
        new_values["tts_rate"] = rate
    new_values["barge_in"] = "1" if form.get("barge_in") == "1" else "0"
    if str(form.get("listen_seconds", "")) in ("10", "15", "20", "40", "60"):
        new_values["listen_seconds"] = str(form.get("listen_seconds"))
    new_values.update({"asr_fallbacks": "[]", "llm_fallback_profiles": ""})  # backups were removed
    set_settings(new_values)
    return RedirectResponse("/config?msg=saved", status_code=303)


class ProviderProbe(BaseModel):
    kind: str                      # "llm" | "asr" | "search"
    protocol: str = "open_ai_chat"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    provider: str = "tavily"       # search only


def _probe_key(probe: ProviderProbe, settings: dict[str, str]) -> str:
    """Typed key wins. Otherwise reuse the saved key, but only against the
    address it was saved for, so a stored key never reaches another host."""
    if probe.api_key.strip() and MASK not in probe.api_key:
        return probe.api_key.strip()
    if probe.kind == "search":
        return settings.get({"firecrawl": "firecrawl_api_key"}.get(probe.provider, "web_search_api_key"), "")
    saved_url = settings.get(f"{probe.kind}_base_url", "").rstrip("/")
    if probe.base_url.strip().rstrip("/") == saved_url:
        return settings.get(f"{probe.kind}_api_key", "")
    return ""


def _probe_guard(request: Request, probe: ProviderProbe) -> JSONResponse | None:
    if not request.session.get("logged_in"):
        return JSONResponse({"ok": False, "message": "登录已过期，请刷新页面重新登录。"}, status_code=401)
    if probe.kind not in ("llm", "asr", "search"):
        return JSONResponse({"ok": False, "message": "未知的检测类型。"}, status_code=400)
    return None


@app.post("/api/providers/models")
async def api_list_models(request: Request, probe: ProviderProbe):
    guard = _probe_guard(request, probe)
    if guard:
        return guard
    key = _probe_key(probe, get_settings())
    if not key:
        return JSONResponse({"ok": False, "message": "请先填写 API Key。换了接口地址时需要填新地址对应的 Key。"})
    protocol = probe.protocol if probe.kind == "llm" else "open_ai_chat"
    try:
        models = await list_models(protocol, probe.base_url.strip(), key, probe.kind)
    except ProviderError as exc:
        return JSONResponse({"ok": False, "message": str(exc)})
    return JSONResponse({"ok": True, "models": models, "message": f"找到 {len(models)} 个模型。"})


@app.post("/api/providers/test")
async def api_test_provider(request: Request, probe: ProviderProbe):
    guard = _probe_guard(request, probe)
    if guard:
        return guard
    key = _probe_key(probe, get_settings())
    if not key:
        return JSONResponse({"ok": False, "message": "请先填写 API Key。换了接口地址时需要填新地址对应的 Key。"})
    try:
        if probe.kind == "llm":
            message = await test_llm(probe.protocol, probe.base_url.strip(), key, probe.model.strip())
        elif probe.kind == "asr":
            message = await test_asr(probe.base_url.strip(), key, probe.model.strip())
        else:
            message = await test_search(probe.provider, key)
    except ProviderError as exc:
        return JSONResponse({"ok": False, "message": str(exc)})
    return JSONResponse({"ok": True, "message": message})


@app.get("/tasks", response_class=HTMLResponse)
async def tasks_get(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    return _render_tasks(request)


def _render_tasks(request: Request, error: str = "", form: dict | None = None, status_code: int = 200):
    with get_conn() as conn:
        tasks = conn.execute("SELECT * FROM tasks ORDER BY enabled DESC, run_at ASC, id DESC").fetchall()
    srcs = sources.list_sources()
    names = {s["id"]: s["name"] for s in srcs}
    tasks = [{**dict(t), "source_names": [names.get(i, "已删除的来源") for i in sources.ids_from_text(t["source_ids"])]}
             for t in tasks]
    return templates.TemplateResponse(
        request, "tasks.html", {"tasks": tasks, "error": error, "form": form or {}, "sources": srcs,
                                "source_kinds": sources.KINDS, "newsnow_platforms": sources.NEWSNOW_PLATFORMS,
                                "rsshub_routes": sources.RSSHUB_ROUTES,
                                "source_error": request.query_params.get("source_err", "")},
        status_code=status_code,
    )


@app.post("/tasks")
async def tasks_post(
    request: Request,
    name: str = Form(...),
    kind: str = Form(...),
    recurrence: str = Form(...),
    run_at: str = Form(...),
    weekday: str = Form(""),
    payload: str = Form(""),
    length: str = Form("standard"),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    form_data = await request.form()
    source_ids = [int(v) for v in form_data.getlist("source_ids") if str(v).isdigit()]
    # Ticked order (from the page) decides the briefing order.
    ordered = [int(v) for v in str(form_data.get("source_order", "")).split(",") if v.strip().isdigit()]
    source_ids = [i for i in ordered if i in source_ids] + [i for i in source_ids if i not in ordered]
    form = {"name": name, "kind": kind, "recurrence": recurrence, "run_at": run_at,
            "weekday": weekday, "payload": payload, "source_ids": source_ids}
    try:
        services.create_task(name, kind, recurrence, run_at, payload, weekday, source_ids, length)
    except ServiceError as exc:
        return _render_tasks(request, str(exc), form, 400)
    return RedirectResponse("/tasks?msg=created", status_code=303)


@app.post("/sources")
async def sources_post(request: Request, name: str = Form(""), kind: str = Form(...), target: str = Form(""),
                       newsnow_id: str = Form(""), newsnow_custom: str = Form(""), rsshub_route: str = Form(""),
                       rsshub_custom: str = Form(""), follow: str = Form(""),
                       max_items: int = Form(10)):
    redirect = _require_login(request)
    if redirect:
        return redirect
    try:
        if kind == "newsnow":
            picked = (await request.form()).getlist("newsnow_ids")
            ids = [str(p) for p in picked] + ([newsnow_custom.strip()] if newsnow_custom.strip() else [])
            if not ids and newsnow_id:
                ids = [newsnow_id]
            # The ticks are the whole list: add new ones, remove unticked ones
            # (platforms typed in by code aren't shown as ticks, so they stay).
            listed = set(sources.NEWSNOW_NAMES)
            current = [s for s in sources.list_sources() if s["kind"] == "newsnow"]
            have = {s["target"] for s in current}
            for s in current:
                if s["target"] in listed and s["target"] not in ids:
                    sources.delete_source(s["id"])
            for pid in ids:
                if pid not in have:
                    sources.save_source(name if len(ids) == 1 else "", kind, pid, follow, max_items)
        else:
            rh = rsshub_custom.strip() or rsshub_route
            sources.save_source(name, kind, {"rsshub": rh}.get(kind, target), follow, max_items)
    except sources.SourceError as exc:
        from urllib.parse import quote
        return RedirectResponse(f"/tasks?source_err={quote(str(exc))}#sources", status_code=303)
    return RedirectResponse("/tasks#sources", status_code=303)


@app.post("/sources/{source_id}/delete")
async def sources_delete(request: Request, source_id: int):
    redirect = _require_login(request)
    if redirect:
        return redirect
    sources.delete_source(source_id)
    return RedirectResponse("/tasks#sources", status_code=303)


@app.get("/api/sources/{source_id}/preview")
async def sources_preview(request: Request, source_id: int):
    if not request.session.get("logged_in"):
        return JSONResponse({"ok": False, "message": "请先登录。"}, status_code=401)
    src = next((s for s in sources.list_sources() if s["id"] == source_id), None)
    if not src:
        return JSONResponse({"ok": False, "message": "来源不存在。"}, status_code=404)
    try:
        items = await sources.fetch_source(src)
    except sources.SourceError as exc:
        return JSONResponse({"ok": False, "message": str(exc)})
    return JSONResponse({"ok": True, "items": [i[:300] for i in items[:10]]})


@app.post("/tasks/{task_id}/length")
async def tasks_length(request: Request, task_id: int, length: str = Form(...)):
    redirect = _require_login(request)
    if redirect:
        return redirect
    if length in ("short", "standard", "long", "full"):
        with get_conn() as conn:
            conn.execute("UPDATE tasks SET length = ? WHERE id = ?", (length, task_id))
            conn.commit()
    return RedirectResponse("/tasks", status_code=303)


@app.post("/tasks/{task_id}/toggle")
async def tasks_toggle(request: Request, task_id: int):
    redirect = _require_login(request)
    if redirect:
        return redirect
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET enabled = 1 - enabled WHERE id = ?", (task_id,))
        conn.commit()
    return RedirectResponse("/tasks", status_code=303)


@app.post("/tasks/{task_id}/run")
async def tasks_run_now(request: Request, task_id: int):
    """Queue a task immediately so the user can hear it without waiting."""
    redirect = _require_login(request)
    if redirect:
        return redirect
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return RedirectResponse("/tasks", status_code=303)
    await queue_task_now(dict(row), get_settings())
    return RedirectResponse("/tasks?msg=queued", status_code=303)


@app.post("/tasks/{task_id}/delete")
async def tasks_delete(request: Request, task_id: int):
    redirect = _require_login(request)
    if redirect:
        return redirect
    with get_conn() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
    return RedirectResponse("/tasks?msg=deleted", status_code=303)


@app.post("/announcements/now")
async def announce_now(request: Request, text: str = Form(...)):
    """Say something right away, bypassing the task scheduler."""
    redirect = _require_login(request)
    if redirect:
        return redirect
    text = " ".join(text.split())
    if text:
        with get_conn() as conn:
            conn.execute("INSERT INTO announcements (task_id, text) VALUES (NULL, ?)", (text,))
            conn.commit()
    return RedirectResponse("/?msg=sent" if text else "/", status_code=303)


@app.post("/announcements/{ann_id}/cancel")
async def announce_cancel(request: Request, ann_id: int):
    redirect = _require_login(request)
    if redirect:
        return redirect
    with get_conn() as conn:
        conn.execute("DELETE FROM announcements WHERE id = ? AND delivered_at IS NULL", (ann_id,))
        conn.commit()
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# Model profiles (switchable by voice) and MCP extensions
# ---------------------------------------------------------------------------


def _kind(value: str) -> str:
    return "asr" if value == "asr" else "llm"


@app.post("/profiles")
async def profiles_save(request: Request, kind: str = Query("llm")):
    redirect = _require_login(request)
    if redirect:
        return redirect
    kind = _kind(kind)
    form = await request.form()
    anchor = "#sec-asr" if kind == "asr" else "#sec-llm"
    try:
        services.save_current_as_profile(str(form.get(f"profile_name_{kind}", "")), kind)
    except ServiceError as exc:
        return RedirectResponse(f"/config?perr={quote(str(exc))}{anchor}", status_code=303)
    return RedirectResponse(f"/config?msg=profile_saved{anchor}", status_code=303)


@app.post("/profiles/{profile_id}/apply")
async def profiles_apply(request: Request, profile_id: int, kind: str = Query("llm")):
    redirect = _require_login(request)
    if redirect:
        return redirect
    kind = _kind(kind)
    try:
        services.apply_profile(profile_id, kind)
    except ServiceError:
        pass
    return RedirectResponse(f"/config?msg=profile_applied{'#sec-asr' if kind == 'asr' else '#sec-llm'}", status_code=303)


@app.post("/profiles/{profile_id}/delete")
async def profiles_delete(request: Request, profile_id: int, kind: str = Query("llm")):
    redirect = _require_login(request)
    if redirect:
        return redirect
    kind = _kind(kind)
    services.delete_profile(profile_id, kind)
    return RedirectResponse(f"/config?msg=profile_deleted{'#sec-asr' if kind == 'asr' else '#sec-llm'}", status_code=303)


@app.get("/extensions", response_class=HTMLResponse)
async def extensions_get(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    return await _render_extensions(request)


async def _render_extensions(request: Request, error: str = "", form: dict | None = None,
                             status_code: int = 200):
    servers = services.list_mcp_servers()
    for srv in servers:
        srv["transport"] = services.mcp_transport(srv["url"])
        srv["private"] = services.is_private_address(srv["url"])
        srv["token_tail"] = key_tail(srv["token"])
    console_tools = [{"name": t.name, "description": t.description or ""} for t in await mcp.list_tools()]
    return templates.TemplateResponse(
        request, "extensions.html",
        {"servers": servers, "console_tools": console_tools, "error": error or request.query_params.get("err", ""),
         "market_error": request.query_params.get("merr", ""),
         "form": form or {}, "market": __import__("app.mcp_market", fromlist=["MARKET"]).MARKET,
         "connected": {s["name"] for s in servers}},
        status_code=status_code,
    )


@app.post("/extensions")
async def extensions_save(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    token: str = Form(""),
    tools: str = Form(""),
    timeout_s: str = Form("10"),
    note: str = Form(""),
    server_id: str = Form(""),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    try:
        services.save_mcp_server(name, url, token, tools, timeout_s, note,
                                 int(server_id) if server_id.isdigit() else None)
    except ServiceError as exc:
        form = {"name": name, "url": url, "tools": tools, "timeout_s": timeout_s, "note": note,
                "server_id": server_id}
        return await _render_extensions(request, str(exc), form, 400)
    return RedirectResponse("/extensions?msg=mcp_saved", status_code=303)


@app.post("/extensions/market/{entry_id}")
async def extensions_market(request: Request, entry_id: str, key: str = Form(""), server: str = Form("")):
    """One-click connect a market entry: build the address, check it, save it."""
    redirect = _require_login(request)
    if redirect:
        return redirect
    from urllib.parse import quote as _q
    from . import mcp_market
    from .mcp_probe import list_remote_tools
    entry = mcp_market.find(entry_id)
    if not entry or not key.strip() or (entry.get("server_label") and not server.strip()):
        return RedirectResponse(f"/extensions?merr={_q('请把 Key 等信息填完整。')}#market", status_code=303)
    if entry_id == "bailian" and server.strip().isdigit():
        return RedirectResponse(f"/extensions?merr={_q(f'「{server.strip()}」是 API Key 的编号，不是 MCP 服务 ID。服务 ID 在 MCP 广场里某个服务详情页的「接入方式」，地址 /mcps/ 后面那段（例如 WebSearch）；整条地址粘过来也行。')}#market", status_code=303)
    name, url, token = mcp_market.build(entry, key, server)
    try:
        tools = await list_remote_tools(url, token)
    except Exception as exc:  # the probe explains HTTP / auth failures in Chinese
        why = str(exc)[:200]
        if entry_id == "bailian" and "404" in why:
            why = (f"百炼上找不到服务「{name.removeprefix('bailian-')}」。请确认它已在 MCP 广场开通，"
                   "再从服务详情的「接入方式」复制地址（/mcps/ 后面那段就是服务 ID）。")
        elif entry_id == "bailian" and "401" in why:
            why = "百炼 API Key 不对或没权限，请到百炼控制台「API Key」页复制 sk- 开头的 Key。"
        return RedirectResponse(f"/extensions?merr={_q('接不上：' + why)}#market", status_code=303)
    try:
        services.save_mcp_server(name, url, token, "", "20", note=f"工具市场：{entry['name']}，{len(tools)} 个工具")
    except ServiceError as exc:
        return RedirectResponse(f"/extensions?merr={_q(str(exc))}#market", status_code=303)
    return RedirectResponse(f"/extensions?msg=mcp_saved#market", status_code=303)


@app.post("/extensions/{server_id}/toggle")
async def extensions_toggle(request: Request, server_id: int):
    redirect = _require_login(request)
    if redirect:
        return redirect
    current = next((s for s in services.list_mcp_servers() if s["id"] == server_id), None)
    if current:
        services.set_mcp_enabled(server_id, not current["enabled"])
    return RedirectResponse("/extensions?msg=mcp_saved", status_code=303)


@app.post("/extensions/{server_id}/delete")
async def extensions_delete(request: Request, server_id: int):
    redirect = _require_login(request)
    if redirect:
        return redirect
    services.delete_mcp_server(server_id)
    return RedirectResponse("/extensions?msg=mcp_deleted", status_code=303)


class McpProbe(BaseModel):
    url: str
    token: str = ""
    server_id: int | None = None


@app.post("/api/mcp/probe")
async def api_mcp_probe(request: Request, probe: McpProbe):
    if not request.session.get("logged_in"):
        return JSONResponse({"ok": False, "message": "登录已过期，请刷新页面重新登录。"}, status_code=401)
    url = probe.url.strip()
    if services.is_private_address(url):
        return JSONResponse({"ok": False, "private": True, "message":
            "这是家里局域网的地址，云上的控制台访问不到，所以没法在这里测试。音箱在家里，"
            "只要它能访问这个地址就可以用：保存后看概览页的运行日志里是否出现「connected MCP tools」。"})
    token = probe.token.strip()
    if not token and probe.server_id:
        saved = next((s for s in services.list_mcp_servers() if s["id"] == probe.server_id), None)
        if saved and saved["url"].rstrip("/") == url.rstrip("/"):
            token = saved["token"]  # same address only: never send a saved token elsewhere
    try:
        tools = await list_remote_tools(url, token)
    except ServiceError as exc:
        return JSONResponse({"ok": False, "message": str(exc)})
    transport = "SSE" if services.mcp_transport(url) == "sse" else "Streamable HTTP"
    return JSONResponse({"ok": True, "tools": tools,
                         "message": f"连接正常（{transport}），这个服务提供 {len(tools)} 个工具。"})


# ---------------------------------------------------------------------------
# Apps (installed on the server through the host-side installer)
# ---------------------------------------------------------------------------


@app.get("/apps", response_class=HTMLResponse)
async def apps_get(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    ctx = {"apps": [], "status": None, "unavailable": "", "job_id": request.query_params.get("job", "")}
    try:
        ctx["status"] = await installer_client.status()
        ctx["apps"] = await installer_client.list_apps()
    except installer_client.InstallerUnavailable as exc:
        ctx["unavailable"] = str(exc)
    except installer_client.InstallerError as exc:
        ctx["unavailable"] = f"安装器出错：{exc}"
    return templates.TemplateResponse(request, "apps.html", ctx)


async def _app_or_redirect(app_id: str):
    try:
        return await apps_service.find_app(app_id), None
    except ServiceError as exc:
        return None, RedirectResponse(f"/apps?err={quote(str(exc))}", status_code=303)


UPLOAD_DIR = Path(os.environ.get("CONSOLE_DATA_DIR", BASE_DIR.parent / "data")) / "uploads"


@app.post("/apps/images/upload")
async def apps_image_upload(request: Request):
    """Receive a `docker save` archive the owner downloaded themselves and hand
    it to the installer to `docker load` (for images the server can't pull)."""
    redirect = _require_login(request)
    if redirect:
        return redirect
    form = await request.form()
    upload = form.get("file")
    if not upload or not getattr(upload, "filename", ""):
        return RedirectResponse(f"/apps?err={quote('请选择要上传的镜像文件。')}", status_code=303)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(upload.filename).name)[:180] or "image.tar"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / name
    with dest.open("wb") as out:
        while chunk := await upload.read(1024 * 1024):
            out.write(chunk)
    try:
        job_id = await installer_client.load_image(name)
    except (installer_client.InstallerUnavailable, installer_client.InstallerError) as exc:
        dest.unlink(missing_ok=True)
        return RedirectResponse(f"/apps?err={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/apps?job={job_id}", status_code=303)


@app.post("/apps/{app_id}/install")
async def apps_install(request: Request, app_id: str):
    redirect = _require_login(request)
    if redirect:
        return redirect
    target, bad = await _app_or_redirect(app_id)
    if bad:
        return bad
    form = await request.form()
    params = {k[6:]: str(v) for k, v in form.items() if k.startswith("param_")}
    try:
        job_id = await apps_service.start_install(target, params, dry_run=form.get("dry_run") == "1")
    except ServiceError as exc:
        return RedirectResponse(f"/apps?err={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/apps?job={job_id}", status_code=303)


@app.post("/apps/{app_id}/uninstall")
async def apps_uninstall(request: Request, app_id: str):
    redirect = _require_login(request)
    if redirect:
        return redirect
    target, bad = await _app_or_redirect(app_id)
    if bad:
        return bad
    form = await request.form()
    try:
        job_id = await apps_service.start_uninstall(target, delete_data=form.get("delete_data") == "1")
    except ServiceError as exc:
        return RedirectResponse(f"/apps?err={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/apps?job={job_id}", status_code=303)


@app.post("/apps/{app_id}/{action}")
async def apps_control(request: Request, app_id: str, action: str):
    redirect = _require_login(request)
    if redirect:
        return redirect
    if action in ("start", "stop", "certify"):
        try:
            await installer_client.control(app_id, action)
        except (installer_client.InstallerUnavailable, installer_client.InstallerError) as exc:
            return RedirectResponse(f"/apps?err={quote(str(exc))}", status_code=303)
    return RedirectResponse("/apps", status_code=303)


@app.get("/apps/{app_id}/logs", response_class=PlainTextResponse)
async def apps_logs(request: Request, app_id: str):
    if not request.session.get("logged_in"):
        return PlainTextResponse("请先登录。", status_code=401)
    try:
        return PlainTextResponse(await installer_client.logs(app_id) or "（暂无日志）")
    except (installer_client.InstallerUnavailable, installer_client.InstallerError) as exc:
        return PlainTextResponse(str(exc), status_code=400)


@app.get("/api/jobs/{job_id}")
async def api_job(request: Request, job_id: str):
    if not request.session.get("logged_in"):
        return JSONResponse({"error": "登录已过期"}, status_code=401)
    try:
        return JSONResponse(await installer_client.job(job_id))
    except (installer_client.InstallerUnavailable, installer_client.InstallerError) as exc:
        return JSONResponse({"status": "failed", "error": str(exc), "log": []})


# ---------------------------------------------------------------------------
# Device-facing endpoints: plain text only, authenticated by a shared token.
# The speaker has no JSON/base64 tooling (BusyBox + curl only), so every
# response here is either a raw file body or one plain line.
# ---------------------------------------------------------------------------


def _check_device_token(token: str, header_token: str | None = None) -> bool:
    """The poller sends the token in the X-Device-Token header so it stays out
    of URL access logs; the query parameter is still accepted for older
    poller copies."""
    supplied = header_token or token or ""
    expected = get_settings().get("device_token", "")
    return bool(expected) and hmac.compare_digest(
        hashlib.sha256(supplied.encode()).digest(), hashlib.sha256(expected.encode()).digest()
    )


@app.get("/api/device/config.yaml", response_class=PlainTextResponse)
async def device_config(request: Request, token: str = Query(""), x_device_token: str | None = Header(None)):
    if not _check_device_token(token, x_device_token):
        return PlainTextResponse("forbidden", status_code=403)
    return PlainTextResponse(render_agent_yaml(get_settings(), _public_base_url(request)))


def _public_base_url(request: Request) -> str:
    """The console's address as the speaker reaches it (nginx terminates TLS)."""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


@app.post("/api/device/search")
async def device_search(request: Request, authorization: str | None = Header(None),
                        x_device_token: str | None = Header(None)):
    """Tavily-compatible search for the speaker's built-in web_search tool.

    The speaker's agent only speaks Tavily's request/response format. It is
    pointed here (see config_render) and authenticates with its device token
    as the Bearer key; the console then searches with whichever provider is
    configured, so Firecrawl works for live questions too and the real search
    key never has to live on the speaker.
    """
    bearer = (authorization or "").removeprefix("Bearer ").strip()
    if not _check_device_token("", x_device_token or bearer):
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "invalid JSON"}, status_code=400)
    query = str(body.get("query", "")).strip()
    if not query:
        return JSONResponse({"detail": "query is empty"}, status_code=400)
    # The agent asks for 3 on simple questions and up to 10 for roundups.
    max_results = max(1, min(int(body.get("max_results") or 3), 10))
    topic = "news" if body.get("topic") == "news" else "general"
    time_range = str(body.get("time_range") or "")
    try:
        results = await web_search(get_settings(), query, max_results, topic, time_range, live=True)
    except ProviderError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=502)
    return JSONResponse({
        "query": query,
        "answer": "",
        "results": [
            {"title": r["title"], "url": r["url"], "content": r["summary"],
             "published_date": r["date"] or None, "score": None}
            for r in results
        ],
    })


@app.get("/api/device/announcement", response_class=PlainTextResponse)
async def device_announcement(token: str = Query(""), x_device_token: str | None = Header(None)):
    """Returns at most one due announcement as "<id>\\t<text>", or empty."""
    if not _check_device_token(token, x_device_token):
        return PlainTextResponse("forbidden", status_code=403)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, text FROM announcements WHERE delivered_at IS NULL "
            "ORDER BY id ASC LIMIT 1"
        ).fetchone()
    if not row:
        return PlainTextResponse("")
    text = row["text"].replace("\t", " ").replace("\n", " ")
    return PlainTextResponse(f"{row['id']}\t{text}")


@app.post("/api/device/announcement/{ann_id}/ack", response_class=PlainTextResponse)
async def device_announcement_ack(ann_id: int, token: str = Query(""), x_device_token: str | None = Header(None)):
    if not _check_device_token(token, x_device_token):
        return PlainTextResponse("forbidden", status_code=403)
    with get_conn() as conn:
        conn.execute(
            "UPDATE announcements SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL",
            (now_local().isoformat(), ann_id),
        )
        conn.commit()
    return PlainTextResponse("ok")


@app.post("/api/device/tts", response_class=PlainTextResponse)
async def device_tts(request: Request, text: str = Form(""), x_device_token: str | None = Header(None)):
    """say.sh asks for a ticket; 'native' tells it to use XiaoAI's own voice."""
    if not _check_device_token("", x_device_token):
        return PlainTextResponse("forbidden", status_code=403)
    settings = get_settings()
    voice = settings.get("tts_voice", tts.NATIVE)
    text = text.strip()
    if voice == tts.NATIVE or voice not in tts.LABELS or not text:
        return "native"
    if tts.voice_problem(voice, settings):
        log.warning("音色 %s 现在用不了，改用小爱原声：%s", voice, tts.voice_problem(voice, settings))
        return "native"  # never leave the speaker silent
    return tts.ticket(text[:8000], voice)


@app.get("/api/device/tts/{ticket}.mp3")
async def device_tts_audio(ticket: str):
    """Streams the reply as MP3. The ticket is a single-purpose 144-bit random
    id that expires after two minutes, so miplayer needs no headers."""
    item = tts.redeem(ticket)
    if not item:
        return PlainTextResponse("expired", status_code=404)
    text, voice = item
    settings = get_settings()
    gen = tts.stream(text, voice, settings)
    try:
        first = await gen.__anext__()  # fail before headers so say.sh can fall back
    except (tts.TtsError, StopAsyncIteration) as exc:
        log.warning("语音合成失败：%s", exc)
        return PlainTextResponse("tts failed", status_code=502)

    async def body():
        yield first
        try:
            async for chunk in gen:
                yield chunk
        except tts.TtsError as exc:
            log.warning("语音合成中断：%s", exc)

    return StreamingResponse(body(), media_type=tts.media_type(voice), headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})


@app.get("/guide", response_class=HTMLResponse)
async def guide_page(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    from . import guide
    return templates.TemplateResponse(request, "guide.html", {"scenes": guide.SCENES})


@app.get("/radio", response_class=HTMLResponse)
async def radio_page(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    moods = [words[0] for words, _ in music.MOODS]
    return templates.TemplateResponse(request, "radio.html", {
        "moods": moods, "online": commands.speaker_online()})


@app.get("/api/radio/browse")
async def radio_browse(request: Request, cat: str = "recommend", q: str = ""):
    if not request.session.get("logged_in"):
        return JSONResponse({"ok": False}, status_code=401)
    src = radio.radio_source()
    favs = {f["station_id"] for f in radio.favourites()}
    try:
        if cat == "favorites":
            stations = [music.song(f["station_id"]) | {"name": f["name"], "album": f["tags"]} for f in radio.favourites()]
        elif cat == "recent":
            stations = [music.song(h["station_id"]) | {"name": h["name"], "album": h["tags"]} for h in radio.history()]
        elif cat == "recommend":
            stations = radio.recommended(20)
        elif cat == "china":
            stations = [music._song(src, f"c{i}", n, ["精选电台"], t.split()[0], None, u) for i, (n, u, t) in enumerate(music.CURATED)]
        else:
            stations = await radio.search(q.strip() or cat, 20)
    except music.MusicError as exc:
        return JSONResponse({"ok": False, "message": str(exc)})
    return JSONResponse({"ok": True, "stations": [
        {"id": s["id"], "name": s["name"], "tags": s.get("album") or "", "fav": s["id"] in favs,
         **radio.station_view(s)} for s in stations]})


@app.post("/api/radio/play")
async def radio_play(request: Request):
    if not request.session.get("logged_in"):
        return JSONResponse({"ok": False}, status_code=401)
    sid = str((await request.json()).get("id", ""))
    if not sid:
        return JSONResponse({"ok": False, "message": "没有选电台。"})
    commands.push({"type": "play", "id": sid})
    msg = "已发给音箱。" if commands.speaker_online() else "音箱好像不在线，指令会在它连上后执行。"
    return JSONResponse({"ok": True, "message": msg})


@app.post("/api/radio/stop")
async def radio_stop(request: Request):
    if not request.session.get("logged_in"):
        return JSONResponse({"ok": False}, status_code=401)
    commands.push({"type": "stop"})
    return JSONResponse({"ok": True})


@app.post("/api/radio/fav")
async def radio_fav(request: Request):
    if not request.session.get("logged_in"):
        return JSONResponse({"ok": False}, status_code=401)
    body = await request.json()
    radio.favourite(str(body.get("id", "")), bool(body.get("on", True)))
    return JSONResponse({"ok": True})


@app.get("/api/radio/relay/{sig}")
async def radio_relay(sig: str, u: str = Query(...)):
    """Buffered pass-through of a far-away station (URL signed by the console)."""
    if not radio.check_relay(sig, u):
        return PlainTextResponse("forbidden", status_code=403)
    return StreamingResponse(radio.relay(u), media_type="audio/mpeg",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})


@app.get("/api/device/music/{action}")
async def device_music(request: Request, action: str, q: str = "", limit: int = 5, id: str = "",
                       x_device_token: str | None = Header(None)):
    if not _check_device_token("", x_device_token):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        if action == "search":
            return JSONResponse(await music.search(q, limit))
        if action == "random":
            return JSONResponse(await music.random_songs(limit))
        if action == "radio":
            return JSONResponse(await radio.search(q, limit))
        if action == "song":
            return JSONResponse(music.song(id))
        if action == "more":
            return JSONResponse(await music.more_like(id, limit))
        if action == "url":
            url = await music.play_url(id)
            src = radio.radio_source()
            if src and id.startswith(f"{src['id']}:"):
                radio.record_play(id)
                host = request.headers.get("host") or request.url.netloc
                url = radio.relay_url(id, url, f"https://{host}")
            return JSONResponse({"url": url, "note": music.take_note(id)})
    except music.MusicError as exc:
        return JSONResponse({"error": str(exc)})
    return JSONResponse({"error": "unknown action"}, status_code=404)


@app.get("/music", response_class=HTMLResponse)
async def music_get(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    netease_app = kugou_app = ""
    try:
        for a in await installer_client.list_apps():
            url = ((a.get("state") or {}).get("url") or "").rstrip("/")
            if a.get("id") == "netease-api" and url:
                netease_app = url
            if a.get("id") == "kugou-api" and url:
                kugou_app = url
    except Exception:
        pass
    return templates.TemplateResponse(request, "music.html", {
        "sources": music.song_sources(), "kinds": {k: v for k, v in music.KINDS.items() if k != "radio"}, "netease_app": netease_app, "kugou_app": kugou_app,
        "example": json.dumps(music.CUSTOM_EXAMPLE, ensure_ascii=False, indent=2),
        "presets": {k: json.dumps(v, ensure_ascii=False, indent=2) for k, v in music.CUSTOM_PRESETS.items()},
        "error": request.query_params.get("err", "")})


@app.post("/music/sources")
async def music_add(request: Request, kind: str = Form(...), name: str = Form(""), base_url: str = Form(""),
                    username: str = Form(""), password: str = Form(""), custom_config: str = Form(""),
                    position: int = Form(50)):
    redirect = _require_login(request)
    if redirect:
        return redirect
    from urllib.parse import quote as _q
    try:
        if kind == "library":
            cfg = {"base_url": base_url, "username": username, "password": password}
        elif kind in ("netease", "kugou"):
            cfg = {"base_url": base_url, "cookie": ""}
        elif kind == "qqmusic":
            cfg = {}
        elif kind == "custom":
            try:
                cfg = json.loads(custom_config)
                assert isinstance(cfg, dict)
            except (ValueError, AssertionError):
                raise music.MusicError("自定义音源的配置不是有效的 JSON。")
        else:
            cfg = {"country": "CN"}
        music.save_source(name, kind, cfg, position)
    except music.MusicError as exc:
        return RedirectResponse(f"/music?err={_q(str(exc))}", status_code=303)
    return RedirectResponse("/music", status_code=303)


@app.post("/api/music/{source_id}/qr")
async def music_qr(request: Request, source_id: int, type: str = Query("qq")):
    if not request.session.get("logged_in"):
        return JSONResponse({"ok": False, "message": "请先登录。"}, status_code=401)
    try:
        return JSONResponse({"ok": True, **await music.qr_start(source_id, type)})
    except music.MusicError as exc:
        return JSONResponse({"ok": False, "message": str(exc)})


@app.get("/api/music/{source_id}/qr/{key}")
async def music_qr_poll(request: Request, source_id: int, key: str):
    if not request.session.get("logged_in"):
        return JSONResponse({"ok": False}, status_code=401)
    try:
        return JSONResponse({"ok": True, "state": await music.qr_check(source_id, key)})
    except music.MusicError as exc:
        return JSONResponse({"ok": False, "message": str(exc)})


@app.post("/api/music/order")
async def music_order(request: Request):
    if not request.session.get("logged_in"):
        return JSONResponse({"ok": False}, status_code=401)
    ids = [int(i) for i in (await request.json()).get("ids", []) if str(i).isdigit()]
    music.reorder(ids)
    return JSONResponse({"ok": True})


@app.post("/music/sources/{source_id}/{action}")
async def music_source_action(request: Request, source_id: int, action: str):
    redirect = _require_login(request)
    if redirect:
        return redirect
    if action == "delete":
        music.delete_source(source_id)
    elif action == "first":
        src = next((s for s in music.list_sources() if s["id"] == source_id), None)
        if src:
            music.use_source(src["name"])
    elif action in ("enable", "disable"):
        music.set_enabled(source_id, action == "enable")
    return RedirectResponse("/music", status_code=303)


@app.get("/api/music/try")
async def music_try(request: Request, q: str = ""):
    if not request.session.get("logged_in"):
        return JSONResponse({"ok": False, "message": "请先登录。"}, status_code=401)
    try:
        songs = await music.search(q, 5)
        if not songs:
            return JSONResponse({"ok": False, "message": "所有音源都没搜到。"})
        first_url = await music.play_url(songs[0]["id"])
    except music.MusicError as exc:
        return JSONResponse({"ok": False, "message": str(exc)})
    return JSONResponse({"ok": True, "songs": songs, "first_url": first_url})


@app.post("/api/device/tts/stream", response_class=PlainTextResponse)
async def device_tts_stream_open(x_device_token: str | None = Header(None)):
    """Open a streamed reply. Answers "native" (speak each sentence with the
    native voice), "segments" (speak each sentence with say.sh) or a ticket."""
    if not _check_device_token("", x_device_token):
        return PlainTextResponse("forbidden", status_code=403)
    settings = get_settings()
    voice = settings.get("tts_voice", tts.NATIVE)
    if voice == tts.NATIVE or voice not in tts.LABELS or tts.voice_problem(voice, settings):
        return "native"
    if not tts.can_stream(voice):
        return "segments"
    return tts.open_stream(voice)


@app.post("/api/device/tts/stream/{sid}", response_class=PlainTextResponse)
async def device_tts_stream_push(sid: str, request: Request, x_device_token: str | None = Header(None)):
    """Body is the next sentence (UTF-8 text); an empty body ends the reply."""
    if not _check_device_token("", x_device_token):
        return PlainTextResponse("forbidden", status_code=403)
    text = (await request.body()).decode("utf-8", errors="replace")
    return "ok" if tts.push_text(sid, text if text.strip() else None) else PlainTextResponse("gone", status_code=404)


@app.get("/api/device/tts/stream/{sid}.mp3")
async def device_tts_stream_audio(sid: str):
    """The streamed reply's audio; the 144-bit ticket is the credential."""
    if sid not in tts._streams:
        return PlainTextResponse("expired", status_code=404)
    return StreamingResponse(tts.stream_session(sid, get_settings()), media_type="audio/mpeg",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})


@app.get("/api/device/command")
async def device_command(x_device_token: str | None = Header(None)):
    """Long poll: answers with the next command, or {} after ~25 s."""
    if not _check_device_token("", x_device_token):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse(await commands.next_command() or {})


@app.get("/api/device/memory", response_class=PlainTextResponse)
async def device_memory(x_device_token: str | None = Header(None)):
    """What the assistant remembers about the user, one per line."""
    if not _check_device_token("", x_device_token):
        return PlainTextResponse("forbidden", status_code=403)
    return memory.speaker_text()


@app.post("/api/device/memory/exchange", response_class=PlainTextResponse)
async def device_memory_exchange(request: Request, x_device_token: str | None = Header(None)):
    """One finished exchange; learned from once the conversation goes quiet."""
    if not _check_device_token("", x_device_token):
        return PlainTextResponse("forbidden", status_code=403)
    try:
        data = await request.json()
        memory.record_exchange(str(data.get("user", "")), str(data.get("assistant", "")))
        ideas.log_exchange(str(data.get("user", "")), str(data.get("assistant", "")))
    except ValueError:
        return PlainTextResponse("bad json", status_code=400)
    return "ok"


_now_playing: dict = {"current": None, "paused": False, "at": 0.0}


@app.post("/api/device/nowplaying", response_class=PlainTextResponse)
async def device_now_playing(request: Request, x_device_token: str | None = Header(None)):
    if not _check_device_token("", x_device_token):
        return PlainTextResponse("forbidden", status_code=403)
    try:
        data = await request.json()
    except ValueError:
        return PlainTextResponse("bad json", status_code=400)
    _now_playing.update({"current": data.get("current"), "paused": bool(data.get("paused")),
                         "has_previous": bool(data.get("has_previous")), "volume": data.get("volume"),
                         "at": time.time()})
    return "ok"


def _now_playing_view() -> dict:
    stale = time.time() - _now_playing["at"] > 90  # speaker stopped reporting
    return {"current": None if stale else _now_playing["current"], "paused": _now_playing["paused"],
            "volume": _now_playing.get("volume"), "online": commands.speaker_online()}


@app.get("/api/nowplaying")
async def now_playing(request: Request):
    if not request.session.get("logged_in"):
        return JSONResponse({"current": None}, status_code=401)
    return JSONResponse(_now_playing_view())


@app.post("/api/nowplaying/{action}")
async def now_playing_control(request: Request, action: str):
    if not request.session.get("logged_in"):
        return JSONResponse({"current": None}, status_code=401)
    if action in ("volume_up", "volume_down"):
        step = 10 if action == "volume_up" else -10
        commands.push({"type": "volume", "delta": step})
        if isinstance(_now_playing.get("volume"), int):
            _now_playing["volume"] = max(5, min(100, _now_playing["volume"] + step))
        return JSONResponse(_now_playing_view())
    if action not in ("pause", "resume", "stop", "next", "previous"):
        return JSONResponse({"error": "unknown"}, status_code=404)
    commands.push({"type": action})
    # Show the expected state right away; the speaker's report confirms it.
    if action in ("pause", "resume"):
        _now_playing["paused"] = action == "pause"
    if action == "stop":
        _now_playing["current"] = None
    return JSONResponse(_now_playing_view())


@app.get("/shortcuts", response_class=HTMLResponse)
async def shortcuts_get(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "shortcuts.html", {
        "items": shortcuts.load(), "built_in": shortcuts.BUILT_IN, "examples": shortcuts.EXAMPLES,
        "msg": request.query_params.get("msg", ""), "error": request.query_params.get("err", "")})


@app.post("/shortcuts")
async def shortcuts_post(request: Request, action: str = Form("save"), say: str = Form(""), run: str = Form(""),
                         index: str = Form("")):
    redirect = _require_login(request)
    if redirect:
        return redirect
    from urllib.parse import quote as _q
    idx = int(index) if index.isdigit() else None
    if action == "delete" and idx is not None:
        shortcuts.delete(idx)
        return RedirectResponse(f"/shortcuts?msg={_q('已删除。')}", status_code=303)
    try:
        msg = shortcuts.upsert(say, run, index=idx)
    except ValueError as exc:
        return RedirectResponse(f"/shortcuts?err={_q(str(exc))}", status_code=303)
    return RedirectResponse(f"/shortcuts?msg={_q(msg)}", status_code=303)


@app.get("/ideas", response_class=HTMLResponse)
async def ideas_get(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "ideas.html", {
        "ideas": ideas.list_ideas(), "msg": request.query_params.get("msg", "")})


@app.post("/ideas")
async def ideas_post(request: Request, action: str = Form(...), id: str = Form("")):
    redirect = _require_login(request)
    if redirect:
        return redirect
    from urllib.parse import quote as _q
    if action == "think":
        try:
            n = await ideas.think()
            msg = f"想了 {n} 个新点子。" if n else "这次没想出新点子，稍后再试。"
        except Exception as exc:
            msg = f"想点子失败：{str(exc)[:120]}"
    elif action in ("adopt", "drop") and id.isdigit():
        msg = ideas.decide(int(id), action == "adopt")
    else:
        msg = ""
    return RedirectResponse(f"/ideas?msg={_q(msg)}", status_code=303)


@app.get("/memory", response_class=HTMLResponse)
async def memory_get(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    with get_conn() as conn:
        pending = conn.execute("SELECT COUNT(*) FROM memory_inbox").fetchone()[0]
    return templates.TemplateResponse(request, "memory.html", {
        "memories": memory.list_memories(), "categories": memory.CATEGORIES,
        "enabled": get_settings().get("memory_enabled", "1") == "1", "pending": pending,
        "msg": request.query_params.get("msg", "")})


@app.post("/memory")
async def memory_post(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    form = await request.form()
    action = str(form.get("action", ""))
    msg = ""
    try:
        if action == "add":
            memory.add_memory(str(form.get("content", "")), str(form.get("category", "其他")), "手动")
        elif action == "save":
            memory.update_memory(int(form.get("id", 0)), str(form.get("content", "")), str(form.get("category", "")))
        elif action == "delete":
            memory.delete_memory(int(form.get("id", 0)))
        elif action == "toggle":
            set_settings({"memory_enabled": "0" if get_settings().get("memory_enabled", "1") == "1" else "1"})
        elif action == "learn":
            msg = await memory.learn_pending(force=True)
    except ValueError as exc:
        msg = str(exc)
    from urllib.parse import quote as _q
    return RedirectResponse(f"/memory?msg={_q(msg)}" if msg else "/memory", status_code=303)


@app.get("/api/device/voice", response_class=PlainTextResponse)
async def device_voice(x_device_token: str | None = Header(None)):
    """One line the poller compares to know when to refresh the wake clips."""
    if not _check_device_token("", x_device_token):
        return PlainTextResponse("forbidden", status_code=403)
    return wake.signature(get_settings())


@app.get("/api/device/wake/{name}")
async def device_wake_clip(name: str, x_device_token: str | None = Header(None)):
    if not _check_device_token("", x_device_token):
        return PlainTextResponse("forbidden", status_code=403)
    try:
        data = await wake.clip(name, get_settings())
    except Exception as exc:  # synthesis/decoding failed: keep the factory clip
        log.warning("唤醒提示音合成失败：%s", exc)
        return PlainTextResponse("failed", status_code=502)
    if data is None:
        return PlainTextResponse("native", status_code=404)
    return Response(data, media_type="audio/wav")


@app.post("/api/device/status", response_class=PlainTextResponse)
async def device_status(request: Request, token: str = Query(""), x_device_token: str | None = Header(None)):
    if not _check_device_token(token, x_device_token):
        return PlainTextResponse("forbidden", status_code=403)
    body = (await request.body()).decode("utf-8", errors="replace")[-8000:]
    with get_conn() as conn:
        conn.execute(
            "UPDATE device_status SET last_seen_at = ?, last_report = ? WHERE id = 1",
            (now_local().isoformat(), body),
        )
        conn.commit()
    try:
        speed.ingest(re.sub(r"\x1b\[[0-9;]*m", "", body))
    except Exception as exc:  # never fail the speaker's report over statistics
        log.warning("speed ingest failed: %s", exc)
    return PlainTextResponse("ok")


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    return PlainTextResponse("ok")
