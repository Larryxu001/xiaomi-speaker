"""Voices for the speaker.

The speaker's say.sh asks the console to synthesize each reply and streams
the MP3 back with miplayer; "native" keeps XiaoAI's own cloud voice. Audio is
streamed chunk by chunk so playback starts before synthesis finishes.
"""

from __future__ import annotations

import secrets
import time
from typing import AsyncIterator

import httpx

from .db import get_settings

NATIVE = "native"

# (group, [(voice id, label)]). Edge voices need no key; CosyVoice uses the
# SiliconFlow key already entered for speech recognition.
VOICES = [
    ("小爱原声", [(NATIVE, "小爱同学（原生，最快）")]),
    ("普通话 · 微软（免费）", [
        ("edge:zh-CN-XiaoxiaoNeural", "晓晓 · 温暖女声"),
        ("edge:zh-CN-XiaoyiNeural", "晓伊 · 活泼女声"),
        ("edge:zh-CN-YunxiNeural", "云希 · 阳光男声"),
        ("edge:zh-CN-YunjianNeural", "云健 · 激昂男声"),
        ("edge:zh-CN-YunyangNeural", "云扬 · 新闻播音男声"),
        ("edge:zh-CN-YunxiaNeural", "云夏 · 少年男声"),
    ]),
    ("方言与港台 · 微软（免费）", [
        ("edge:zh-CN-liaoning-XiaobeiNeural", "晓北 · 东北话"),
        ("edge:zh-CN-shaanxi-XiaoniNeural", "晓妮 · 陕西话"),
        ("edge:zh-HK-HiuMaanNeural", "曉曼 · 粤语女声"),
        ("edge:zh-HK-WanLungNeural", "雲龍 · 粤语男声"),
        ("edge:zh-TW-HsiaoChenNeural", "曉臻 · 台湾女声"),
        ("edge:zh-TW-YunJheNeural", "雲哲 · 台湾男声"),
    ]),
    ("DeepSeek（1.5 元/万字，不能调语速）", [
        ("ds:stella", "海星 · DeepSeek 女声"), ("ds:tide", "暗潮 · DeepSeek 男声"),
    ]),
    ("CosyVoice · 硅基流动（更像真人）", [
        ("sf:anna", "Anna · 沉稳女声"), ("sf:bella", "Bella · 热情女声"),
        ("sf:claire", "Claire · 温柔女声"), ("sf:diana", "Diana · 欢快女声"),
        ("sf:alex", "Alex · 沉稳男声"), ("sf:benjamin", "Benjamin · 低沉男声"),
        ("sf:charles", "Charles · 磁性男声"), ("sf:david", "David · 欢快男声"),
    ]),
]
LABELS = {vid: label for _, items in VOICES for vid, label in items}
RATES = {"slow": "-15%", "normal": "+0%", "fast": "+15%"}
SF_MODEL = "FunAudioLLM/CosyVoice2-0.5B"

_pending: dict[str, tuple[float, str, str]] = {}
_TTL_S = 120


class TtsError(Exception):
    pass


def find_voice(query: str) -> str | None:
    q = (query or "").strip().lower()
    if not q:
        return None
    if q in LABELS:
        return q
    for vid, label in LABELS.items():
        name = label.split(" · ")[0].lower()
        if q == name or name in q or q in label.lower():
            return vid
    return None


def ticket(text: str, voice: str) -> str:
    now = time.time()
    for k in [k for k, (t, _, _) in _pending.items() if now - t > _TTL_S]:
        _pending.pop(k, None)
    tid = secrets.token_urlsafe(18)
    _pending[tid] = (now, text, voice)
    return tid


def redeem(tid: str) -> tuple[str, str] | None:
    item = _pending.get(tid)
    if not item or time.time() - item[0] > _TTL_S:
        return None
    return item[1], item[2]


def _ds_key(settings: dict) -> str:
    """The DeepSeek key from the chat model or speech recognition settings."""
    for base, key in (("llm_base_url", "llm_api_key"), ("asr_base_url", "asr_api_key")):
        if "deepseek.com" in settings.get(base, "") and settings.get(key, "").strip():
            return settings[key].strip()
    return _profile_key("deepseek.com")


def media_type(voice: str) -> str:
    return "audio/wav" if voice.startswith("ds:") else "audio/mpeg"


def _profile_key(host: str) -> str:
    """A key for `host` from the saved chat / speech recognition profiles."""
    from .db import get_conn
    with get_conn() as conn:
        for table in ("asr_profiles", "model_profiles"):
            for row in conn.execute(f"SELECT base_url, api_key FROM {table} ORDER BY id"):
                if host in row["base_url"] and row["api_key"].strip():
                    return row["api_key"].strip()
    return ""


