"""Music for the speaker, from sources the user chooses.

The speaker's music tools (search / play / random) ask the console, which
tries each enabled source in order and returns playable URLs:

- library: your own music on a Subsonic-compatible server (Navidrome from
  the app store, or any Subsonic server).
- radio:   internet radio stations from the open radio-browser.info
  directory (free, no key): 中国之声, 音乐之声, regional music stations...
- custom:  any HTTP music API you add yourself, described by a small JSON
  mapping (where the search results are, which field is the title, how to
  get the play URL). This is how you plug in sources of your own.

Song ids handed to the speaker are "<source id>:<item id>".
"""

from __future__ import annotations

import hashlib
import json
import random
import secrets
import time
from urllib.parse import quote, urlencode

import logging

import httpx

from .db import get_conn

KINDS = {
    "netease": "网易云音乐（自建 API，用你自己的账号）",
    "qqmusic": "QQ 音乐（用你自己的账号）",
    "kugou": "酷狗音乐（自建 API，用你自己的账号）",
    "library": "我的曲库（Navidrome / Subsonic）",
    "radio": "网络电台",
    "custom": "自定义音源",
}

RADIO_API = "https://de1.api.radio-browser.info"
UA = "xiaomi-speaker-console/1.0"
RADIO_WORDS = ("电台", "广播", "之声", "fm", "FM", "频道", "radio", "收音机", "换一台", "台")

# Broadcaster-run Chinese streams (HLS), verified playable on the speaker on
# 2026-11-15. List idea from qiaomu-radio (github.com/joeseesun/qiaomu-radio).
CURATED = [
    ("央广中国之声", "https://ngcdn001.cnr.cn/live/zgzs/index.m3u8", "新闻 综合 央广"),
    ("央广经济之声", "https://ngcdn002.cnr.cn/live/jjzs/index.m3u8", "财经 新闻 央广"),
    ("央广音乐之声", "https://ngcdn001.cnr.cn/live/yyzs/index.m3u8", "音乐 流行 央广"),
    ("北京音乐广播 FM97.4", "https://brtv-radiolive.rbc.cn/alive/fm974.m3u8", "音乐 流行 北京"),
    ("北京交通广播 FM103.9", "https://brtv-radiolive.rbc.cn/alive/fm1039.m3u8", "交通 路况 北京"),
    ("CRI 环球资讯", "https://sk.cri.cn/hyhq.m3u8", "国际 新闻 资讯"),
    ("CRI 南海之声", "https://sk.cri.cn/nhzs.m3u8", "国际 资讯 华语"),
    ("河北青年音乐广播", "https://radio.pull.hebtv.com/live/hebqcyy.m3u8", "音乐 青年 河北"),
    ("香港电台第三台", "https://rthkradio3-live.akamaized.net/hls/live/2040079/radio3/master.m3u8", "香港 英语 综合"),
]

# Mood channels -> radio-browser tags (same grouping as qiaomu-radio).
MOODS = [
    (("爵士", "jazz", "蓝调", "灵魂"), ["jazz", "smooth jazz", "soul"]),
    (("古典", "交响", "钢琴", "歌剧"), ["classical", "baroque", "piano"]),
    (("放松", "轻松", "松弛", "助眠", "睡", "安静", "舒缓"), ["chillout", "lounge", "easy listening"]),
    (("专注", "学习", "工作", "写代码", "读书", "白噪"), ["ambient", "downtempo", "instrumental"]),
    (("摇滚", "运动", "健身", "跑步", "燃", "能量"), ["rock", "indie", "alternative"]),
    (("世界", "民谣", "民族", "拉丁"), ["world", "folk", "latin"]),
    (("流行", "华语", "中文歌"), ["pop", "chinese", "mandopop"]),
    (("电子", "舞曲", "蹦迪"), ["electronic", "dance", "house"]),
]


def _mood_tags(q: str) -> list[str] | None:
    for words, tags in MOODS:
        if any(w in q for w in words):
            return tags
    return None


def _curated_hits(q: str) -> list[tuple[int, tuple]]:
    words = [w for w in q.replace("电台", " ").replace("广播", " ").replace("频道", " ").split() if w]
    hits = []
    for i, st in enumerate(CURATED):
        name, _, tags = st
        if any(w in name or w in tags for w in words) or any(t in q for t in tags.split()):
            hits.append((i, st))
    # Best name match first: "北京交通广播" -> 交通 before 音乐.
    return sorted(hits, key=lambda h: -sum(ch in h[1][0] for ch in q))

CUSTOM_EXAMPLE = {
    "search": "https://你的音源/search?keywords={q}&limit={limit}",
    "list": "result.songs",
    "id": "id",
    "name": "name",
    "artist": "artists.0.name",
    "album": "album.name",
    "url": "https://你的音源/song/url?id={id}",
    "url_path": "data.0.url",
    "random": "",
    "headers": {},
}

# Ready-made mappings for common self-hosted music APIs. Replace the host
# with your own deployment; nothing here is contacted until you add it.
CUSTOM_PRESETS = {
    "网易云音乐 API（自建，用你自己的账号）": {
        "search": "https://你的网易云API地址/cloudsearch?keywords={q}&limit={limit}",
        "list": "result.songs", "id": "id", "name": "name", "artist": "ar.0.name", "album": "al.name",
        "url": "https://你的网易云API地址/song/url/v1?id={id}&level=standard",
        "url_path": "data.0.url",
        "random": "https://你的网易云API地址/personalized/newsong?limit={limit}",
        "headers": {"Cookie": "MUSIC_U=登录后从 /login/status 或浏览器 Cookie 里取"},
    },
    "通用模板": CUSTOM_EXAMPLE,
}

