"""Radio on top of the music module's radio source: favourites, history,
taste-based ranking and a relay that buffers far-away streams.

Station ids are the music module's song ids ("<source id>:<station key>"),
so anything here can be played with the speaker's normal music commands.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import quote, urlencode

import httpx

from . import music
from .db import get_conn, get_settings, now_local

SKIP_SECONDS = 60  # switching away sooner than this counts as a skip
_last_play: dict = {"id": None, "at": 0.0}


def radio_source() -> dict:
    """The internal station source (kept out of the music page). Station ids
    stay "<its id>:<key>", so it is created once and always enabled."""
    src = next((s for s in music.list_sources() if s["kind"] == "radio"), None)
    if not src:
        music.save_source("网络电台", "radio", {"country": "CN"}, 90)
        src = next(s for s in music.list_sources() if s["kind"] == "radio")
    if not src["enabled"]:
        music.set_enabled(src["id"], True)
    return src


_search_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}
CACHE_S = 6 * 3600


async def search(q: str, limit: int = 8) -> list[dict]:
    """Stations for a name or mood; empty = the ones the user likes most.
    Directory lookups are cached for 6 hours (the overseas directory takes
    seconds per query; station lists barely change); taste ranking is
    re-applied on every call."""
    key = ((q or "").strip(), limit)
    hit = _search_cache.get(key)
    if hit and time.time() - hit[0] < CACHE_S:
        for st in hit[1]:  # make the ids playable again after a restart
            music._cache.setdefault(st["id"], {**st, "_t": time.time()})
        return rank([{k: v for k, v in x.items() if not k.startswith("_")} for x in hit[1]])
    out = await _search(q, limit)
    if out and key[0] not in ("", "电台", "广播", "收音机", "网络电台"):
        _search_cache[key] = (time.time(), [
            {**x, **{k: v for k, v in music._cache.get(x["id"], {}).items() if k.startswith("_")}} for x in out])
    return out


async def prewarm() -> None:
    """Fill the cache for every category button (called in the background)."""
    import asyncio
    for words, _ in music.MOODS:
        try:
            await search(words[0], 20)
        except Exception:
            pass
        await asyncio.sleep(1)


async def _search(q: str, limit: int = 8) -> list[dict]:
    q = (q or "").strip()
    for w in ("打开", "播放", "放个", "放点", "来点", "听", "一下"):
        q = q.replace(w, "")
    if not q or q in ("电台", "广播", "收音机", "网络电台"):
        return recommended(limit)
    src = radio_source()
    async with httpx.AsyncClient(timeout=8) as client:
        hits = await music._search_one(client, src, q if music._mood_tags(q) or any(
            w in q for w in music.RADIO_WORDS) else f"{q} 电台", limit)
    return rank(hits) or recommended(limit)


def _meta(song_id: str) -> dict:
    item = music._cache.get(song_id) or {}
    return {"name": item.get("name") or song_id.split(":", 1)[-1], "tags": item.get("album") or "",
            "url": item.get("_url") or ""}


# ---------------------------------------------------------- history / likes

def record_play(song_id: str) -> None:
    """Called whenever a station's stream is handed to the speaker."""
    now = time.time()
    prev, prev_at = _last_play["id"], _last_play["at"]
    if prev and prev != song_id and now - prev_at < SKIP_SECONDS:
        with get_conn() as conn:
            conn.execute("UPDATE radio_history SET skips = skips + 1 WHERE station_id = ?", (prev,))
            conn.commit()
    _last_play.update(id=song_id, at=now)
    meta = _meta(song_id)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO radio_history (station_id, name, tags, plays, skips, last_played) VALUES (?, ?, ?, 1, 0, ?) "
            "ON CONFLICT(station_id) DO UPDATE SET plays = plays + 1, last_played = excluded.last_played, "
            "name = CASE WHEN excluded.name = substr(excluded.station_id, instr(excluded.station_id, ':') + 1) "
            "THEN radio_history.name ELSE excluded.name END",
            (song_id, meta["name"], meta["tags"], now_local().isoformat(timespec="seconds")))
        conn.commit()


def current_station() -> dict | None:
    if not _last_play["id"]:
        return None
    return {"id": _last_play["id"], **_meta(_last_play["id"])}


def favourite(song_id: str, on: bool = True) -> dict:
    meta = _meta(song_id)
    with get_conn() as conn:
        if on:
            conn.execute("INSERT OR IGNORE INTO radio_favorites (station_id, name, tags, added_at) VALUES (?, ?, ?, ?)",
                         (song_id, meta["name"], meta["tags"], now_local().isoformat(timespec="seconds")))
        else:
            conn.execute("DELETE FROM radio_favorites WHERE station_id = ?", (song_id,))
        conn.commit()
    return {"id": song_id, **meta}


def favourites() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM radio_favorites ORDER BY added_at DESC")]


def history(limit: int = 30) -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM radio_history ORDER BY last_played DESC LIMIT ?", (limit,))]


def taste() -> dict[str, float]:
    """station id -> score: favourites and plays up, skips down."""
    score: dict[str, float] = {}
    for h in history(200):
        score[h["station_id"]] = h["plays"] * 1.0 - h["skips"] * 2.0
    for f in favourites():
        score[f["station_id"]] = score.get(f["station_id"], 0) + 5
    return score


def rank(stations: list[dict]) -> list[dict]:
    """Stable re-order by taste (unknown stations keep their place); overseas
    high-bitrate streams and stations that stuttered before go down."""
    score = taste()
    return sorted(stations, key=lambda s: -(score.get(s["id"], 0) - stutter_risk(s["id"])))


# ------------------------------------------------- stutter risk / slow links

HEAVY_KBPS = 160  # overseas streams above this rarely keep up cross-border