def _sf_key(settings: dict) -> str:
    key = settings.get("tts_api_key", "").strip()
    if not key and "siliconflow" in settings.get("asr_base_url", ""):
        key = settings.get("asr_api_key", "").strip()
    return key or _profile_key("siliconflow")


def voice_problem(voice: str, settings: dict | None = None) -> str:
    """Why `voice` can't be used right now ("" when it can)."""
    settings = settings or get_settings()
    if voice.startswith("sf:") and not _sf_key(settings):
        return "CosyVoice 音色需要硅基流动的 Key，请先在「模型与密钥」里存一个硅基流动的档案。"
    if voice.startswith("ds:") and not _ds_key(settings):
        return "DeepSeek 音色需要 DeepSeek 的 Key。"
    return ""


async def stream(text: str, voice: str, settings: dict | None = None) -> AsyncIterator[bytes]:
    settings = settings or get_settings()
    rate = RATES.get(settings.get("tts_rate", "normal"), "+0%")
    if voice.startswith("edge:"):
        import edge_tts
        comm = edge_tts.Communicate(text, voice[5:], rate=rate)
        try:
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    yield chunk["data"]
        except Exception as exc:
            raise TtsError(f"微软语音合成失败：{exc.__class__.__name__}")
        return
    if voice.startswith("sf:"):
        key = _sf_key(settings)
        if not key:
            raise TtsError("CosyVoice 需要硅基流动的 Key（和语音识别共用）。")
        speed = {"slow": 0.85, "fast": 1.15}.get(settings.get("tts_rate", "normal"), 1.0)
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5)) as client:
            async with client.stream("POST", "https://api.siliconflow.cn/v1/audio/speech",
                                     headers={"Authorization": f"Bearer {key}"},
                                     json={"model": SF_MODEL, "input": text, "voice": f"{SF_MODEL}:{voice[3:]}",
                                           "response_format": "mp3", "stream": True, "speed": speed}) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread())[:200].decode(errors="replace")
                    raise TtsError(f"CosyVoice 合成失败：HTTP {resp.status_code} {body}")
                async for chunk in resp.aiter_bytes():
                    yield chunk
        return
    if voice.startswith("ds:"):
        key = _ds_key(settings)
        if not key:
            raise TtsError("DeepSeek 音色需要 DeepSeek 的 Key（和对话模型共用）。")
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5)) as client:
            async with client.stream("POST", "https://api.deepseek.com/v1/tools/tts",
                                     headers={"Authorization": f"Bearer {key}"},
                                     json={"text": text, "voice_id": voice[3:], "format": "wav"}) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread())[:200].decode(errors="replace")
                    raise TtsError(f"DeepSeek 合成失败：HTTP {resp.status_code} {body}")
                async for chunk in resp.aiter_bytes():
                    yield chunk
        return
    raise TtsError("不认识这个音色。")


async def synthesize(text: str, voice: str, settings: dict | None = None) -> bytes:
    return b"".join([c async for c in stream(text, voice, settings)])


# ---------------------------------------------------------- streamed replies
# The speaker sends a reply sentence by sentence while the model is still
# writing it; one MP3 stream plays them back to back without gaps.

import asyncio

_streams: dict[str, tuple[float, str, asyncio.Queue]] = {}
_END = None


def can_stream(voice: str) -> bool:
    """MP3 voices can be concatenated into one stream; WAV ones can't."""
    return voice.startswith(("edge:", "sf:"))


def open_stream(voice: str) -> str:
    now = time.time()
    for k in [k for k, (t, _, _) in _streams.items() if now - t > 600]:
        _streams.pop(k, None)
    sid = secrets.token_urlsafe(18)
    _streams[sid] = (now, voice, asyncio.Queue())
    return sid


def push_text(sid: str, text: str | None) -> bool:
    item = _streams.get(sid)
    if not item:
        return False
    item[2].put_nowait(text)
    return True


async def stream_session(sid: str, settings: dict) -> AsyncIterator[bytes]:
    item = _streams.get(sid)
    if not item:
        return
    _, voice, queue = item
    try:
        while True:
            try:
                text = await asyncio.wait_for(queue.get(), timeout=60)
            except asyncio.TimeoutError:
                return
            if text is _END:
                return
            text = text.strip()
            if not text:
                continue
            try:
                async for chunk in stream(text, voice, settings):
                    yield chunk
            except TtsError:
                continue  # skip a sentence rather than stop the whole reply
    finally:
        _streams.pop(sid, None)
