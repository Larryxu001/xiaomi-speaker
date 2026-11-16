"""Install/uninstall flow shared by the 应用 page and the voice tools.

The installer does the heavy lifting on the host; this module starts jobs,
follows them in the background, and when an install finishes wires the app
into the speaker (registers its MCP endpoint) and announces the result.
"""

from __future__ import annotations

import asyncio
import logging

from . import installer_client as inst
from .db import get_conn
from .services import ServiceError, delete_mcp_server, list_mcp_servers, save_mcp_server

log = logging.getLogger("console.apps")
_followers: set[asyncio.Task] = set()


def _announce(text: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT INTO announcements (task_id, text) VALUES (NULL, ?)", (text,))
        conn.commit()


async def find_app(query: str) -> dict:
    try:
        apps = await inst.list_apps()
    except inst.InstallerUnavailable as exc:
        raise ServiceError(f"{exc}暂时不能安装软件。")
    if not apps:
        raise ServiceError("应用商店里还没有任何应用。")
    q = (query or "").strip().lower()
    for a in apps:
        if q in (a["id"], str(a.get("name", "")).lower()):
            return a
    hits = [a for a in apps if q and (q in a["id"] or q in str(a.get("name", "")).lower()
                                       or q in str(a.get("description", "")).lower())]
    if len(hits) == 1:
        return hits[0]
    names = "、".join(a.get("name", a["id"]) for a in (hits or apps))
    raise ServiceError(f"没有找到「{query}」。商店里有：{names}。" if not hits
                       else f"「{query}」能匹配好几个应用：{names}。")


async def _follow(kind: str, app: dict, job_id: str, announce: bool) -> None:
    name = app.get("name", app["id"])
    for _ in range(600):  # up to ~20 minutes
        await asyncio.sleep(2)
        try:
            job = await inst.job(job_id)
        except Exception:  # noqa: BLE001 - installer restarted; keep trying briefly
            continue
        if job["status"] == "running":
            continue
        if job["status"] == "done" and kind == "install" and not (job.get("result") or {}).get("dry_run"):
            try:
                sec = await inst.secret(app["id"])
                if sec.get("mcp_url"):
                    save_mcp_server(app["id"], sec["mcp_url"], sec.get("mcp_token") or "", "", "15",
                                    note=f"应用：{name}")
            except Exception as exc:  # noqa: BLE001
                log.warning("could not register MCP for %s: %s", app["id"], exc)
            if app["id"] == "kugou-api":
                try:
                    from . import music
                    state = next((a.get("state") or {} for a in await inst.list_apps() if a.get("id") == "kugou-api"), {})
                    if state.get("url") and not any(s["kind"] == "kugou" for s in music.list_sources()):
                        music.save_source("酷狗音乐", "kugou", {"base_url": state["url"].rstrip("/"), "cookie": ""}, 12)
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not add KuGou music source: %s", exc)
            if app["id"] == "netease-api":
                try:
                    from . import music
                    sec = await inst.secret(app["id"])
                    state = next((a.get("state") or {} for a in await inst.list_apps() if a.get("id") == "netease-api"), {})
                    if state.get("url") and not any(s["kind"] == "netease" for s in music.list_sources()):
                        music.save_source("网易云音乐", "netease", {"base_url": state["url"].rstrip("/"), "cookie": ""}, 10)
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not add NetEase music source: %s", exc)
            if announce:
                _announce(f"{name}已经装好了。")
        elif job["status"] == "done" and kind == "uninstall":
            for srv in list_mcp_servers():
                if srv["name"] == app["id"]:
                    delete_mcp_server(srv["id"])
            if announce:
                _announce(f"{name}已经卸载了。")
        elif job["status"] == "failed" and announce:
            _announce(f"{name}{'安装' if kind == 'install' else '卸载'}失败：{job.get('error') or '原因不明'}")
        return


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _followers.add(task)
    task.add_done_callback(_followers.discard)


async def start_install(app: dict, params: dict, dry_run: bool = False, announce: bool = True) -> str:
    if (app.get("state") or {}).get("installed"):
        raise ServiceError(f"{app.get('name', app['id'])}已经装过了。")
    try:
        job_id = await inst.install(app["id"], params, dry_run)
    except (inst.InstallerUnavailable, inst.InstallerError) as exc:
        raise ServiceError(str(exc))
    _spawn(_follow("install", app, job_id, announce and not dry_run))
    return job_id


async def start_uninstall(app: dict, delete_data: bool = False, announce: bool = True) -> str:
    if not (app.get("state") or {}).get("installed"):
        raise ServiceError(f"{app.get('name', app['id'])}没有安装。")
    try:
        job_id = await inst.uninstall(app["id"], delete_data)
    except (inst.InstallerUnavailable, inst.InstallerError) as exc:
        raise ServiceError(str(exc))
    _spawn(_follow("uninstall", app, job_id, announce))
    return job_id
