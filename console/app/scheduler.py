"""Background loop that turns due scheduled tasks into announcements.

Runs inside the console process (one asyncio task, checked every 30s - no
extra scheduler dependency). A task is either:

- "announcement": the stored text is queued verbatim.
- "news_analysis": the console asks the configured LLM (optionally after a
  Tavily web search) to turn the task's topic into a short, speakable
  summary, then queues that.

Queued items are picked up by the speaker's poller (see
deploy/console-poller.sh) and read aloud with the native TTS command.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

import httpx

from .db import get_conn, get_settings, local_tz, now_local
from .providers import ProviderError, _anthropic_root, web_search
from .sources import gather_material, ids_from_text

log = logging.getLogger("console.scheduler")

_CHECK_INTERVAL_S = 30
_WEEKDAY_SET = {0, 1, 2, 3, 4}  # Monday .. Friday



def _is_due(task: dict, now: datetime) -> bool:
    """A task is due once per matching slot; last_run_at guards re-firing."""
    # "Don't make a sound tomorrow morning" is one quiet morning, not the end
    # of the task: skip_until keeps a task enabled and visible while holding
    # it quiet up to and including that day.
    if (task.get("skip_until") or "").strip() >= now.date().isoformat():
        return False
    recurrence = task["recurrence"]
    last_run_at = task["last_run_at"]

    if recurrence == "once":
        try:
            target = datetime.fromisoformat(task["run_at"])
        except ValueError:
            return False
        if target.tzinfo is None:
            target = target.replace(tzinfo=now.tzinfo)
        return now >= target and not last_run_at

    try:
        hh, mm = (int(x) for x in task["run_at"].split(":"))
    except ValueError:
        return False

    if now.hour != hh or now.minute != mm:
        return False
    if recurrence == "weekdays" and now.weekday() not in _WEEKDAY_SET:
        return False
    if recurrence == "weekly" and task["weekday"] is not None and now.weekday() != int(task["weekday"]):
        return False

    if last_run_at:
        try:
            last_run = datetime.fromisoformat(last_run_at)
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=now.tzinfo)
            if now - last_run < timedelta(minutes=1):
                return False  # already fired this exact minute
        except ValueError:
            pass
    return True


async def _search_notes(settings: dict[str, str], query: str) -> str:
    try:
        results = await web_search(settings, query)
    except ProviderError as exc:  # search is enrichment; never block the broadcast
        log.warning("web search failed: %s", exc)
        return ""
    lines = []
    for r in results:
        date = f"（{r['date']}）" if r.get("date") else ""
        lines.append(f"- {r['title']}{date}：{r['summary']}")
    return "\n".join(lines)


async def _llm_complete(settings: dict[str, str], system: str, user: str, max_tokens: int = 400) -> str:
    protocol = settings.get("llm_protocol", "open_ai_chat")
    base_url = settings.get("llm_base_url", "").rstrip("/")
    api_key = settings.get("llm_api_key", "")
    model = settings.get("llm_model", "")
    async with httpx.AsyncClient(timeout=120) as client:
        if protocol == "anthropic":
            resp = await client.post(
                f"{_anthropic_root(base_url)}/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return "".join(block.get("text", "") for block in data.get("content", []))
        extra = {"thinking": {"type": "disabled"}} if "deepseek.com" in base_url else {}
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                **extra,  # DeepSeek otherwise spends the token budget thinking
                "model": model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


# Phrases a model falls back on when the material was unusable. Such a
# section is dropped rather than read out at six in the morning.
_APOLOGY = ("无法播报", "无法还原", "不具备播报价值", "没有内容", "暂无内容", "内容为空",
            "原始数据出现了问题", "乱码", "程序代码", "无法完整播报", "请核对", "建议核对",
            "未提供", "没有提供", "资料不足", "无法生成")


def note_problem(task: dict, reason: str) -> None:
    """Recorded for the console; never spoken."""
    log.warning("task %s skipped: %s", task.get("id"), reason)
    with get_conn() as conn:
        conn.execute("INSERT INTO task_problems (task_id, task_name, reason, created_at) VALUES (?, ?, ?, ?)",
                     (task.get("id"), task.get("name") or "", reason[:300],
                      now_local().isoformat(timespec="seconds")))
        conn.execute("DELETE FROM task_problems WHERE id NOT IN "
                     "(SELECT id FROM task_problems ORDER BY id DESC LIMIT 100)")
        conn.commit()


def _unusable(text: str) -> str:
    """Why this spoken text should not be played, or '' when it is fine."""
    body = " ".join((text or "").split())
    if len(body) < 40:
        return "生成出来的稿子太短"
    hits = [w for w in _APOLOGY if w in body]
    # An apology only counts when it is most of what the section says.
    if hits and (len(body) < 320 or body.count("第") < 3):
        return f"稿子里只有「{hits[0]}」这类说明，没有真正的内容"
    return ""


async def _run_news_analysis(task: dict, settings: dict[str, str]) -> str | None:
    topic = task["payload"] or "今天的重要新闻"
    source_ids = ids_from_text(task.get("source_ids"))
    if source_ids:
        return await _run_source_digest(task, settings, topic, source_ids)
    search_notes = await _search_notes(settings, topic)
    profile = settings.get("profile_notes", "").strip()
    system = (
        "你是一个为智能音箱生成口播稿的助手。"
        "输出必须是适合直接朗读的中文口语，不用 Markdown，不用列表符号，"
        "通常 3 到 5 句话，先说结论再补充细节，不要编造没有依据的具体数字。"
    )
    user_parts = [f"请基于下面的话题给出一段简短的语音播报：{topic}"]
    if search_notes:
        user_parts.append(f"\n参考资料（可能不完整，仅供参考）：\n{search_notes}")
    else:
        user_parts.append("\n（没有联网搜索资料，请基于你已知的信息回答，若不确定要说明。）")
    if profile:
        user_parts.append(f"\n用户的背景和偏好（供你调整语气和关注点，不要逐字读出）：{profile}")
    try:
        text = (await _llm_complete(settings, system, "\n".join(user_parts))).strip()
    except Exception as exc:  # pragma: no cover
        log.error("news analysis generation failed: %s", exc)
        note_problem(task, f"生成播报时出错（{exc.__class__.__name__}）")
        return None
    bad = _unusable(text)
    if bad:
        note_problem(task, bad)
        return None
    return text


# Spoken Chinese runs ~250 characters a minute.
class SectionFailed(str):
    """A briefing section that must not be spoken; carries the reason."""


LENGTHS = {
    "short": ("简短", "1 分钟左右，约 250 字，挑最重要的 3 到 4 条", 700),
    "standard": ("标准", "3 分钟左右，约 750 字，挑最重要的 6 到 8 条", 1600),
    "long": ("详细", "5 分钟左右，约 1300 字，尽量覆盖资料里的重要内容", 2600),
    "full": ("完整", "每个来源前 10 条，按顺序逐条完整播报", 2600),
}


async def _full_section(src: dict, settings: dict[str, str], topic: str, first: bool, last: bool) -> str | None:
    """One source of the 「完整」 briefing: its top 10, in its own order,
    each told in full (what happened + background), nothing merged or cut."""
    from .sources import SourceError, full_items
    try:
        items = await full_items(src, settings, 10)
    except SourceError as exc:
        return SectionFailed(f"{src['name']}：{exc}")
    now = datetime.now(local_tz(settings))
    greeting = ("早上好" if now.hour < 11 else "中午好" if now.hour < 13 else "下午好" if now.hour < 18 else "晚上好")
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. 标题：{it['title']}" + (f"\n   提示：{it['info']}" if it["info"] else "")
                     + (f"\n   正文节选：{it['body']}" if it["body"] else ""))
    opening = (f"开头先问好并点明日期，比如「{greeting}，今天是{now.strftime('%-m月%-d日')}，先来看{src['name']}」。"
               if first else f"开头用一句自然的过渡引出「{src['name']}」，不要再问好。")
    closing = "最后用一句话收尾。" if last else "结尾不要总结，下一个来源会接着播。"
    system = (
        "你是电台新闻主播，为用户的智能音箱写口播稿，稿子会被直接朗读：全部口语，不用 Markdown、列表符号、网址和括号。\n"
        f"这一段只播「{src['name']}」，按资料的顺序把 10 条全部播完，一条都不能跳、不能合并、不能只念标题。"
        "每条用「第一条」「第二条」这样的说法开头，先说标题讲的是什么事，再用两三句话讲清楚来龙去脉、关键数字和为什么值得关注。"
        "有正文节选就依据正文；只有标题和提示时，根据提示和常识说明这是什么事，拿不准的细节不要编。\n"
        + opening + closing
    )
    user = f"用户的要求：{topic}\n\n{src['name']}的前 {len(items)} 条：\n" + "\n".join(lines)
    try:
        text = (await _llm_complete(settings, system, user, max_tokens=2600)).strip()
    except Exception as exc:  # pragma: no cover
        log.error("full briefing section failed: %s", exc)
        return SectionFailed(f"{src['name']}：生成播报时出错（{exc.__class__.__name__}）")
    bad = _unusable(text)
    return SectionFailed(f"{src['name']}：{bad}") if bad else text


async def _run_full_briefing(task: dict, settings: dict[str, str], topic: str, source_ids: list[int]) -> list[str]:
    """Sections in the order the sources were picked, written in parallel.
    A section with nothing usable is left out, not spoken as an apology."""
    import asyncio
    from .sources import list_sources
    by_id = {s["id"]: s for s in list_sources()}
    srcs = [by_id[i] for i in source_ids if i in by_id]
    if not srcs:
        note_problem(task, "这个任务的来源都被删除了")
        return []
    out = []
    for section in await asyncio.gather(*(
            _full_section(src, settings, topic, i == 0, i == len(srcs) - 1) for i, src in enumerate(srcs))):
        if isinstance(section, SectionFailed):
            note_problem(task, str(section))
        elif section:
            out.append(section)
    return out


async def _run_source_digest(task: dict, settings: dict[str, str], topic: str, source_ids: list[int]) -> str | None:
    material, failures = await gather_material(source_ids, settings)
    for f in failures:
        log.warning("source fetch failed for task %s: %s", task["id"], f)
    if not material:
        note_problem(task, "没有取到内容：" + ("；".join(failures) or "来源已被删除"))
        return None
    _, length_rule, max_tokens = LENGTHS.get(task.get("length") or "standard", LENGTHS["standard"])
    profile = settings.get("profile_notes", "").strip()
    now = datetime.now(local_tz(settings))
    today = now.strftime("%-m月%-d日")
    greeting = ("早上好" if now.hour < 11 else "中午好" if now.hour < 13
                else "下午好" if now.hour < 18 else "晚上好")
    system = (
        "你是一位有经验的电台新闻主播，为用户的智能音箱写一段口播稿。稿子会被直接朗读，所以全部用口语，"
        "不用 Markdown、列表符号、编号符号、网址和括号注释。\n"
        f"篇幅：{length_rule}。不要因为资料多就只念标题，也不要为了凑字数说空话。\n"
        "结构：开头一句问好并点明今天的日期和来源，比如「" + greeting + "，来看" + today + "的财经早餐」，问候语按现在的时间用「" + greeting + "」；"
        "然后按主题归类播报（比如宏观政策、市场、公司、国际），用自然的过渡语连接；"
        "每条讲清楚发生了什么，再用一句话说它为什么重要或有什么影响；数字要说清单位；"
        "最后用一两句话收尾总结。\n"
        "准确性：事实和数字只能来自资料。资料只有标题时（比如热搜），可以根据常识用一句话解释这是什么事，"
        "拿不准就只说标题，不要编造细节。"
    )
    user = f"播报要求：{topic}\n\n资料：\n{material}"
    if failures:
        user += "\n\n（以下来源这次没取到，请在结尾用一句话说明：" + "；".join(f.split("：")[0] for f in failures) + "）"
    if profile:
        user += f"\n\n用户的背景和偏好（用来决定取舍和侧重，不要读出来）：{profile}"
    try:
        text = (await _llm_complete(settings, system, user, max_tokens=max_tokens)).strip()
    except Exception as exc:  # pragma: no cover
        log.error("source digest generation failed: %s", exc)
        note_problem(task, f"生成播报时出错（{exc.__class__.__name__}）")
        return None
    bad = _unusable(text)
    if bad:
        note_problem(task, bad)
        return None
    return text


async def queue_task_now(task: dict, settings: dict[str, str]) -> str:
    """Generate the task's text and queue it for the speaker.

    Used by the scheduler and by the "立即试播" button. It does not touch
    last_run_at, so testing a task never suppresses its real schedule.
    """
    from .sources import ids_from_text
    ids = ids_from_text(task.get("source_ids"))
    if task["kind"] == "news_analysis" and task.get("length") == "full" and ids:
        # One announcement per source, so a long briefing plays as a series.
        texts = await _run_full_briefing(task, settings, task["payload"], ids)
    elif task["kind"] == "news_analysis":
        texts = [await _run_news_analysis(task, settings)]
    else:
        texts = [task["payload"]]
    # Nothing usable: stay silent and let the console show why.
    texts = [" ".join(t.split()) for t in texts if t and t.strip()]  # one TTS line each
    if not texts:
        return ""
    with get_conn() as conn:
        for t in texts:
            conn.execute("INSERT INTO announcements (task_id, text) VALUES (?, ?)", (task["id"], t))
        conn.commit()
    text = "\n\n".join(texts)
    log.info("task %s (%s) queued announcement: %s", task["id"], task["kind"], text[:60])
    return text


async def _fire_task(task: dict, settings: dict[str, str], now: datetime) -> None:
    await queue_task_now(task, settings)
    with get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET last_run_at = ? WHERE id = ?",
            (now.isoformat(), task["id"]),
        )
        conn.commit()


_radio_warmed = [0.0]


_SELF_CHECK_AT = (7, 30)   # local time, after the morning briefings have run
_self_checked_on = [""]


async def _daily_self_check(now: datetime) -> None:
    """Once a day, say out loud anything that is newly broken.

    A problem shown only on a web page is a problem nobody sees: three daily
    briefings sat disabled for five mornings before anyone noticed.
    """
    today = now.date().isoformat()
    if _self_checked_on[0] == today or (now.hour, now.minute) < _SELF_CHECK_AT:
        return
    _self_checked_on[0] = today
    try:
        from . import health
        line = health.announce_new_problems()
        if line:
            log.warning("self-check: %s", line)
    except Exception:
        log.exception("self-check failed")


async def run_scheduler_forever() -> None:
    log.info("scheduler loop started (checking every %ss)", _CHECK_INTERVAL_S)
    while True:
        try:
            settings = get_settings()
            now = datetime.now(local_tz(settings))
            with get_conn() as conn:
                tasks = [dict(row) for row in conn.execute(
                    "SELECT * FROM tasks WHERE enabled = 1"
                ).fetchall()]
            from .memory import learn_pending
            await learn_pending()
            from .logins import check_all
            await check_all()
            from .ideas import maybe_think
            await maybe_think()
            from . import radio as _radio
            if time.time() - _radio_warmed[0] > _radio.CACHE_S - 600:
                _radio_warmed[0] = time.time()
                asyncio.create_task(_radio.prewarm())
            await _daily_self_check(now)
            for task in tasks:
                if _is_due(task, now):
                    await _fire_task(task, settings, now)
                    if task["recurrence"] == "once":
                        with get_conn() as conn:
                            conn.execute(
                                "UPDATE tasks SET enabled = 0 WHERE id = ?", (task["id"],)
                            )
                            conn.commit()
        except Exception:  # pragma: no cover - never let the loop die
            log.exception("scheduler tick failed")
        await asyncio.sleep(_CHECK_INTERVAL_S)