_cache: dict[str, dict] = {}
# song id -> a note for the speaker about the URL just handed out ("preview only").
_NOTES: dict[str, str] = {}


def take_note(song_id: str) -> str:
    return _NOTES.pop(song_id, "")


class MusicError(Exception):
    pass


def list_sources(enabled_only: bool = False) -> list[dict]:
    q = "SELECT * FROM music_sources" + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY position, id"
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(q).fetchall()]
    for r in rows:
        r["config"] = json.loads(r["config"] or "{}")
    return rows


def song_sources(enabled_only: bool = False) -> list[dict]:
    """Song sources only: radio lives on its own page (see radio.py)."""
    return [s for s in list_sources(enabled_only) if s["kind"] != "radio"]


def seed_defaults() -> None:
    with get_conn() as conn:
        if conn.execute("SELECT COUNT(*) FROM music_sources").fetchone()[0]:
            return
        conn.execute("INSERT INTO music_sources (name, kind, config, position) VALUES (?, ?, ?, ?)",
                     ("网络电台", "radio", json.dumps({"country": "CN"}), 90))
        conn.commit()


def save_source(name: str, kind: str, config: dict, position: int = 50) -> int:
    name = (name or "").strip() or KINDS.get(kind, "")
    if kind not in KINDS:
        raise MusicError("音源类型不对。")
    if kind == "library":
        for k in ("base_url", "username", "password"):
            if not str(config.get(k, "")).strip():
                raise MusicError("曲库需要填地址、用户名和密码。")
        config["base_url"] = config["base_url"].strip().rstrip("/")
    if kind == "kugou" and not str(config.get("base_url", "")).strip():
        raise MusicError("请填写酷狗 API 的地址（在「应用」里装好「酷狗音乐 API」会自动填）。")
    if kind == "kugou":
        config["base_url"] = config["base_url"].strip().rstrip("/")
    if kind == "netease":
        if not str(config.get("base_url", "")).strip():
            raise MusicError("请填写网易云 API 的地址（在「应用」里装好「网易云音乐 API」会自动填）。")
        config["base_url"] = config["base_url"].strip().rstrip("/")
    if kind == "custom":
        for k in ("search", "list", "id", "name"):
            if not str(config.get(k, "")).strip():
                raise MusicError(f"自定义音源的配置里缺少「{k}」。")
        if not config.get("url") and not config.get("url_field"):
            raise MusicError("自定义音源需要「url」（取播放地址的接口）或「url_field」（搜索结果里直接带的播放地址字段）。")
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO music_sources (name, kind, config, position) VALUES (?, ?, ?, ?)",
                           (name[:40], kind, json.dumps(config, ensure_ascii=False), position))
        conn.commit()
        return cur.lastrowid


def reorder(ids: list[int]) -> None:
    """Set the search priority from a dragged list (first = searched first)."""
    with get_conn() as conn:
        for rank, sid in enumerate(ids, start=1):
            conn.execute("UPDATE music_sources SET position = ? WHERE id = ?", (rank * 10, sid))
        conn.commit()


