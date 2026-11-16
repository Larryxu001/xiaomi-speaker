"""Weather without any key: place names are resolved with Open-Meteo's
geocoder, the forecast comes from MET Norway's public API (api.met.no).
Both are cached, so a question about the weather is answered at once.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

UA = "xiaomi-speaker-console/1.0 github.com/Larryxu001/xiaomi-speaker"
_places: dict[str, dict] = {}
_forecasts: dict[tuple[float, float], tuple[float, dict]] = {}
FORECAST_TTL = 30 * 60


class WeatherError(Exception):
    pass


def _text(symbol: str) -> str:
    s = (symbol or "").split("_")[0]
    if "thunder" in s:
        return "雷阵雨"
    if "sleet" in s:
        return "雨夹雪"
    if "snow" in s:
        return "小雪" if s.startswith("light") else "大雪" if s.startswith("heavy") else "雪"
    if "rain" in s:
        size = "小" if s.startswith("light") else "大" if s.startswith("heavy") else ""
        return f"{size}阵雨" if "showers" in s else f"{size}雨" if size else "中雨"
    return {"clearsky": "晴", "fair": "晴间多云", "partlycloudy": "多云", "cloudy": "阴", "fog": "雾"}.get(s, "多云")


async def _place(name: str) -> dict:
    name = name.strip()
    for w in ("市", "天气", "的"):
        name = name[: -len(w)] if name.endswith(w) and len(name) > len(w) + 1 else name
    if name in _places:
        return _places[name]
    async with httpx.AsyncClient(timeout=8, headers={"User-Agent": UA}) as client:
        resp = await client.get("https://geocoding-api.open-meteo.com/v1/search",
                                params={"name": name, "count": 1, "language": "zh"})
    hits = (resp.json() or {}).get("results") or []
    if not hits:
        raise WeatherError(f"没找到「{name}」这个地方")
    h = hits[0]
    label = h.get("name") or name
    if h.get("admin1") and h["admin1"] not in label and label not in h["admin1"]:
        label = f"{h['admin1']}{label}"
    _places[name] = {"lat": round(h["latitude"], 2), "lon": round(h["longitude"], 2), "label": label,
                     "tz": h.get("timezone") or "Asia/Shanghai"}
    return _places[name]


async def _series(lat: float, lon: float) -> list[dict]:
    key = (lat, lon)
    hit = _forecasts.get(key)
    if hit and time.time() - hit[0] < FORECAST_TTL:
        return hit[1]
    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": UA}) as client:
        resp = await client.get("https://api.met.no/weatherapi/locationforecast/2.0/compact",
                                params={"lat": lat, "lon": lon})
        resp.raise_for_status()
    series = resp.json()["properties"]["timeseries"]
    _forecasts[key] = (time.time(), series)
    return series


async def lookup(location: str = "", lat: float | None = None, lon: float | None = None) -> dict:
    """Now plus four days, in plain words for the model to read out."""
    try:
        if location.strip():
            place = await _place(location)
        elif lat is not None and lon is not None:
            place = {"lat": round(lat, 2), "lon": round(lon, 2), "label": "你所在的位置", "tz": "Asia/Shanghai"}
        else:
            raise WeatherError("不知道要查哪里的天气")
        series = await _series(place["lat"], place["lon"])
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise WeatherError(f"天气服务暂时连不上（{exc.__class__.__name__}）") from exc
    tz = ZoneInfo(place["tz"])
    days: dict[str, dict] = {}
    for e in series:
        t = datetime.fromisoformat(e["time"].replace("Z", "+00:00")).astimezone(tz)
        d = days.setdefault(t.strftime("%Y-%m-%d"), {"temps": [], "symbols": [], "rain": 0.0})
        data = e["data"]
        d["temps"].append(data["instant"]["details"]["air_temperature"])
        nxt = data.get("next_1_hours") or data.get("next_6_hours") or {}
        if 7 <= t.hour <= 21 and nxt.get("summary"):
            d["symbols"].append(_text(nxt["summary"]["symbol_code"]))
        d["rain"] += (nxt.get("details") or {}).get("precipitation_amount") or 0.0
    today = datetime.now(tz).date()
    labels = {0: "今天", 1: "明天", 2: "后天"}
    out_days = []
    for key in sorted(days)[:5]:
        d = days[key]
        day = datetime.strptime(key, "%Y-%m-%d").date()
        if day < today or not d["temps"]:
            continue
        common = Counter(d["symbols"]).most_common(2)
        wet = next((s for s, _ in Counter(d["symbols"]).most_common() if "雨" in s or "雪" in s), None)
        text = common[0][0] if common else "多云"
        if wet and wet != text and d["rain"] >= 0.5:
            text = f"{text}，有{wet}"
        out_days.append({"date": key, "label": labels.get((day - today).days, f"{day.month}月{day.day}日"),
                         "text": text, "min": round(min(d["temps"])), "max": round(max(d["temps"])),
                         "rain_mm": round(d["rain"], 1)})
    now = series[0]["data"]
    det = now["instant"]["details"]
    return {
        "location": place["label"],
        "now": {"temp": round(det["air_temperature"]),
                "text": _text(((now.get("next_1_hours") or {}).get("summary") or {}).get("symbol_code", "")),
                "humidity": round(det.get("relative_humidity", 0)), "wind_ms": det.get("wind_speed")},
        "days": out_days,
        "hint": "rain_mm 是当天总降水量（毫米），不到 0.5 就是基本不下雨。只说用户问的那一天，口语化、简短。",
    }
