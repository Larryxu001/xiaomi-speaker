"""Outbound calls to model and search providers.

Everything the console says to DeepSeek, OpenAI, Claude, SiliconFlow, Tavily
or Firecrawl goes through here, so the config page's "获取模型列表" and
"测试连接" buttons and the scheduled news tasks share one implementation.
"""

from __future__ import annotations

import io
import logging
import math
import struct
import time
import wave

import httpx

log = logging.getLogger("console.providers")

_TIMEOUT = httpx.Timeout(20.0, connect=8.0)


class ProviderError(Exception):
    """A failure worth showing to the user as-is (already in Chinese)."""


def _explain(resp: httpx.Response) -> str:
    code = resp.status_code
    if code in (401, 403):
        return f"API Key 无效或没有权限（HTTP {code}）。"
    if code == 404:
        return "接口地址不对，服务商没有这个路径（HTTP 404）。检查地址末尾是否需要 /v1。"
    if code == 429:
        return "请求太频繁或额度用完了（HTTP 429）。"
    try:
        detail = resp.json()
        msg = detail.get("error", {}).get("message") if isinstance(detail.get("error"), dict) else detail.get("message") or detail.get("error")
    except Exception:
        msg = resp.text[:160]
    return f"服务商返回错误（HTTP {code}）：{msg or '无详细信息'}"


async def _request(method: str, url: str, **kwargs) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.request(method, url, **kwargs)
    except httpx.ConnectError as exc:
        text = str(exc).lower()
        host = httpx.URL(url).host
        if any(k in text for k in ("name or service not known", "nodename nor servname",
                                   "temporary failure in name resolution", "no address associated",
                                   "name resolution", "getaddrinfo")):
            raise ProviderError(
                f"找不到「{host}」这个域名。它可能只在公司内网或 VPN 里能用，"
                "控制台服务器和音箱都在公网和你家里，访问不到。请换成公网能访问的地址。"
            )
        if "refused" in text:
            raise ProviderError(f"「{host}」拒绝连接。检查端口是否正确、服务是否在运行。")
        raise ProviderError("连不上这个地址。检查地址是否写对，或者服务器能否访问它。")
    except httpx.TimeoutException:
        raise ProviderError("连接超时。服务商响应太慢，或服务器访问不到它。")
    except httpx.HTTPError as exc:
        raise ProviderError(f"网络错误：{exc}")
    if resp.status_code >= 400:
        raise ProviderError(_explain(resp))
    return resp


def _llm_headers(protocol: str, api_key: str) -> dict[str, str]:
    if protocol == "anthropic":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {api_key}"}