def stutter_risk(song_id: str) -> float:
    item = music._cache.get(song_id) or {}
    risk = 0.0
    if item.get("_cc") and item["_cc"] != "CN" and (item.get("_br") or 0) > HEAVY_KBPS:
        risk += 1.5
    if is_slow(song_id):
        risk += 3.0
    return risk


def _url_map() -> dict[str, str]:
    """station id -> stream URL, kept across restarts (the in-memory cache is not)."""
    import json
    try:
        return json.loads(get_settings().get("radio_url_map") or "{}")
    except ValueError:
        return {}


def _remember_url(song_id: str, upstream: str) -> None:
    import json
    from .db import set_settings
    data = _url_map()
    if data.get(song_id) != upstream:
        data[song_id] = upstream
        set_settings({"radio_url_map": json.dumps(dict(list(data.items())[-300:]))})


def is_slow(song_id: str) -> bool:
    url = (music._cache.get(song_id) or {}).get("_url") or _url_map().get(song_id)
    return bool(url) and url in slow_urls()


def slow_urls() -> dict[str, float]:
    import json
    try:
        data = json.loads(get_settings().get("radio_slow") or "{}")
    except ValueError:
        return {}
    week = time.time() - 7 * 86400
    return {u: t for u, t in data.items() if t > week}


def mark_slow(upstream: str) -> None:
    import json
    from .db import set_settings
    data = slow_urls()
    if upstream not in data:
        data[upstream] = time.time()
        set_settings({"radio_slow": json.dumps(data)})


def station_view(s: dict) -> dict:
    """Bitrate and warnings for the radio page."""
    item = music._cache.get(s["id"]) or {}
    br, cc = item.get("_br") or 0, item.get("_cc") or ""
    note = ""
    if is_slow(s["id"]):
        note = "最近卡过"
    elif cc and cc != "CN" and br > HEAVY_KBPS:
        note = "海外高码率，可能卡顿"
    return {"bitrate": br, "overseas": bool(cc) and cc != "CN", "note": note}


def recommended(limit: int = 12) -> list[dict]:
    """Favourites and well-liked history first, then the curated list."""
    src = radio_source()
    if not src:
        return []
    score = taste()
    known = [i for i, v in sorted(score.items(), key=lambda kv: -(kv[1] - stutter_risk(kv[0]))) if v > 0]
    names = {h["station_id"]: h for h in history(200)} | {f["station_id"]: f for f in favourites()}
    out = []
    for i in known:
        if not i.startswith(f"{src['id']}:"):
            continue
        st = music.song(i)
        if i in names and st["name"] == i.split(":", 1)[-1]:  # not in memory: use the saved name
            st = st | {"name": names[i]["name"], "album": names[i]["tags"], "artists": ["网络电台"]}
        out.append(st)
    for i, (name, url, tags) in enumerate(music.CURATED):
        sid = f"{src['id']}:c{i}"
        if sid not in known:
            out.append(music._song(src, f"c{i}", name, ["精选电台"], tags.split()[0], None, url))
    return out[:limit]


def next_after(song_id: str, limit: int = 10) -> list[dict]:
    """What "换一台" moves to: liked stations first, never the same one, and
    never one whose link stuttered lately (it would start with a long silence)."""
    favs = {f["station_id"] for f in favourites()}
    return [s for s in recommended(limit + 6)
            if s["id"] != song_id and (s["id"] in favs or not is_slow(s["id"]))][:limit]


# ---------------------------------------------------------- stream relay

def relay_url(song_id: str, upstream: str, console_url: str) -> str:
    """Far-away (non-HLS) streams go through the console, which buffers a
    little before playing, so a slow overseas link stutters far less."""
    if upstream:
        _remember_url(song_id, upstream)
    if not upstream or ".m3u8" in upstream or "cnr.cn" in upstream or "qtfm" in upstream or "qingting" in upstream:
        return upstream  # domestic / HLS: direct is better
    token = get_settings().get("device_token", "")
    sig = hmac.new(token.encode(), upstream.encode(), hashlib.sha256).hexdigest()[:20]
    return f"{console_url.rstrip('/')}/api/radio/relay/{sig}?{urlencode({'u': upstream})}"


def check_relay(sig: str, upstream: str) -> bool:
    token = get_settings().get("device_token", "")
    want = hmac.new(token.encode(), upstream.encode(), hashlib.sha256).hexdigest()[:20]
    return hmac.compare_digest(want, sig)


async def relay(upstream: str):
    """Yields the station's audio almost at once (about a second's worth is
    held back so the player gets a clean start; it buffers on its own).
    Measures how fast the station really arrives: a link slower than the
    stream's bitrate can only stutter, so the station is marked and ranked
    lower next time."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(20, read=60), follow_redirects=True) as client:
        async with client.stream("GET", upstream, headers={"User-Agent": "xiaomi-speaker/1.0", "Icy-MetaData": "0"}) as resp:
            try:
                kbps = int(resp.headers.get("icy-br", "0").split(",")[0] or 0)
            except ValueError:
                kbps = 0
            if not kbps:  # the directory's bitrate for this stream, if known
                kbps = next((i.get("_br") or 0 for i in music._cache.values() if i.get("_url") == upstream), 0)
            need = kbps * 125 if kbps else 16_000  # bytes per second
            buffered, first = 0, []
            started, total, checked = time.monotonic(), 0, False
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if kbps and not checked and time.monotonic() - started > 45:
                    checked = True
                    if total / (time.monotonic() - started) < need * 0.95:
                        mark_slow(upstream)
                if first is not None:
                    first.append(chunk)
                    buffered += len(chunk)
                    if buffered < min(48_000, max(16_000, need)):
                        continue
                    for c in first:
                        yield c
                    first = None
                    continue
                yield chunk
            for c in first or []:
                yield c
