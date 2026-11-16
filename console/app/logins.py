"""Keeps music logins alive: refreshes NetEase / KuGou / QQ Music logins
before they lapse, and if one really expires, says so (console banner and a
spoken notice in the daytime) instead of the speaker quietly falling back to
30-second previews.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import httpx

from . import music
from .db import get_conn, get_settings, local_tz

log = logging.getLogger(__name__)

CHECK_EVERY_S = 6 * 3600
APP_NAMES = {"netease": "网易云音乐", "kugou": "酷狗音乐", "qqmusic": "QQ 音乐"}
_last_run = 0.0


def logged_in(src: dict) -> bool:
    cfg = src["config"]
    if src["kind"] == "qqmusic":
        return bool(cfg.get("credential"))
    if src["kind"] == "kugou":
        return "token=" in (cfg.get("cookie") or "")
    if src["kind"] == "netease":
        return bool((cfg.get("cookie") or "").strip())
    return False


def expired_sources() -> list[dict]:
    """Logged-in sources whose login has lapsed (for the console banner)."""
    return [s for s in music.song_sources() if s["kind"] in APP_NAMES and s["config"].get("expired")]


def _save(src: dict, cfg: dict) -> None:
    music._update_config(src["id"], cfg)
    src["config"] = cfg


def _merge_cookie(old: str, fresh: dict) -> str:
    parts = dict(p.strip().split("=", 1) for p in (old or "").split(";") if "=" in p)
    parts.update({k: str(v) for k, v in fresh.items() if v not in (None, "")})
    return "; ".join(f"{k}={v}" for k, v in parts.items())


async def _netease(src: dict) -> bool | None:
    """True = alive (refreshed), False = expired, None = couldn't tell."""
    cfg = src["config"]
    base, headers = cfg["base_url"], music._netease_headers(cfg)
    async with httpx.AsyncClient(timeout=10) as client:
        status = (await client.get(f"{base}/login/status?timestamp={int(time.time()*1000)}", headers=headers)).json()
        if not ((status.get("data") or status).get("account")):
            return False
        fresh = (await client.get(f"{base}/login/refresh?timestamp={int(time.time()*1000)}", headers=headers)).json()
    if fresh.get("code") == 200 and fresh.get("cookie"):
        _save(src, {**cfg, "cookie": fresh["cookie"]})
    return True


async def _kugou(src: dict) -> bool | None:
    cfg = src["config"]
    base, headers = cfg["base_url"], music._cookie_headers(cfg)
    async with httpx.AsyncClient(timeout=10) as client:
        data = (await client.get(f"{base}/login/token?timestamp={int(time.time()*1000)}", headers=headers)).json()
    info = data.get("data") or {}
    if data.get("status") == 1 and info.get("token"):
        _save(src, {**cfg, "cookie": _merge_cookie(cfg.get("cookie", ""),
                                                   {"token": info["token"], "userid": info.get("userid")})})
        return True
    if data.get("error_code") in (20017, 20018, 51002, 34175):  # not logged in / token invalid
        return False
    return None


async def _qq(src: dict) -> bool | None:
    cfg = src["config"]
    try:
        fresh = await music._qq_call(cfg, lambda c: c.login.refresh_credential(music._qq_credential(cfg)))
    except music.MusicError as exc:
        return False if ("Expired" in str(exc) or "Refresh" in str(exc)) else None
    if fresh:
        _save(src, {**cfg, "credential": fresh.model_dump(mode="json")})
        return True
    return None


CHECKS = {"netease": _netease, "kugou": _kugou, "qqmusic": _qq}


async def check_all(force: bool = False) -> dict[str, str]:
    """Refresh every login; mark the ones that truly expired and announce them."""
    global _last_run
    if not force and time.time() - _last_run < CHECK_EVERY_S:
        return {}
    _last_run = time.time()
    results = {}
    for src in music.song_sources():
        if src["kind"] not in CHECKS or not logged_in(src):
            continue
        try:
            alive = await CHECKS[src["kind"]](src)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("检查 %s 登录状态失败：%s", src["name"], exc)
            alive = None
        cfg = src["config"]
        if alive is True and cfg.get("expired"):
            _save(src, {k: v for k, v in cfg.items() if k not in ("expired", "expired_told")})
        elif alive is False and not cfg.get("expired"):
            _save(src, {**cfg, "expired": True})
            log.warning("%s 的登录过期了", src["name"])
        results[src["name"]] = {True: "ok", False: "expired", None: "unknown"}[alive]
    _announce_expired()
    return results


def _announce_expired() -> None:
    """One spoken notice per expiry, only in the daytime (else next check)."""
    settings = get_settings()
    hour = datetime.now(local_tz(settings)).hour
    if not 9 <= hour < 21:
        return
    for src in expired_sources():
        if src["config"].get("expired_told"):
            continue
        name = APP_NAMES[src["kind"]]
        with get_conn() as conn:
            conn.execute("INSERT INTO announcements (task_id, text) VALUES (NULL, ?)",
                         (f"提醒一下，{name}的登录过期了，会员歌现在只能试听。有空到控制台的音乐页重新扫码登录一下。",))
            conn.commit()
        _save(src, {**src["config"], "expired_told": True})