def _anthropic_root(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


# ---------------------------------------------------------------- models ----


async def list_models(protocol: str, base_url: str, api_key: str, kind: str = "llm") -> list[str]:
    """Return model ids the key can use. kind="asr" keeps speech models first."""
    if not base_url:
        raise ProviderError("请先填写接口地址。")
    if kind == "asr" and base_url.rstrip("/").endswith("/tools/asr"):
        return ["deepseek-asr"]  # DeepSeek's ASR endpoint has one model and no listing API
    if protocol == "anthropic":
        url = f"{_anthropic_root(base_url)}/v1/models?limit=1000"
    else:
        url = f"{base_url.rstrip('/')}/models"
    resp = await _request("GET", url, headers=_llm_headers(protocol, api_key))
    try:
        data = resp.json().get("data", [])
        ids = sorted({str(m["id"]) for m in data if isinstance(m, dict) and m.get("id")})
    except Exception:
        raise ProviderError("服务商返回的模型列表格式无法识别。")
    if not ids:
        raise ProviderError("服务商没有返回任何模型。")
    if kind == "asr":
        speech = ("whisper", "sensevoice", "asr", "transcri", "paraformer", "speech", "audio", "telespeech")
        hits = [m for m in ids if any(k in m.split('/')[-1].lower() for k in speech)]
        rest = [m for m in ids if m not in hits]
        return hits + rest if hits else ids
    return ids


async def test_llm(protocol: str, base_url: str, api_key: str, model: str) -> str:
    if not model:
        raise ProviderError("请先填写模型。")
    started = time.monotonic()
    if protocol == "anthropic":
        resp = await _request(
            "POST",
            f"{_anthropic_root(base_url)}/v1/messages",
            headers=_llm_headers(protocol, api_key),
            json={"model": model, "max_tokens": 16,
                  "messages": [{"role": "user", "content": "只回复两个字：在的"}]},
        )
        reply = "".join(b.get("text", "") for b in resp.json().get("content", []))
    else:
        resp = await _request(
            "POST",
            f"{base_url.rstrip('/')}/chat/completions",
            headers=_llm_headers(protocol, api_key),
            json={"model": model, "max_tokens": 16,
                  "messages": [{"role": "user", "content": "只回复两个字：在的"}]},
        )
        try:
            reply = resp.json()["choices"][0]["message"]["content"] or ""
        except Exception:
            raise ProviderError("服务商返回了无法识别的回复格式。")
    elapsed = time.monotonic() - started
    return f"连接正常，{elapsed:.1f} 秒内回复了「{reply.strip()[:20]}」。"


def _test_wav() -> bytes:
    """One second of a quiet tone: enough for the endpoint to accept a file."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(
            struct.pack("<h", int(2000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(16000)
        ))
    return buf.getvalue()


async def test_asr(base_url: str, api_key: str, model: str) -> str:
    if not model:
        raise ProviderError("请先填写模型。")
    started = time.monotonic()
    await _request(
        "POST",
        base_url.rstrip('/') if base_url.rstrip('/').endswith(("/tools/asr", "/transcriptions"))
        else f"{base_url.rstrip('/')}/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key}"},
        data={"model": model},
        files={"file": ("test.wav", _test_wav(), "audio/wav")},
    )
    elapsed = time.monotonic() - started
    return f"连接正常，识别一段 1 秒的测试音频用了 {elapsed:.1f} 秒。"


# ---------------------------------------------------------------- search ----


_TIME_RANGES = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}


async def tavily_search(api_key: str, query: str, max_results: int = 5,
                        topic: str = "news", time_range: str = "day") -> list[dict]:
    body = {"api_key": api_key, "query": query, "max_results": max_results,
            "search_depth": "basic", "topic": "news" if topic == "news" else "general"}
    if time_range in _TIME_RANGES:
        body["time_range"] = time_range
    resp = await _request("POST", "https://api.tavily.com/search", json=body)
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "summary": r.get("content", ""),
         "date": r.get("published_date", "")}
        for r in resp.json().get("results", [])[:max_results]
    ]


async def firecrawl_search(api_key: str, query: str, max_results: int = 5,
                           topic: str = "news", time_range: str = "day") -> list[dict]:
    """Firecrawl v2: news results carry a date, web results fill any gap."""
    body = {"query": query, "limit": max_results, "country": "CN",
            "sources": ["news", "web"] if topic == "news" else ["web"]}
    if time_range in _TIME_RANGES:
        body["tbs"] = _TIME_RANGES[time_range]
    resp = await _request(
        "POST",
        "https://api.firecrawl.dev/v2/search",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
    )
    data = resp.json().get("data") or {}
    items = [
        {"title": r.get("title", ""), "url": r.get("url", ""), "summary": r.get("snippet", ""),
         "date": r.get("date", "")}
        for r in data.get("news", [])
    ] + [
        {"title": r.get("title", ""), "url": r.get("url", ""), "summary": r.get("description", ""),
         "date": ""}
        for r in data.get("web", [])
    ]
    return items[:max_results]


SEARCHERS = {"tavily": tavily_search, "firecrawl": firecrawl_search}
KEY_FIELDS = {"tavily": "web_search_api_key", "firecrawl": "firecrawl_api_key"}
LIVE_ORDER = ("tavily", "firecrawl")  # fastest first for spoken answers


def live_provider(settings: dict[str, str]) -> str | None:
    """The provider chosen in settings (or by voice) if it has a key,
    otherwise the first one that does."""
    if settings.get("web_search_enabled") != "1":
        return None
    chosen = settings.get("search_provider")
    if chosen in KEY_FIELDS and settings.get(KEY_FIELDS[chosen]):
        return chosen
    return next((p for p in LIVE_ORDER if settings.get(KEY_FIELDS[p])), None)


def search_ready(settings: dict[str, str]) -> bool:
    """Web search is on and at least one provider has a key."""
    return live_provider(settings) is not None


async def web_search(settings: dict[str, str], query: str, max_results: int = 5,
                     topic: str = "news", time_range: str = "day", live: bool = False) -> list[dict]:
    """Uses the chosen provider, falling back to any configured one."""
    if not search_ready(settings):
        return []
    provider = live_provider(settings)
    return await SEARCHERS[provider](settings[KEY_FIELDS[provider]], query, max_results, topic, time_range)


async def test_search(provider: str, api_key: str) -> str:
    if not api_key:
        raise ProviderError("请先填写 API Key。")
    started = time.monotonic()
    search = SEARCHERS.get(provider, tavily_search)
    results = await search(api_key, "今日新闻", 3)
    elapsed = time.monotonic() - started
    if not results:
        return f"连接正常，但这次没搜到结果（{elapsed:.1f} 秒）。"
    return f"连接正常，{elapsed:.1f} 秒搜到 {len(results)} 条，例如「{results[0]['title'][:24]}」。"