def set_enabled(source_id: int, enabled: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE music_sources SET enabled = ? WHERE id = ?", (1 if enabled else 0, source_id))
        conn.commit()


def delete_source(source_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM music_sources WHERE id = ?", (source_id,))
        conn.commit()


def any_enabled() -> bool:
    return bool(list_sources(enabled_only=True))


# ---------- helpers ----------

def _path(data, path: str):
    cur = data
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _song(src: dict, item_id, name, artists, album="", duration_ms=None, url=None) -> dict:
    song = {"id": f"{src['id']}:{item_id}", "name": str(name or ""), "album": str(album or ""),
            "artists": [str(a) for a in (artists or []) if a], "duration_ms": duration_ms}
    _cache[song["id"]] = {**song, "_url": url, "_t": time.time()}
    if len(_cache) > 2000:
        for k in sorted(_cache, key=lambda k: _cache[k]["_t"])[:500]:
            _cache.pop(k, None)
    return song


async def _json(client: httpx.AsyncClient, url: str, headers: dict | None = None):
    try:
        resp = await client.get(url, headers={"User-Agent": UA, **(headers or {})}, follow_redirects=True)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        raise MusicError(f"音源返回 HTTP {exc.response.status_code}")
    except (httpx.HTTPError, ValueError) as exc:
        raise MusicError(f"音源连不上或返回的不是 JSON：{exc.__class__.__name__}")


def _subsonic(cfg: dict, method: str, **params) -> str:
    salt = secrets.token_hex(6)
    token = hashlib.md5((cfg["password"] + salt).encode()).hexdigest()
    q = {"u": cfg["username"], "t": token, "s": salt, "v": "1.16.1", "c": "xiaomi-speaker", "f": "json", **params}
    return f"{cfg['base_url']}/rest/{method}.view?{urlencode(q)}"


def _lib_song(src, it):
    return _song(src, it["id"], it.get("title") or it.get("name"), [it.get("artist")], it.get("album"),
                 (it.get("duration") or 0) * 1000 or None)


def _netease_headers(cfg: dict) -> dict:
    cookie = (cfg.get("cookie") or "").strip()
    return {"Cookie": cookie} if cookie else {}


# ---------- originals before covers ----------

import re as _re
_VERSION_WORDS = _re.compile(r"(?i)dj|remix|翻唱|翻自|改编|网友|cover|伴奏|女声版|男声版|弦乐版|钢琴版|史诗版|史詩版|抖音|片段|原唱\s|live|热播版|0\.8|加速|降调|升调")


# title -> singers NetEase marks as the original (QQ / KuGou carry no marks).
_ORIGINAL_SINGERS: dict[str, set[str]] = {}


def _title_key(name: str) -> str:
    core = _re.split(r"[（(\[-]", name or "")[0]
    return "".join(ch for ch in core.lower() if ch.isalnum())


def _is_original_take(song: dict) -> bool:
    """False for another singer's take of a song whose original we know."""
    if any("dj" in (a or "").lower() for a in song.get("artists") or []):
        return False
    known = _ORIGINAL_SINGERS.get(_title_key(song.get("name") or ""))
    if not known:
        return True
    return any(_artist_is(k, a) or _artist_is(a, k) for k in known for a in song.get("artists") or [])


def _artist_is(word: str, artist: str) -> bool:
    """Exact singer match: 王菲 is not 王菲菲; 等什么君 matches 邓寓君（等什么君）."""
    artist = (artist or "").strip()
    if word == artist:
        return True
    inner = _re.findall(r"[（(]([^）)]+)[）)]", artist)
    return word in inner or word == _re.sub(r"[（(].*?[）)]", "", artist).strip()


def _requested_artists(songs: list[dict], words: list[str]) -> list[str]:
    names = {a.get("name") or "" for t in songs for a in t.get("ar") or t.get("artists") or []}
    return [w for w in words if any(_artist_is(w, n) for n in names)]


def _originals_first(songs: list[dict], query: str) -> list[dict]:
    """NetEase marks versions: originCoverType 1 original, 2 cover, 3 remix,
    0 unknown. Put originals first, the artist the user named before others,
    and bring back the original a cover points to when it isn't in the list."""
    words = [w for w in _re.split(r"\s+", query.strip()) if w]
    seen = {t["id"] for t in songs}
    extra = []
    for t in songs:
        origin = t.get("originSongSimpleData") or {}
        oid = origin.get("songId")
        if oid and oid not in seen:
            seen.add(oid)
            extra.append({"id": oid, "name": origin.get("name"), "originCoverType": 1,
                          "ar": [{"name": a.get("name")} for a in origin.get("artists") or []],
                          "al": {"name": (origin.get("albumMeta") or {}).get("name")}, "_restored": True})

    singer_words = _requested_artists(songs + extra, words)
    for t in songs + extra:
        if t.get("originCoverType") == 1:
            _ORIGINAL_SINGERS.setdefault(_title_key(t.get("name") or ""), set()).update(
                a.get("name") or "" for a in t.get("ar") or [])

    def score(t: dict) -> float:
        name = t.get("name") or ""
        names = [a.get("name") or "" for a in t.get("ar") or []]
        kind = t.get("originCoverType", 0)
        value = {1: 30, 0: 15, 3: 0, 2: 0}.get(kind, 10)
        if _VERSION_WORDS.search(name):
            value -= 20
        artist_words = [w for w in singer_words if any(_artist_is(w, n) for n in names)]
        title_words = [w for w in words if w not in singer_words]
        if artist_words:
            value += 25  # the singer the user asked for
        if title_words:
            if any(w in name for w in title_words):
                value += 40  # the song asked for, not just the same singer
                if name.split("（")[0].split("(")[0].strip() in title_words:
                    value += 5  # exact title
            else:
                value -= 40
        if t.get("_restored"):
            value += 10  # the original a cover points to
        return value + (t.get("pop") or 0) / 100

    return sorted(songs + extra, key=score, reverse=True)


def _ranked_songs(src: dict, items: list[dict], q: str, limit: int) -> list[dict]:
    """Shared ranking (originals first, exact singer/title) for any source
    whose items are shaped like NetEase's: id, name, ar[{name}], al{name}."""
    items = _originals_first(items, q)
    singers = _requested_artists(items, [w for w in _re.split(r"\s+", q) if w])
    out = []
    for t in items[:limit]:
        names = [a.get("name") or "" for a in t.get("ar") or []]
        cover = (t.get("originCoverType") in (2, 3) or _VERSION_WORDS.search(t.get("name") or "")
                 or (singers and not any(_artist_is(w, n) for w in singers for n in names)))
        out.append(_song(src, t["id"], t.get("name"), names, "非原版" if cover else (t.get("al") or {}).get("name"), t.get("dt")))
    return out


def _cookie_headers(cfg: dict) -> dict:
    cookie = (cfg.get("cookie") or "").strip()
    return {"Cookie": cookie} if cookie else {}


# ---------- QQ Music (qqmusic-api-python, in process) ----------

QQ_STREAM = "https://isure.stream.qqmusic.qq.com/"


def _qq_credential(cfg: dict):
    from qqmusic_api.models.request import Credential
    data = cfg.get("credential")
    return Credential.model_validate(data) if data else None


async def _qq_call(cfg: dict, make, attr: str | None = None):
    from qqmusic_api import Client
    try:
        async with Client(credential=_qq_credential(cfg)) as client:
            result = await make(client)
    except Exception as exc:  # the library raises its own error types
        name = exc.__class__.__name__
        logging.getLogger(__name__).warning("QQ 音乐请求失败：%s: %s", name, exc)
        friendly = {
            "LoginDeviceLimitError": "这个 QQ 账号登录的设备太多了，请在手机 QQ 音乐里退出几台旧设备再扫。",
            "LoginAccountRestrictedError": "QQ 音乐说这个账号受限，暂时不能这样登录。",
            "LoginRateLimitError": "扫得太频繁了，等一两分钟再试。",
            "LoginAuthExpiredError": "这张二维码已经失效，请重新点一次扫码。",
        }.get(name)
        if friendly:
            raise MusicError(friendly)
        if "Ratelimit" in name or "RateLimit" in name:
            raise MusicError("QQ 音乐触发了风控，扫码登录后会好很多。")
        raise MusicError(f"QQ 音乐请求失败：{name}")
    return (getattr(result, attr, None) or []) if attr else result


def _qq_item(t) -> dict:
    album = getattr(t, "album", None)
    return {"id": f"{t.mid}|{t.id}", "name": t.name, "ar": [{"name": a.name} for a in getattr(t, "singer", []) or []],
            "al": {"name": getattr(album, "name", "") if album else ""},
            "dt": (getattr(t, "interval", 0) or 0) * 1000, "originCoverType": 0}


async def _qq_url(src: dict, mid: str) -> str:
    from qqmusic_api.modules.song import SongFileInfo
    cfg = src["config"]
    for attempt in range(2):
        try:
            resp = await _qq_call(cfg, lambda c: c.song.get_song_urls([SongFileInfo(mid=mid)], credential=_qq_credential(cfg)))
        except MusicError as exc:
            if "CredentialExpired" not in str(exc):
                raise
            resp = None
        purl = (resp.data[0].purl if resp and resp.data else "") or ""
        if purl:
            return QQ_STREAM + purl
        if resp is None and attempt == 1:
            # Refresh didn't help: stop searching QQ until the user scans again,
            # otherwise every request burns seconds on links that never work.
            _update_config(src["id"], {**cfg, "expired": True})
            raise MusicError("QQ 音乐登录过期了，请在控制台「音乐」页重新扫码登录。")
        if attempt == 0 and cfg.get("credential"):
            # Login keys expire every few days; refresh once and retry.
            try:
                fresh = await _qq_call(cfg, lambda c: c.login.refresh_credential(_qq_credential(cfg)))
                cfg = {**cfg, "credential": fresh.model_dump(mode="json")}
                _update_config(src["id"], cfg)
                continue
            except MusicError:
                pass
        break
    if not cfg.get("credential"):
        raise MusicError("QQ 音乐要先扫码登录才能放会员歌。")
    raise MusicError("QQ 音乐不给这首歌的播放地址，通常是需要会员或已下架。")


def _fresh(cfg: dict) -> dict:
    """Config after a new login: drop the "login expired" marks."""
    return {k: v for k, v in cfg.items() if k not in ("expired", "expired_told")}


def _update_config(source_id: int, cfg: dict) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE music_sources SET config = ? WHERE id = ?", (json.dumps(cfg, ensure_ascii=False), source_id))
        conn.commit()


# ---------- QR login for NetEase / KuGou / QQ (scan with the app; no password) ----------

_qr_sessions: dict[str, dict] = {}


async def qr_start(source_id: int, login_type: str = "qq") -> dict:
    import base64
    for k in [k for k, v in _qr_sessions.items() if time.time() - v.get("at", 0) > 600]:
        _qr_sessions.pop(k, None)  # codes older than 10 minutes are dead anyway
    src = next((s for s in list_sources() if s["id"] == source_id), None)
    if not src:
        raise MusicError("音源不存在。")
    if src["kind"] == "netease":
        return await netease_qr_start(src["config"]["base_url"])
    if src["kind"] == "kugou" and login_type == "wx":
        # WeChat scan: KuGou's open-platform login (see login_wx_create.js).
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{src['config']['base_url']}/login/wx/create?timestamp={int(time.time()*1000)}")
            data = resp.json()
            uuid = data.get("uuid")
            img = ((data.get("qrcode") or {}).get("qrcodebase64") or "")
            if not uuid or not img:
                raise MusicError("酷狗 API 没有返回微信登录二维码。")
            _qr_sessions[uuid] = {"cookies": dict(client.cookies), "at": time.time(), "wx": True}
        return {"key": uuid, "img": img if img.startswith("data:") else f"data:image/jpeg;base64,{img}"}
    if src["kind"] == "kugou":
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{src['config']['base_url']}/login/qr/key?qrimg=true&timestamp={int(time.time()*1000)}")
            data = (resp.json().get("data") or {})
            if not data.get("qrcode"):
                raise MusicError("酷狗 API 没有返回登录二维码。")
            _qr_sessions[data["qrcode"]] = {"cookies": dict(client.cookies), "at": time.time()}
        return {"key": data["qrcode"], "img": data.get("qrcode_img", "")}
    if src["kind"] == "qqmusic":
        from qqmusic_api.models.login import QRLoginType
        kind = QRLoginType.WX if login_type == "wx" else QRLoginType.QQ
        qr = await _qq_call({}, lambda c: c.login.get_qrcode(kind))
        _qr_sessions[qr.identifier] = {"qr": qr, "at": time.time()}
        return {"key": qr.identifier, "img": f"data:{qr.mimetype};base64,{base64.b64encode(qr.data).decode()}"}
    raise MusicError("这个音源不需要登录。")


async def qr_check(source_id: int, key: str) -> str:
    src = next((s for s in list_sources() if s["id"] == source_id), None)
    if not src:
        raise MusicError("音源不存在。")
    if src["kind"] == "netease":
        return await netease_qr_check(source_id, key)
    session = _qr_sessions.get(key)
    if not session:
        return "expired"
    if src["kind"] == "kugou" and session.get("wx"):
        base = src["config"]["base_url"]
        # WeChat's check is a long poll (~25 s until something changes).
        async with httpx.AsyncClient(timeout=35, cookies=session["cookies"]) as client:
            try:
                check = (await client.get(f"{base}/login/wx/check?uuid={quote(key)}&timestamp={int(time.time()*1000)}")).json()
            except (httpx.HTTPError, ValueError):
                return "waiting"
            code = check.get("wx_errcode")
            if code == 405 and check.get("wx_code"):
                # Confirmed in WeChat: trade the code for a KuGou login.
                await client.get(f"{base}/login/openplat?code={quote(check['wx_code'])}&timestamp={int(time.time()*1000)}")
                cookies = dict(client.cookies)
                if not cookies.get("token"):
                    return "expired"
                _update_config(source_id, {**_fresh(src["config"]), "cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())})
                _qr_sessions.pop(key, None)
                return "done"
        return {408: "waiting", 404: "scanned", 402: "expired", 403: "expired"}.get(code, "waiting")
    if src["kind"] == "kugou":
        async with httpx.AsyncClient(timeout=10, cookies=session["cookies"]) as client:
            resp = await client.get(f"{src['config']['base_url']}/login/qr/check?key={quote(key)}&timestamp={int(time.time()*1000)}")
            status = ((resp.json().get("data") or {}).get("status"))
            session["cookies"] = dict(client.cookies)
        if status == 4:
            cookie = "; ".join(f"{k}={v}" for k, v in session["cookies"].items())
            _update_config(source_id, {**_fresh(src["config"]), "cookie": cookie})
            _qr_sessions.pop(key, None)
            return "done"
        return {0: "expired", 1: "waiting", 2: "scanned"}.get(status, "waiting")
    if src["kind"] == "qqmusic":
        result = await _qq_call({}, lambda c: c.login.check_qrcode(session["qr"]))
        event = getattr(result.event, "name", str(result.event))
        if result.credential:
            _update_config(source_id, {**_fresh(src["config"]),
                                       "credential": result.credential.model_dump(mode="json")})
            _qr_sessions.pop(key, None)
            return "done"
        return {"TIMEOUT": "expired", "REFUSE": "expired", "CONF": "scanned"}.get(event, "waiting")
    return "expired"


# ---------- NetEase QR login (scan with the NetEase app; no password) ----------

async def netease_qr_start(base_url: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        key = (await _json(client, f"{base_url}/login/qr/key?timestamp={int(time.time()*1000)}")).get("data", {}).get("unikey")
        if not key:
            raise MusicError("网易云 API 没有返回登录二维码。")
        img = await _json(client, f"{base_url}/login/qr/create?key={key}&qrimg=true&timestamp={int(time.time()*1000)}")
    return {"key": key, "img": (img.get("data") or {}).get("qrimg", "")}


async def netease_qr_check(source_id: int, key: str) -> str:
    src = next((s for s in list_sources() if s["id"] == source_id and s["kind"] == "netease"), None)
    if not src:
        raise MusicError("音源不存在。")
    async with httpx.AsyncClient(timeout=10) as client:
        data = await _json(client, f"{src['config']['base_url']}/login/qr/check?key={key}&timestamp={int(time.time()*1000)}")
    code = data.get("code")
    if code == 803:
        cfg = {**_fresh(src["config"]), "cookie": data.get("cookie", "")}
        with get_conn() as conn:
            conn.execute("UPDATE music_sources SET config = ? WHERE id = ?", (json.dumps(cfg, ensure_ascii=False), source_id))
            conn.commit()
        return "done"
    if code not in (800, 801, 802):
        # e.g. 8821: NetEase wants extra verification for this login.
        raise MusicError(data.get("message") or f"网易云返回 {code}，请稍后重新扫码。")
    return {800: "expired", 801: "waiting", 802: "scanned"}.get(code, "waiting")


def use_source(name: str) -> dict:
    """Put a source first (voice: 「用网易云放」)."""
    said = (name or "").strip().lower()
    if any(w in said for w in ("电台", "广播", "radio")):
        raise MusicError("电台不是歌曲音源，直接说「打开电台」就行。")
    sources = song_sources()
    hit = next((s for s in sources if said and (said in s["name"].lower() or said in KINDS[s["kind"]].lower())), None)
    if not hit:
        raise MusicError("没有这个音源。现有：" + "、".join(s["name"] for s in sources) + "。")
    with get_conn() as conn:
        conn.execute("UPDATE music_sources SET position = position + 1 WHERE position <= ?", (hit["position"],))
        conn.execute("UPDATE music_sources SET position = 0, enabled = 1 WHERE id = ?", (hit["id"],))
        conn.commit()
    return hit


# ---------- per-kind search ----------

async def _search_one(client, src: dict, q: str, limit: int) -> list[dict]:
    cfg, kind = src["config"], src["kind"]
    if kind == "library":
        data = await _json(client, _subsonic(cfg, "search3", query=q, songCount=limit, albumCount=0, artistCount=0))
        items = _path(data, "subsonic-response.searchResult3.song") or []
        return [_lib_song(src, it) for it in items[:limit]]
    if kind == "kugou":
        data = await _json(client, f"{cfg['base_url']}/search?{urlencode({'keywords': q, 'pagesize': max(limit, 15)})}",
                           _cookie_headers(cfg))
        items = [{"id": f"{t.get('FileHash')}|{t.get('MixSongID') or 0}", "name": t.get("SongName"),
                  "ar": [{"name": n} for n in (t.get("SingerName") or "").split("、") if n],
                  "al": {"name": t.get("AlbumName")}, "dt": (t.get("Duration") or 0) * 1000, "originCoverType": 0}
                 for t in ((data.get("data") or {}).get("lists") or []) if t.get("FileHash")]
        return _ranked_songs(src, items, q, limit)
    if kind == "qqmusic":
        items = [_qq_item(t) for t in await _qq_call(cfg, lambda c: c.search.search_by_type(q, num=max(limit, 15)), "song")]
        return _ranked_songs(src, items, q, limit)
    if kind == "netease":
        data = await _json(client, f"{cfg['base_url']}/cloudsearch?{urlencode({'keywords': q, 'limit': max(limit, 15)})}",
                           _netease_headers(cfg))
        songs = (data.get("result") or {}).get("songs") or []
        songs = _originals_first(songs, q)
        if not (cfg.get("cookie") or "").strip():
            # Not logged in: members-only songs are 30 s previews. For a
            # singer or mood, play full songs first; a song asked for by name
            # still comes first even if it is only a preview.
            songs.sort(key=lambda t: t.get("fee") == 1 and (t.get("name") or "~") not in q)
        singers = _requested_artists(songs, [w for w in _re.split(r"\s+", q) if w])
        out = []
        for t in songs[:limit]:
            names = [a.get("name") or "" for a in t.get("ar") or []]
            cover = (t.get("originCoverType") in (2, 3) or _VERSION_WORDS.search(t.get("name") or "")
                     or (singers and not any(_artist_is(w, n) for w in singers for n in names)))
            out.append(_song(src, t["id"], t.get("name"), [a.get("name") for a in t.get("ar") or []],
                             "非原版" if cover else (t.get("al") or {}).get("name"), t.get("dt")))
        return out
    if kind == "radio":
        mood = _mood_tags(q)
        if not mood and not any(w in q for w in RADIO_WORDS):
            # "周杰伦" shouldn't turn into a radio station; only radio-ish requests do.
            return []
        out = [_song(src, f"c{i}", name, ["精选电台"], tags.split()[0], None, url)
               for i, (name, url, tags) in _curated_hits(q)]
        if mood:
            import asyncio
            seen = set()
            urls = [f"{RADIO_API}/json/stations/search?" + urlencode(
                {"tag": tag, "limit": 8, "hidebroken": "true", "order": "clickcount", "reverse": "true"}) for tag in mood]
            batches = await asyncio.gather(*(_json(client, u) for u in urls), return_exceptions=True)
            for tag, data in zip(mood, batches):
                if isinstance(data, Exception):
                    continue
                for st in data:
                    if st["stationuuid"] in seen or st.get("codec", "").upper() not in ("MP3", "AAC", "AAC+", ""):
                        continue
                    seen.add(st["stationuuid"])
                    out.append(_song(src, st["stationuuid"], st["name"].strip(), ["网络电台"], tag, None,
                                     st.get("url_resolved") or st.get("url")))
            random.shuffle(out)  # a different station each time you ask for jazz
            from . import radio
            out = radio.rank(out)  # but stations you like first, ones you skip last
        elif not out:
            name = q
            for w in ("网络电台", "电台", "广播", "收音机", "收听", "播放", "打开", "换一台"):
                name = name.replace(w, "")
            params = {"limit": limit, "hidebroken": "true", "order": "clickcount", "reverse": "true"}
            if name.strip():
                params["name"] = name.strip()
            else:
                out = [_song(src, f"c{i}", n, ["精选电台"], t.split()[0], None, u) for i, (n, u, t) in enumerate(CURATED)]
            if name.strip():
                data = await _json(client, f"{RADIO_API}/json/stations/search?{urlencode(params)}")
                out = [_song(src, st["stationuuid"], st["name"].strip(), ["网络电台"], st.get("country", ""), None,
                             st.get("url_resolved") or st.get("url")) for st in data]
        return out[:limit]
    # custom
    url = cfg["search"].replace("{q}", quote(q)).replace("{limit}", str(limit))
    data = await _json(client, url, cfg.get("headers"))
    items = _path(data, cfg["list"]) or []
    out = []
    for it in items[:limit]:
        artist = _path(it, cfg.get("artist", ""))
        artists = artist if isinstance(artist, list) else [artist]
        if artists and isinstance(artists[0], dict):
            artists = [a.get("name") for a in artists]
        direct = _path(it, cfg["url_field"]) if cfg.get("url_field") else None
        out.append(_song(src, _path(it, cfg["id"]), _path(it, cfg["name"]), artists,
                         _path(it, cfg.get("album", "")), None, direct))
    return out


async def search(q: str, limit: int = 5) -> list[dict]:
    """All enabled sources are searched at once; results keep the sources'
    priority order (the first source's hits come first)."""
    import asyncio

    q, limit = (q or "").strip(), max(1, min(int(limit), 20))
    # A QQ source whose login has expired only returns links that fail.
    sources = [s for s in song_sources(enabled_only=True)
               if not (s["kind"] == "qqmusic" and s["config"].get("expired"))]
    async with httpx.AsyncClient(timeout=6) as client:
        outcomes = await asyncio.gather(*(_search_one(client, src, q, limit) for src in sources),
                                        return_exceptions=True)
    results, errors = [], []
    for src, outcome in zip(sources, outcomes):
        if isinstance(outcome, MusicError):
            errors.append(f"{src['name']}：{outcome}")
        elif isinstance(outcome, Exception):
            errors.append(f"{src['name']}：{outcome.__class__.__name__}")
        else:
            results += outcome
    # Originals only: covers, DJ / live versions and other singers' takes of
    # the song are not played (stations are untouched).
    results = [r for r in results
               if r.get("album") != "非原版" and (r.get("artists") == ["网络电台"] or r.get("artists") == ["精选电台"]
                                                 or _is_original_take(r))][:limit]
    if not results and errors:
        raise MusicError("；".join(errors))
    if not results and any(s["kind"] in ("netease", "qqmusic", "kugou") for s in sources) \
            and not any(w in q for w in RADIO_WORDS) and not _mood_tags(q):
        raise MusicError("没找到能放的原唱版本。原唱多半是会员歌，在控制台「音乐」页扫码登录网易云、QQ 音乐或酷狗的会员账号后就能放。")
    if not results and not sources:
        raise MusicError("还没有能搜歌的音源。请在控制台「音乐」页添加网易云、QQ 音乐、酷狗或曲库。")
    return results


async def random_songs(limit: int = 10) -> list[dict]:
    async with httpx.AsyncClient(timeout=8) as client:
        for src in list_sources(enabled_only=True):
            try:
                if src["kind"] == "netease":
                    cfg = src["config"]
                    data = await _json(client, f"{cfg['base_url']}/personalized/newsong?limit={limit}", _netease_headers(cfg))
                    items = [r.get("song") or r for r in data.get("result") or []]
                    if items:
                        return [_song(src, t["id"], t.get("name"), [a.get("name") for a in t.get("artists") or t.get("ar") or []],
                                      "", None) for t in items[:limit]]
                elif src["kind"] == "library":
                    data = await _json(client, _subsonic(src["config"], "getRandomSongs", size=limit))
                    items = _path(data, "subsonic-response.randomSongs.song") or []
                    if items:
                        return [_lib_song(src, it) for it in items]
                elif src["kind"] == "custom" and src["config"].get("random"):
                    data = await _json(client, src["config"]["random"].replace("{limit}", str(limit)),
                                       src["config"].get("headers"))
                    cfg = src["config"]
                    items = _path(data, cfg["list"]) or []
                    if items:
                        return [_song(src, _path(it, cfg["id"]), _path(it, cfg["name"]),
                                      [_path(it, cfg.get("artist", ""))], "", None,
                                      _path(it, cfg["url_field"]) if cfg.get("url_field") else None)
                                for it in items[:limit]]
            except MusicError:
                continue
    raise MusicError("没有能随机放歌的音源。网易云、曲库或带随机接口的自定义音源都可以。")


def _good_original(t: dict, logged_in: bool) -> bool:
    """Recommend only originals that will actually play."""
    if t.get("originCoverType") in (2, 3) or _VERSION_WORDS.search(t.get("name") or ""):
        return False
    if not logged_in and t.get("fee") == 1:
        return False  # members-only: only a 30 s preview without login
    return True


async def more_like(song_id: str, limit: int = 10) -> list[dict]:
    """What to play next once a song ends ("play on until I say stop"): the
    singer's popular songs mixed with NetEase's similar songs, originals only."""
    sid, _, item_id = song_id.partition(":")
    src = next((s for s in list_sources(enabled_only=True) if str(s["id"]) == sid), None)
    if src and src["kind"] == "radio":
        from . import radio  # "换一台": liked stations first
        return radio.next_after(song_id, limit)
    if src and src["kind"] == "qqmusic":
        try:
            sim = await _qq_call(src["config"], lambda c: c.song.get_similar_song(int(item_id.split("|")[1])), "song")
            picks = [_qq_item(t) for t in sim if not _VERSION_WORDS.search(getattr(t, "name", "") or "")]
            random.shuffle(picks)
            if picks:
                return [_song(src, t["id"], t["name"], [a["name"] for a in t["ar"]], t["al"]["name"], t["dt"])
                        for t in picks[:limit]]
        except (MusicError, ValueError, IndexError):
            pass
    if src and src["kind"] == "kugou":
        artist = ((_cache.get(song_id) or {}).get("artists") or [""])[0]
        if artist:
            try:
                async with httpx.AsyncClient(timeout=8) as client:
                    picks = [x for x in await _search_one(client, src, artist, 15)
                             if x["id"] != song_id and x["album"] != "非原版"]
                random.shuffle(picks)
                if picks:
                    return picks[:limit]
            except MusicError:
                pass
    if src and src["kind"] == "netease":
        cfg = src["config"]
        logged_in = bool((cfg.get("cookie") or "").strip())
        picks: list[dict] = []
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                detail = await _json(client, f"{cfg['base_url']}/song/detail?ids={quote(item_id)}", _netease_headers(cfg))
                artist_ids = [a.get("id") for a in ((detail.get("songs") or [{}])[0].get("ar") or []) if a.get("id")]
                same_artist = []
                if artist_ids:
                    top = await _json(client, f"{cfg['base_url']}/artist/top/song?id={artist_ids[0]}", _netease_headers(cfg))
                    same_artist = [t for t in top.get("songs") or [] if _good_original(t, logged_in)]
                    random.shuffle(same_artist)
                simi = await _json(client, f"{cfg['base_url']}/simi/song?id={quote(item_id)}", _netease_headers(cfg))
                similar = [t for t in simi.get("songs") or [] if _good_original(t, logged_in)]
            # Alternate: one by the same singer, one similar song.
            while (same_artist or similar) and len(picks) < limit:
                for pool in (same_artist, similar):
                    if pool:
                        picks.append(pool.pop(0))
        except MusicError:
            picks = []
        if picks:
            return [_song(src, t["id"], t.get("name"), [a.get("name") for a in t.get("ar") or t.get("artists") or []],
                          (t.get("al") or t.get("album") or {}).get("name"), t.get("dt") or t.get("duration"))
                    for t in picks if str(t["id"]) != item_id]
    return await random_songs(limit)


def song(song_id: str) -> dict:
    sid, _, item_id = song_id.partition(":")
    if item_id.startswith("c") and item_id[1:].isdigit() and int(item_id[1:]) < len(CURATED) and song_id not in _cache:
        name, url, tags = CURATED[int(item_id[1:])]
        src = next((s for s in list_sources() if str(s["id"]) == sid), None)
        if src:
            _song(src, item_id, name, ["精选电台"], tags.split()[0], None, url)
    item = _cache.get(song_id)
    if item:
        return {k: v for k, v in item.items() if not k.startswith("_")}
    return {"id": song_id, "name": song_id.split(":", 1)[-1], "artists": [], "album": "", "duration_ms": None}


async def play_url(song_id: str) -> str:
    sid, _, item_id = song_id.partition(":")
    cached = _cache.get(song_id)
    if cached and cached.get("_url"):
        return cached["_url"]
    src = next((s for s in list_sources() if str(s["id"]) == sid), None)
    if not src:
        raise MusicError("这首歌的音源已经被删除。")
    cfg = src["config"]
    if src["kind"] == "library":
        return _subsonic(cfg, "stream", id=item_id)
    async with httpx.AsyncClient(timeout=8) as client:
        if src["kind"] == "radio" and item_id.startswith("c") and item_id[1:].isdigit():
            idx = int(item_id[1:])
            if idx < len(CURATED):
                return CURATED[idx][1]
        if src["kind"] == "radio":
            data = await _json(client, f"{RADIO_API}/json/stations/byuuid/{quote(item_id)}")
            if data:
                return data[0].get("url_resolved") or data[0]["url"]
            raise MusicError("电台已下线。")
        if src["kind"] == "kugou":
            hash_, _, mix = item_id.partition("|")
            data = await _json(client, f"{cfg['base_url']}/song/url?{urlencode({'hash': hash_, 'album_audio_id': mix or 0})}",
                               _cookie_headers(cfg))
            urls = data.get("url") or data.get("backupUrl") or []
            url = urls[0] if isinstance(urls, list) and urls else (urls if isinstance(urls, str) else "")
            if not url:
                # Members-only without login: KuGou can still give the preview part.
                trial = await _json(client, f"{cfg['base_url']}/song/url?{urlencode({'hash': hash_, 'album_audio_id': mix or 0, 'free_part': 1})}",
                                    _cookie_headers(cfg))
                turls = trial.get("url") or trial.get("backupUrl") or []
                turl = turls[0] if isinstance(turls, list) and turls else (turls if isinstance(turls, str) else "")
                if turl:
                    _NOTES[song_id] = "会员歌，只能先放试听片段"
                    return turl
            if not url:
                if data.get("errcode") == 20028 or not (cfg.get("cookie") or "").count("token="):
                    raise MusicError("酷狗要先扫码登录才能放歌。")
                raise MusicError("酷狗不给这首歌的播放地址，通常是需要会员或已下架。")
            return url
        if src["kind"] == "qqmusic":
            return await _qq_url(src, item_id.partition("|")[0])
        if src["kind"] == "netease":
            # /song/url/v1 is refused (-460 "risky network") for a data-center
            # IP without login; the older /song/url still works.
            found = {}
            for path in (f"/song/url/v1?{urlencode({'id': item_id, 'level': 'standard'})}",
                         f"/song/url?{urlencode({'id': item_id, 'br': 320000})}"):
                try:
                    data = await _json(client, cfg["base_url"] + path, _netease_headers(cfg))
                except MusicError:
                    continue
                found = ((data.get("data") or [{}])[0] or {})
                if found.get("url"):
                    break
            if not found.get("url"):
                raise MusicError("网易云不给这首歌的播放地址，通常是需要会员或已下架。")
            if found.get("freeTrialInfo"):
                _NOTES[song_id] = "会员歌，只能先放约 30 秒试听"
            return str(found["url"])
        if not cfg.get("url"):
            raise MusicError("这首歌没有播放地址。")
        url = cfg["url"].replace("{id}", quote(item_id))
        if not cfg.get("url_path"):
            return url  # the url itself streams audio
        found = _path(await _json(client, url, cfg.get("headers")), cfg["url_path"])
        if not found:
            raise MusicError("音源没有给出这首歌的播放地址，可能需要会员或已下架。")
        return str(found)
