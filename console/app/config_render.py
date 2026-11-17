"""Render the speaker's agent.yaml from console settings.

This mirrors xiaoai-agent/agent.example.yaml exactly for everything the
console does not yet manage (device commands, capture tuning, mcp, music,
airplay), so a console-generated config behaves exactly like the
hand-written one that was already verified on the speaker. Only the ASR and
LLM provider fields, plus optional web search, are filled in from settings.
"""

from __future__ import annotations

import json

import yaml

from . import hotwords, shortcuts

from .providers import search_ready
from .services import list_mcp_servers


def _yaml_str(value: str) -> str:
    """Render a Python string as a safe single-line YAML scalar."""
    return yaml.safe_dump(value, allow_unicode=True, default_style='"').strip()


_TEMPLATE = """\
runtime:
  speech_socket: /tmp/mico_aivs_lab/usock/speech.usock
  session_idle_timeout_s: @@LISTEN_SECONDS@@
  barge_in: @@BARGE_IN@@
  barge_in_boost: 3.0

device:
  tts_command: "if [ -x /data/open-xiaoai/say.sh ]; then /data/open-xiaoai/say.sh {text}; else /usr/sbin/tts_play.sh --notify-pns {text}; fi"
  # Music and speech both use miplayer; they are kept apart so one never
  # stops the other. Music: its pid is in /tmp/xiaoai-music.pid. Speech: the
  # miplayer whose parent is say.sh / tts_play.sh. Never "mphelper play": that
  # resumes the firmware's own media player (old native news/music).
  play_url_command: "p=$(cat /tmp/xiaoai-music.pid 2>/dev/null); [ -n \\"$p\\" ] && kill -9 $p 2>/dev/null; miplayer -f {url} >/tmp/xiaoai-miplayer.log 2>&1 & echo $! > /tmp/xiaoai-music.pid"
  stop_audio_command: "p=$(cat /tmp/xiaoai-music.pid 2>/dev/null); [ -n \\"$p\\" ] && kill -9 $p 2>/dev/null; rm -f /tmp/xiaoai-music.pid; mphelper pause >/dev/null 2>&1; true"
  pause_audio_command: "p=$(cat /tmp/xiaoai-music.pid 2>/dev/null); [ -n \\"$p\\" ] && kill -STOP $p 2>/dev/null; true"
  resume_audio_command: "p=$(cat /tmp/xiaoai-music.pid 2>/dev/null); [ -n \\"$p\\" ] && kill -CONT $p 2>/dev/null; true"
  duck_audio_command: "amixer sget mysoftvol | awk -F'[][]' '/%/ {print $2; exit}' | tr -d '%' >/tmp/xiaoai-mysoftvol.before; amixer sset mysoftvol 25% >/dev/null 2>&1 || true"
  unduck_audio_command: "v=$(cat /tmp/xiaoai-mysoftvol.before 2>/dev/null); if [ -n \\"$v\\" ]; then amixer sset mysoftvol \\"${v}%\\" >/dev/null 2>&1 || true; fi; rm -f /tmp/xiaoai-mysoftvol.before"
  abort_command: "for p in $(pidof miplayer 2>/dev/null); do pp=$(awk '{print $4}' /proc/$p/stat 2>/dev/null); case \\"$(cat /proc/$pp/comm 2>/dev/null)\\" in say.sh|tts_play.sh) kill -9 $p 2>/dev/null;; esac; grep -q api/device/tts /proc/$p/cmdline 2>/dev/null && kill -9 $p 2>/dev/null; done; killall say.sh tts_play.sh 2>/dev/null; if [ -f /tmp/xiaoai-music.held ]; then rm -f /tmp/xiaoai-music.held; m=$(cat /tmp/xiaoai-music.pid 2>/dev/null); [ -n \\"$m\\" ] && kill -CONT $m 2>/dev/null; fi; ubus call mediaplayer media_control '{\\"player\\":\\"mediaplayer\\",\\"action\\":\\"pause\\",\\"volume\\":0}' >/dev/null 2>&1; true"
  heard_sound_command: ""   # no "di" after the user stops talking (too noisy)
  led_enabled: true
  led_listening: 3
  led_user_speaking: 4
  led_thinking: 2
  led_speaking: 5

capture:
  sample_rate: 16000
  threshold: 0.006
  mic_gain: 1.0
  block_ms: 100
  pre_roll_ms: 300
  silence_ms: 650
  min_speech_ms: 300
  max_utterance_s: 15.0
  cooldown_s: 0.7

asr:
  provider: open_ai
  fallbacks: @@ASR_FALLBACKS@@
  open_ai:
    base_url: @@ASR_BASE_URL@@
    api_key: @@ASR_API_KEY@@
    model: @@ASR_MODEL@@
    language: zh
    prompt: @@ASR_PROMPT@@
    timeout_s: @@ASR_TIMEOUT@@
    retries: @@ASR_RETRIES@@
  openai_realtime:
    base_url: https://api.openai.com/v1
    api_key: EMPTY
    model: gpt-realtime-whisper
    target_sample_rate: 16000
    chunk_ms: 200
    timeout_s: 10
    retries: 1
    server_vad:
      enabled: true
      prefix_padding_ms: 300
      silence_duration_ms: 1200
      threshold: 0.5

llm:
  protocol: @@LLM_PROTOCOL@@
  base_url: @@LLM_BASE_URL@@
  api_key: @@LLM_API_KEY@@
  model: @@LLM_MODEL@@
  timeout_s: 15
  max_tokens: @@LLM_MAX_TOKENS@@
  temperature: @@LLM_TEMPERATURE@@
  thinking:
    mode: disabled
    budget_tokens: 1024

llm_fallbacks: @@LLM_FALLBACKS@@

agent:
  timezone: @@TIMEZONE@@
  profile_notes: @@PROFILE_NOTES@@
  shortcuts: @@SHORTCUTS@@
  weather:
    qweather_url: ""
    default_location: ""
    ip_lookup_url: https://ipapi.co/json/
    timeout_s: 5
  web_search:
    enabled: @@WEB_SEARCH_ENABLED@@
    tavily_url: @@WEB_SEARCH_URL@@
    api_key: @@WEB_SEARCH_API_KEY@@
    timeout_s: 10
    max_results: 3
    search_depth: basic

@@MCP_SECTION@@

music:
  enabled: @@MUSIC_ENABLED@@
  provider: console
  console:
    base_url: @@CONSOLE_URL@@
    token: @@DEVICE_TOKEN@@
    timeout_s: 10
  interruption:
    mode: pause
  netease:
    api_base_url: http://127.0.0.1:3300
    login_mode: captcha
    account: ""
    phone: ""
    password: ""
    md5_password: ""
    cookie_file: null
    default_level: standard
    timeout_s: 5
    login_on_start: true
  navidrome:
    base_url: http://127.0.0.1:4533
    username: ""
    password: ""
    api_version: 1.16.1
    timeout_s: 5

airplay:
  enabled: false
  name: XiaoAI AirPlay
  port: 5000
  password: ""
  hwaddr: ""
  output:
    backend: aplay
    aplay_path: /usr/bin/aplay
    device: default
    format: S16_LE
  interruption:
    mode: duck
    duck_gain: 0.25
"""


