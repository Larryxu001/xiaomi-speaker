"""Content sources that scheduled tasks read from.

A task can name the sources it should read ("微博热搜", "万得财经早餐") so the
broadcast is built from exactly those, not from a random web search.

Kinds:
- newsnow: a NewsNow platform id (https://github.com/ourongxing/newsnow),
  fetched from the NewsNow API (public instance by default, or your own).
- rss:     any RSS / Atom feed URL (RSSHub routes, blogs, WeWe-RSS for
  WeChat official accounts...).
- web:     a web page. With `follow` set, the page is treated as a list: the
  article whose title contains `follow` (e.g. "财经早餐") is opened and its
  text is used. Without it, the page's own text is used.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urljoin

import httpx

from .db import get_conn, get_settings, local_tz

KINDS = {"newsnow": "NewsNow 热榜", "rsshub": "RSSHub", "rss": "RSS 订阅", "web": "网页"}

# RSSHub routes verified on this server's RSSHub (2026-11-15). Any other
# route from https://docs.rsshub.app can be typed in by hand.
RSSHUB_ROUTES = [
    ("财经", [("/cls/telegraph", "财联社电报"), ("/cls/depth/1000", "财联社头条"), ("/wallstreetcn/live", "华尔街见闻快讯"),
            ("/jin10", "金十数据"), ("/gelonghui/live", "格隆汇快讯"), ("/yicai/brief", "第一财经简报"),
            ("/caixin/latest", "财新最新"), ("/eastmoney/report/strategyreport", "东方财富策略研报")]),
    ("科技", [("/36kr/newsflashes", "36 氪快讯"), ("/ithome/ranking/24h", "IT 之家 24 小时热榜"), ("/huxiu/article", "虎嗅"),
            ("/sspai/index", "少数派")]),
    ("综合", [("/zhihu/hot", "知乎热榜"), ("/thepaper/featured", "澎湃精选"), ("/bilibili/hot-search", "B 站热搜"),
            ("/infzm/2", "南方周末")]),
]
RSSHUB_NAMES = {r: n for _, items in RSSHUB_ROUTES for r, n in items}
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15"
MAX_BYTES = 3_000_000
MAX_CHARS = 6000

# NewsNow platform ids verified against the public API on 2026-11-15.
NEWSNOW_PLATFORMS = [
    ("综合热榜", [("weibo", "微博热搜"), ("zhihu", "知乎热榜"), ("baidu", "百度热搜"), ("douyin", "抖音热榜"),
                ("bilibili-hot-search", "B 站热搜"), ("toutiao", "今日头条"), ("tieba", "百度贴吧"), ("hupu", "虎扑")]),
    ("新闻", [("thepaper", "澎湃新闻"), ("ifeng", "凤凰网"), ("zaobao", "联合早报"), ("cankaoxiaoxi", "参考消息"),
            ("kaopu", "靠谱新闻")]),
    ("财经", [("cls-telegraph", "财联社电报"), ("cls-depth", "财联社深度"), ("wallstreetcn-quick", "华尔街见闻快讯"),
            ("wallstreetcn-hot", "华尔街见闻最热"), ("jin10", "金十数据"), ("gelonghui", "格隆汇"),
            ("xueqiu-hotstock", "雪球热股"), ("fastbull-express", "法布财经快讯")]),
    ("科技", [("ithome", "IT 之家"), ("sspai", "少数派"), ("juejin", "稀土掘金"), ("v2ex-share", "V2EX"),
            ("coolapk", "酷安"), ("solidot", "Solidot"), ("github-trending-today", "GitHub 今日趋势"),
            ("hackernews", "Hacker News"), ("producthunt", "Product Hunt"), ("nowcoder", "牛客")]),
]
NEWSNOW_NAMES = {pid: name for _, items in NEWSNOW_PLATFORMS for pid, name in items}

DEFAULT_SOURCES = [
    ("微博热搜", "newsnow", "weibo", "", 10),
    ("财联社电报", "newsnow", "cls-telegraph", "", 10),
    ("华尔街见闻快讯", "newsnow", "wallstreetcn-quick", "", 10),
    ("万得财经早餐", "web", "https://m.163.com/news/sub/T1452670453540.html", "财经早餐", 1),
    ("AIHot AI 热门资讯", "web", "https://aihot.today/ai-news", "", 10),
]


class SourceError(Exception):
    pass


def seed_defaults() -> None:
    with get_conn() as conn:
        if conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]:
            return
        conn.executemany(
            "INSERT INTO sources (name, kind, target, follow, max_items) VALUES (?, ?, ?, ?, ?)",
            DEFAULT_SOURCES)
        conn.commit()


def list_sources() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM sources ORDER BY id").fetchall()]


def save_source(name: str, kind: str, target: str, follow: str = "", max_items: int = 10,
                source_id: int | None = None) -> int:
    name, target, follow = (name or "").strip(), (target or "").strip(), (follow or "").strip()
    if kind not in KINDS:
        raise SourceError("来源类型只能是 NewsNow 热榜、RSS 订阅或网页。")
    if kind == "newsnow":
        if not re.fullmatch(r"[a-z0-9-]{2,40}", target):
            raise SourceError("NewsNow 平台代号不对，例如 weibo、cls-telegraph。")
        name = name or NEWSNOW_NAMES.get(target, target)
    elif kind == "rsshub":
        target = "/" + target.strip().lstrip("/")
        if not re.fullmatch(r"/[\w\-./:%?=&]{2,200}", target):
            raise SourceError("RSSHub 路由不对，例如 /cls/telegraph。")
        name = name or RSSHUB_NAMES.get(target, target)
    elif not re.match(r"https?://", target):
        raise SourceError("请填写以 http:// 或 https:// 开头的地址。")
    if not name:
        raise SourceError("请给来源起个名字，说话时就用这个名字指定它。")
    max_items = max(1, min(int(max_items or 10), 30))
    with get_conn() as conn:
        if source_id:
            conn.execute("UPDATE sources SET name=?, kind=?, target=?, follow=?, max_items=? WHERE id=?",
                         (name[:40], kind, target[:500], follow[:40], max_items, source_id))
        else:
            cur = conn.execute("INSERT INTO sources (name, kind, target, follow, max_items) VALUES (?, ?, ?, ?, ?)",
                               (name[:40], kind, target[:500], follow[:40], max_items))
            source_id = cur.lastrowid
        conn.commit()
    return source_id


def delete_source(source_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        conn.commit()


def resolve_names(text: str) -> list[int]:
    """Turn "微博热搜，万得" into source ids; unknown names raise SourceError."""
    ids, sources = [], list_sources()
    for part in re.split(r"[,，、;；\s]+|和|还有", text or ""):
        part = part.strip()
        if not part:
            continue
        hit = next((s for s in sources if s["name"] == part), None) or \
            next((s for s in sources if part in s["name"] or s["name"] in part), None)
        if not hit:
            names = "、".join(s["name"] for s in sources)
            raise SourceError(f"没有叫「{part}」的来源。现有来源：{names}。")
        if hit["id"] not in ids:
            ids.append(hit["id"])
    return ids


def ids_from_text(value: str | None) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", value or "")]


# ---------- fetching ----------

def _text_of(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    text = re.sub(r"[ \t\r\f\v ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


async def _get(client: httpx.AsyncClient, url: str) -> str:
    try:
        resp = await client.get(url, headers={"User-Agent": UA}, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise SourceError(f"连不上：{exc.__class__.__name__}")
    if resp.status_code >= 400:
        raise SourceError(f"对方返回 HTTP {resp.status_code}")
    return resp.content[:MAX_BYTES].decode(resp.encoding or "utf-8", errors="replace")


async def _newsnow(client, src, base) -> list[str]:
    try:
        resp = await client.get(f"{base}/api/s", params={"id": src["target"]}, headers={"User-Agent": UA})
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SourceError(f"NewsNow 取不到数据：{exc.__class__.__name__}")
    items = data.get("items") or []
    if not items:
        raise SourceError("NewsNow 这个平台暂时没有内容。")
    out = []
    for it in items[: src["max_items"]]:
        extra = it.get("extra") or {}
        hint = extra.get("hover") or extra.get("info") or ""
        out.append(f"{it.get('title', '').strip()}" + (f"：{str(hint).strip()[:120]}" if hint else ""))
    return out


async def _rss(client, src) -> list[str]:
    body = await _get(client, src["target"])
    try:
        root = ET.fromstring(body.encode("utf-8"))
    except ET.ParseError:
        raise SourceError("这个地址返回的不是 RSS 或 Atom。")
    out = []
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        fields = {c.tag.rsplit("}", 1)[-1]: (c.text or "") for c in item}
        title = fields.get("title", "").strip()
        summary = _text_of(fields.get("description") or fields.get("summary") or fields.get("content") or "")
        out.append(title + (f"：{summary[:200]}" if summary else ""))
        if len(out) >= src["max_items"]:
            break
    if not out:
        raise SourceError("订阅源里没有条目。")
    return out


def _nearest_link(page: str, keyword: str, base_url: str) -> str | None:
    idx = page.find(keyword)
    if idx < 0:
        return None
    window = page[max(0, idx - 4000): idx + 4000]
    best, best_d = None, None
    for m in re.finditer(r"""(?:href=["']|"(?:url|link|href)"\s*:\s*")([^"'<>\s]+)""", window):
        link = m.group(1).replace("\\/", "/")
        if link.startswith("#") or link.startswith("javascript"):
            continue
        d = abs(m.start() - (idx - max(0, idx - 4000)))
        if best_d is None or d < best_d:
            best, best_d = link, d
    return urljoin(base_url, best) if best else None


async def _web(client, src) -> list[str]:
    page = await _get(client, src["target"])
    follow = (src.get("follow") or "").strip()
    if follow:
        link = _nearest_link(page, follow, src["target"])
        if not link:
            raise SourceError(f"页面里找不到标题含「{follow}」的文章。")
        page = await _get(client, link)
    text = _text_of(page)
    if follow and follow in text:
        text = text[text.find(follow):]  # skip site navigation above the title
    if len(text) < 40:
        raise SourceError("页面里几乎没有文字，可能需要登录或由脚本动态加载。")
    return [text[:MAX_CHARS]]


async def rsshub_base(settings: dict) -> str:
    """Where this server's RSSHub app answers (from the installer), or a
    hand-set rsshub_base_url for an RSSHub running elsewhere."""
    manual = (settings.get("rsshub_base_url") or "").strip().rstrip("/")
    if manual:
        return manual
    from . import installer_client
    try:
        apps = await installer_client.list_apps()
    except Exception:
        apps = []
    state = next((a.get("state") or {} for a in apps if a.get("id") == "rsshub"), {})
    if state.get("installed") and state.get("url"):
        return state["url"].rstrip("/")
    raise SourceError("还没有安装 RSSHub。请先在「应用」页安装它。")


async def fetch_source(src: dict, settings: dict | None = None) -> list[str]:
    settings = settings or get_settings()
    base = (settings.get("newsnow_base_url") or "https://newsnow.busiyi.world").rstrip("/")
    async with httpx.AsyncClient(timeout=12) as client:
        if src["kind"] == "newsnow":
            return await _newsnow(client, src, base)
        if src["kind"] == "rss":
            return await _rss(client, src)
        if src["kind"] == "rsshub":
            base = await rsshub_base(settings)
            return await _rss(client, {**src, "target": base + src["target"]})
        return await _web(client, src)


def looks_unreadable(text: str) -> bool:
    """True for anything that is not prose a presenter could read out: binary
    or mojibake, a page of code, or a stub too short to say anything. A
    briefing is skipped rather than read out as an apology."""
    text = (text or "").strip()
    if len(text) < 12:
        return True
    if "\ufffd" in text or any(ord(c) < 9 or 11 <= ord(c) < 32 for c in text):
        return True
    letters = sum(1 for c in text if c.isalnum() or "\u4e00" <= c <= "\u9fff")
    if letters / len(text) < 0.55:
        return True
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    codeish = sum(text.count(c) for c in "{};=<>_/\\|$#*")
    if codeish > len(text) * 0.04 and cjk < len(text) * 0.25:
        return True
    return False


async def full_items(src: dict, settings: dict, n: int = 10) -> list[dict]:
    """Top n items of one source with whatever body text we can get, for
    the 「完整」 briefing: {title, info, body}. Article pages are opened in
    parallel; hot-search entries without an article keep their NewsNow hint."""
    import asyncio
    settings = settings or get_settings()
    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        if src["kind"] == "newsnow":
            base = (settings.get("newsnow_base_url") or "https://newsnow.busiyi.world").rstrip("/")
            try:
                data = (await client.get(f"{base}/api/s", params={"id": src["target"]}, headers={"User-Agent": UA})).json()
            except (httpx.HTTPError, ValueError) as exc:
                raise SourceError(f"NewsNow 取不到数据：{exc.__class__.__name__}")
            raw = data.get("items") or []
            items = [{"title": (it.get("title") or "").strip(),
                      "info": str((it.get("extra") or {}).get("hover") or (it.get("extra") or {}).get("info") or "").strip()[:400],
                      "url": it.get("mobileUrl") or it.get("url") or ""} for it in raw[:n]]
        else:
            items = [{"title": t, "info": "", "url": ""} for t in (await fetch_source({**src, "max_items": n}, settings))[:n]]
        items = [it for it in items if not looks_unreadable(it["title"])]
        if not items:
            raise SourceError("这个来源这次取到的内容没法播（空的，或者是乱码/网页代码）。")

        async def body(it: dict) -> str:
            url = it["url"]
            if not url.startswith("http") or any(h in url for h in ("s.weibo.com", "/search", "douyin.com", "baidu.com/s")):
                return ""
            try:
                text = _text_of(await asyncio.wait_for(_get(client, url), 6))
            except Exception:
                return ""
            # Skip navigation: start near the title if it appears on the page.
            key = it["title"][:8]
            if key and key in text:
                text = text[text.find(key):]
            return text[:900]
        bodies = await asyncio.gather(*(body(it) for it in items))
    for it, b in zip(items, bodies):
        it["body"] = "" if looks_unreadable(b) else b
    # Hot-search entries are just a title: look each up so it can be told
    # properly instead of guessed at (skipped when no search key is set).
    bare = [it for it in items if not it["body"] and len(it["info"]) < 40]
    if bare:
        from .providers import web_search

        async def lookup(it: dict) -> None:
            try:
                hits = await asyncio.wait_for(web_search(settings, it["title"], 2, "news", "week"), 15)
            except Exception:
                return
            it["body"] = " ".join(f"{h.get('title', '')}：{h.get('summary', '')}" for h in hits)[:900]
        await asyncio.gather(*(lookup(it) for it in bare))
    return items


async def gather_material(source_ids: list[int], settings: dict) -> tuple[str, list[str]]:
    """Return (material for the LLM, list of failure notes)."""
    by_id = {s["id"]: s for s in list_sources()}
    parts, failures = [], []
    today = datetime.now(local_tz(settings)).strftime("%Y-%m-%d")
    for sid in source_ids:
        src = by_id.get(sid)
        if not src:
            continue
        try:
            items = await fetch_source(src, settings)
        except SourceError as exc:
            failures.append(f"{src['name']}：{exc}")
            continue
        body = "\n".join(f"- {i}" for i in items) if src["kind"] != "web" else items[0]
        parts.append(f"【{src['name']}】（抓取于 {today}）\n{body}")
    return "\n\n".join(parts), failures
