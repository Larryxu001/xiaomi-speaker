"""Daily self-check: notice when the assistant has quietly stopped working.

Everything here answers one question the console could not answer before:
is what the user set up still happening? A week of mornings was lost because
three daily briefings sat disabled and nothing anywhere said so - the pages
showed what *had* played, never what should have played and didn't.

`problems()` is read by the overview page, and once a day by the scheduler,
which speaks a one-line summary the first time a problem appears so a silent
failure cannot sit unnoticed for days again.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .db import get_conn, get_settings, now_local

OFFLINE_AFTER_S = 15 * 60       # the poller checks in every ~20s
SILENT_DAYS = 2                 # an enabled daily briefing that hasn't run
BACKLOG_MINUTES = 60            # queued speech nobody collected
PROBLEM_DAYS = 7                # how far back source problems count


def _ts(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now_local().tzinfo)
    return parsed


def _ago(when: datetime | None) -> str:
    if not when:
        return "从来没有"
    days = (now_local() - when).days
    if days >= 1:
        return f"{days} 天前"
    return when.strftime("%H:%M")


def problems() -> list[dict]:
    """Everything that looks broken right now, worst first.

    Each item has a stable `key` (so the same problem is not announced twice),
    a `level` of "坏了" or "注意", the `text` shown to the user, and `fix`,
    one line on what to do about it.
    """
    now = now_local()
    today = now.date().isoformat()
    out: list[dict] = []

    with get_conn() as conn:
        status = conn.execute("SELECT * FROM device_status WHERE id = 1").fetchone()
        tasks = [dict(r) for r in conn.execute("SELECT * FROM tasks")]
        backlog = conn.execute(
            "SELECT COUNT(*), MIN(created_at) FROM announcements WHERE delivered_at IS NULL"
        ).fetchone()
        recent_problems = [dict(r) for r in conn.execute(
            "SELECT * FROM task_problems ORDER BY id DESC LIMIT 20")]

    last_seen = _ts(status["last_seen_at"]) if status else None
    if not last_seen or (now - last_seen).total_seconds() > OFFLINE_AFTER_S:
        out.append({
            "key": "offline",
            "level": "坏了",
            "text": f"音箱没来取状态了（最后一次 {_ago(last_seen)}）",
            "fix": "看音箱是否通电、联网，或者程序是不是停了",
        })

    for task in tasks:
        if task["recurrence"] == "once":
            continue  # a one-off is meant to stop after it fires
        name = task["name"]
        if not task["enabled"]:
            out.append({
                "key": f"off:{task['id']}",
                "level": "坏了",
                "text": f"「{name}」被停用了，上次播出是 {_ago(_ts(task['last_run_at']))}",
                "fix": "想继续听就在「定时任务」里打开它",
            })
            continue
        held = (task.get("skip_until") or "").strip()
        if held >= today:
            out.append({
                "key": f"muted:{task['id']}",
                "level": "注意",
                "text": f"「{name}」静音到 {held}，之后自动恢复",
                "fix": "不想等就说「恢复播报」",
            })
            continue
        last_run = _ts(task["last_run_at"])
        if task["recurrence"] in ("daily", "weekdays") and (
                not last_run or now - last_run > timedelta(days=SILENT_DAYS)):
            out.append({
                "key": f"silent:{task['id']}",
                "level": "坏了",
                "text": f"「{name}」开着却 {_ago(last_run)} 没播过",
                "fix": "看「跳过的播报」里有没有记录，或让我查一次日志",
            })

    count, oldest = backlog[0], _ts(backlog[1])
    if count and oldest and (now - oldest).total_seconds() > BACKLOG_MINUTES * 60:
        out.append({
            "key": "backlog",
            "level": "坏了",
            "text": f"有 {count} 条播报排着没念出来（最早一条 {_ago(oldest)}）",
            "fix": "多半是音箱没在线，或者播报脚本出错了",
        })

    cutoff = now - timedelta(days=PROBLEM_DAYS)
    seen: set[str] = set()
    for problem in recent_problems:
        created = _ts(problem.get("created_at"))
        if not created or created < cutoff:
            continue
        name = problem.get("task_name") or ""
        if name in seen:
            continue
        seen.add(name)
        out.append({
            "key": f"source:{problem.get('task_id')}",
            "level": "注意",
            "text": f"「{name}」有一次取不到内容被跳过了（{_ago(created)}）",
            "fix": "偶尔一次是对方网站的问题；天天如此就换个来源",
        })

    out.sort(key=lambda item: 0 if item["level"] == "坏了" else 1)
    return out


def speak_line(items: list[dict]) -> str:
    """The one sentence the speaker says when something is newly broken."""
    broken = [i for i in items if i["level"] == "坏了"]
    if not broken:
        return ""
    head = "、".join(i["text"] for i in broken[:2])
    tail = f"，还有 {len(broken) - 2} 项" if len(broken) > 2 else ""
    return f"提醒一下：{head}{tail}。详情在控制台首页。"


def announce_new_problems() -> str:
    """Say a problem out loud once, the first time it shows up.

    Called once a day by the scheduler. Problems already announced stay quiet,
    so this reports a new failure without nagging about a known one.
    """
    items = problems()
    keys = ",".join(sorted(i["key"] for i in items if i["level"] == "坏了"))
    settings = get_settings()
    if keys == (settings.get("health_announced") or ""):
        return ""
    from .db import set_settings
    set_settings({"health_announced": keys})
    line = speak_line(items)
    if not line:
        return ""
    with get_conn() as conn:
        conn.execute("INSERT INTO announcements (task_id, text) VALUES (NULL, ?)", (line,))
        conn.commit()
    return line