def _mcp_section(settings: dict[str, str], console_url: str) -> str:
    """The console's own tools first, then every enabled external server."""
    servers = []
    if console_url and settings.get("device_token"):
        servers.append({"name": "console", "enabled": True, "url": f"{console_url.rstrip('/')}/mcp/",
                        "token": settings["device_token"], "timeout_s": 40, "tools": []})
    for srv in list_mcp_servers():
        if not srv["enabled"]:
            continue
        servers.append({"name": srv["name"], "enabled": True, "url": srv["url"], "token": srv["token"],
                        "timeout_s": float(srv["timeout_s"]),
                        "tools": [t for t in srv["tools"].split(",") if t]})
    return yaml.safe_dump({"mcp": {"servers": servers}}, allow_unicode=True,
                          sort_keys=False, default_flow_style=False).rstrip("\n")


def asr_fallbacks(settings: dict[str, str]) -> list[dict]:
    """Every other saved speech-recognition profile, tried in turn when the
    chosen one times out (no separate "backup" setting to manage)."""
    from .db import get_conn
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM asr_profiles ORDER BY id").fetchall()]
    current = (settings.get("asr_base_url", "").rstrip("/"), settings.get("asr_model", ""))
    return [{"base_url": r["base_url"], "api_key": r.get("api_key") or "EMPTY", "model": r["model"],
             "language": "zh", "prompt": "", "timeout_s": 6, "retries": 0}
            for r in rows if r.get("base_url") and r.get("model")
            and (r["base_url"].rstrip("/"), r["model"]) != current]


