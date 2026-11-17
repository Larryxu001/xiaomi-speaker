"""Voice shortcuts: a phrase the user says (「晚安」) that stands for a longer
instruction (「关掉空调，音量调到 30，放点助眠电台」). Kept in settings as
JSON and sent to the speaker with its config; the speaker swaps the phrase
for the instruction before anything else handles it.
"""

from __future__ import annotations

import json

from .db import get_settings, set_settings

# What the speaker can do without a shortcut. These are not fixed phrasings:
# every sentence goes to the model, so any natural wording works. The words
# below are examples, shown so the user knows what is possible.
BUILT_IN = [
    ("小爱同学", "唤醒音箱。每条指令都从这句开始；说完它会先把正在放的东西停下来再听你说。"),
    ("停止 / 关掉 / 别放了 / 安静", "立刻停掉正在放的一切——音乐、电台、故事、播报，不管是谁放的。怎么说都行。"),
    ("我不想听这个了 / 你听不懂吗，停止", "同上。说得再长、再着急也能停。"),
    ("暂停 / 暂停播放", "暂停音乐或电台，结束对话。"),
    ("继续 / 继续播放", "从暂停的地方接着放。"),
    ("下一首 / 换一个 / 切歌 / 跳过 / 换个台", "换下一首歌或下一个电台。不用记固定说法。"),
    ("上一首", "回到刚才那首。"),
    ("大点声 / 小声一点 / 音量调到六十", "调音箱自己的音量。"),
    ("没有了 / 不用了 / 再见", "结束对话。"),
    ("你会做什么", "听一遍音箱能做的事。"),
    ("放首歌 / 来点轻音乐 / 放个电台 / 讲个故事", "放音乐、网络电台，或交给小爱原生服务放有声内容。"),
]

EXAMPLES = [
    ("晚安", "关掉空调，把音量调到 30，放点助眠的电台"),
    ("早上好", "现在播一下早报"),
    ("出门了", "关掉空调和客厅灯，停止播放"),
    ("我回来了", "打开客厅灯，放我收藏的电台"),
]


def load(settings: dict | None = None) -> list[dict]:
    try:
        items = json.loads((settings or get_settings()).get("shortcuts") or "[]")
    except ValueError:
        return []
    return [i for i in items if isinstance(i, dict) and i.get("say") and i.get("run")]


def save(items: list[dict]) -> None:
    set_settings({"shortcuts": json.dumps(items, ensure_ascii=False)})


def upsert(say: str, run: str, note: str = "", index: int | None = None) -> str:
    say, run, note = say.strip(), run.strip(), note.strip()
    if not say or not run:
        raise ValueError("「说什么」和「音箱会做什么」都要填。")
    if len(say) > 20:
        raise ValueError("说的话短一点更好认，最多 20 个字。")
    items = load()
    clash = next((n for n, i in enumerate(items) if i["say"] == say and n != index), None)
    if clash is not None:
        raise ValueError(f"已经有一条「{say}」了。")
    entry = {"say": say, "run": run[:300], "note": note[:80]}
    if index is not None and 0 <= index < len(items):
        items[index] = entry
    else:
        items.append(entry)
    save(items)
    return f"已保存「{say}」，音箱大约半分钟内生效。"


def delete(index: int) -> None:
    items = load()
    if 0 <= index < len(items):
        items.pop(index)
        save(items)