def llm_fallbacks(settings: dict[str, str]) -> list[dict]:
    from .db import get_conn
    ids = [int(x) for x in (settings.get("llm_fallback_profiles") or "").split(",") if x.strip().isdigit()]
    if not ids:
        return []
    with get_conn() as conn:
        rows = {r["id"]: dict(r) for r in conn.execute(
            f"SELECT * FROM model_profiles WHERE id IN ({','.join('?' * len(ids))})", ids)}
    out = []
    for i in ids:
        p = rows.get(i)
        if not p or (p["base_url"] == settings.get("llm_base_url") and p["model"] == settings.get("llm_model")):
            continue  # deleted, or the same as the primary model
        out.append({"protocol": p["protocol"], "base_url": p["base_url"], "api_key": p["api_key"] or "EMPTY",
                    "model": p["model"], "timeout_s": 15,
                    "max_tokens": int(settings.get("llm_max_tokens") or 600),
                    "temperature": float(settings.get("llm_temperature") or 0.5),
                    "thinking": {"mode": "disabled", "budget_tokens": 1024}})
    return out


def render_agent_yaml(settings: dict[str, str], console_url: str = "") -> str:
    # The speaker's live web_search tool talks to the console's
    # Tavily-compatible /api/device/search, which uses whichever provider is
    # configured (Tavily or Firecrawl). The speaker authenticates with its
    # device token, so the real search key stays on the console.
    enabled = search_ready(settings) and bool(console_url)
    web_search_enabled = "true" if enabled else "false"
    search_url = f"{console_url.rstrip('/')}/api/device/search" if enabled else "https://api.tavily.com/search"
    search_key = settings.get("device_token", "") if enabled else ""
    from .music import any_enabled
    replacements = {
        "@@LISTEN_SECONDS@@": str(int(settings.get("listen_seconds") or 15)),
        "@@SHORTCUTS@@": json.dumps([{"say": x["say"], "run": x["run"]} for x in shortcuts.load(settings)],
                                    ensure_ascii=False),
        "@@PROFILE_NOTES@@": _yaml_str(" ".join((settings.get("profile_notes") or "").split())[:600]),
        "@@BARGE_IN@@": "true" if settings.get("barge_in", "1") == "1" else "false",
        "@@ASR_FALLBACKS@@": json.dumps(asr_fallbacks(settings), ensure_ascii=False),
        # With another service to fall back on, give up on a slow one sooner.
        # Recognition normally answers in about a second. With a backup,
        # switch early; with none, still fail fast and ask the user to repeat
        # rather than leaving them waiting in silence for a retry.
        # Words the recogniser should expect, so short commands and the
        # names this household uses are not turned into something similar.
        "@@ASR_PROMPT@@": _yaml_str(hotwords.build(settings)),
        "@@ASR_TIMEOUT@@": "4" if asr_fallbacks(settings) else "6",
        "@@ASR_RETRIES@@": "0",
        "@@LLM_FALLBACKS@@": json.dumps(llm_fallbacks(settings), ensure_ascii=False),
        "@@MUSIC_ENABLED@@": "true" if (console_url and any_enabled()) else "false",
        "@@CONSOLE_URL@@": _yaml_str(console_url.rstrip("/")),
        "@@DEVICE_TOKEN@@": _yaml_str(settings.get("device_token", "")),
        "@@ASR_BASE_URL@@": _yaml_str(settings.get("asr_base_url", "")),
        "@@ASR_API_KEY@@": _yaml_str(settings.get("asr_api_key", "")),
        "@@ASR_MODEL@@": _yaml_str(settings.get("asr_model", "")),
        "@@LLM_PROTOCOL@@": _yaml_str(settings.get("llm_protocol", "open_ai_chat")),
        "@@LLM_BASE_URL@@": _yaml_str(settings.get("llm_base_url", "")),
        "@@LLM_API_KEY@@": _yaml_str(settings.get("llm_api_key", "")),
        "@@LLM_MODEL@@": _yaml_str(settings.get("llm_model", "")),
        "@@LLM_MAX_TOKENS@@": str(int(settings.get("llm_max_tokens") or 600)),
        "@@LLM_TEMPERATURE@@": str(float(settings.get("llm_temperature") or 0.5)),
        "@@TIMEZONE@@": _yaml_str(settings.get("timezone", "Asia/Shanghai")),
        "@@WEB_SEARCH_ENABLED@@": web_search_enabled,
        "@@WEB_SEARCH_API_KEY@@": _yaml_str(search_key),
        "@@WEB_SEARCH_URL@@": _yaml_str(search_url),
        "@@MCP_SECTION@@": _mcp_section(settings, console_url),
    }
    text = _TEMPLATE
    for token, value in replacements.items():
        text = text.replace(token, value)
    return text
