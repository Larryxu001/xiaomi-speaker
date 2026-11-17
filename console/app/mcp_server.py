"""The console as an MCP server, so the speaker can operate it by voice.

The speaker's agent connects here like any other MCP server (it is added to
agent.yaml automatically as "console"), authenticating with its device token.
After "小爱同学", the model can call these tools to switch models, manage
scheduled tasks and change a few settings. By the owner's choice, anyone who
can wake the speaker may use every tool; there is no extra confirmation.

Changes that touch agent.yaml (model, search, answer length) reach the
speaker on its next poll, which restarts the voice service: the tool replies
say so, so the speaker can warn the user before the short restart.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import logging

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from . import services
from . import sources as srcmod
from .db import get_conn, get_settings
from .services import ServiceError

log = logging.getLogger("console.mcp")

APPLY_NOTE = "大约 20 秒后生效，生效时语音服务会重启几秒钟。"

mcp = MCPServer(
    name="console",
    instructions=(
        "这是小米音箱的控制台。用户想切换大模型、查看或管理定时任务（提醒、播报、新闻分析）、"
        "开关联网搜索、调整回答长短、安装或卸载服务器上的应用，或询问音箱和控制台状态时，调用这里的工具。"
        "工具返回的文字可以直接转述给用户。"
    ),
)


def _say(fn):
    """Turn ServiceError into a spoken explanation instead of a tool failure."""
    @functools.wraps(fn)  # keeps the signature, which becomes the tool's input schema
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (ServiceError, srcmod.SourceError) as exc:
            return str(exc)
    return wrapper


@mcp.tool(name="console_status", description="查询音箱和控制台的当前状态：用的哪个大模型、联网搜索是否开启、回答长度、启用中的定时任务数量。")
@_say
async def console_status() -> str:
    s = get_settings()
    profile = services.current_profile_name(s) or s.get("llm_model")
    enabled_tasks = sum(1 for t in services.list_tasks() if t["enabled"])
    search = "开启" if s.get("web_search_enabled") == "1" else "关闭"
    return (f"当前对话模型是 {profile}（{s.get('llm_model')}），联网搜索{search}，"
            f"回答长度上限 {s.get('llm_max_tokens')} token，启用中的定时任务有 {enabled_tasks} 个。")


@mcp.tool(name="list_model_profiles", description=(
    "列出控制台里保存的对话模型档案和语音识别档案，以及当前各用哪一个。用户问「有哪些模型可以用」时调用。"))
@_say
async def list_model_profiles() -> str:
    out = []
    for kind, label in (("llm", "对话模型"), ("asr", "语音识别")):
        profiles = services.list_profiles(kind)
        if not profiles:
            out.append(f"{label}还没有保存档案")
            continue
        current = services.current_profile_name(kind=kind)
        names = "、".join(f"{p['name']}{'（正在用）' if p['name'] == current else ''}" for p in profiles)
        out.append(f"{label}档案有：{names}")
    return "；".join(out) + "。"


@mcp.tool(name="switch_model", description=(
    "切换到控制台里保存的某个档案。profile 填档案名或模型名，例如 DeepSeek、通义、硅基流动。"
    "kind 填 llm 表示对话模型（默认），填 asr 表示语音识别；用户说「语音识别换成硅基流动」时用 asr。"))
@_say
async def switch_model(profile: str, kind: str = "llm") -> str:
    kind = "asr" if kind.strip().lower() == "asr" else "llm"
    label = "语音识别" if kind == "asr" else "对话模型"
    target = services.find_profile(profile, kind)
    if target["name"] == services.current_profile_name(kind=kind):
        return f"{label}现在用的已经是 {target['name']} 了。"
    services.apply_profile(target["id"], kind)
    log.info("voice switched %s to %s", kind, target["name"])
    return f"好的，{label}已切换到 {target['name']}（{target['model']}），{APPLY_NOTE}"


@mcp.tool(name="list_tasks", description="列出所有定时任务（提醒、固定播报、AI 新闻分析），包括时间和是否启用。")
@_say
async def list_tasks() -> str:
    tasks = services.list_tasks()
    if not tasks:
        return "还没有定时任务。"
    parts = []
    for t in tasks:
        state = "" if t["enabled"] else "，已停用"
        kind = "AI 分析" if t["kind"] == "news_analysis" else "提醒"
        parts.append(f"{t['name']}：{services.describe_schedule(t)}，{kind}{state}")
    return "；".join(parts) + "。"


@mcp.tool(
    name="create_task",
    description=(
        "新建定时任务。content 是到点要说的话；如果 kind 是 news_analysis，content 是要分析的话题，"
        "到点会联网搜索后生成口播稿。repeat 取值：daily 每天、weekdays 工作日、weekly 每周、once 只一次。"
        "time 用 24 小时制 HH:MM；repeat 为 once 时 time 写成 YYYY-MM-DD HH:MM。"
        "weekday 只在 weekly 时填，如 周一 或 0-6。name 是简短的任务名，可省略。"
        "用户说「明天早上七点提醒我开会」这类一次性提醒时，用 once 并算出具体日期。"
        "用户指定了内容来源（如「每天八点播微博热搜和万得财经早餐」）时，kind 用 news_analysis，"
        "sources 填来源名称，用逗号分隔，content 写怎么播（如「侧重 A 股和科技」）。先用 list_sources 查有哪些来源。"
        "length 是播报长度：short 约 1 分钟、standard 约 3 分钟（默认）、long 约 5 分钟、"
        "full 完整（每个来源前 10 条按顺序逐条完整播，适合早报，用户说「完整」「别删减」「每个平台十条」时用）。"
        "用户的要求模糊时（如「帮我盯着 AI 圈」「早上给我播点新闻」），不要照原话建：先用 list_sources 看现有来源，"
        "缺的 NewsNow 平台可以用 add_newsnow_source 补上，替用户想好来源、时间、长度，用一句话说出方案问「这样行吗」，"
        "用户同意后再调用本工具。用户已经说得很具体时直接建。"
    ),
)
@_say
async def create_task(content: str, time: str, repeat: str = "daily", kind: str = "announcement",
                      weekday: str = "", name: str = "", sources: str = "", length: str = "standard") -> str:
    source_ids = srcmod.resolve_names(sources) if sources.strip() else []
    if source_ids:
        kind = "news_analysis"
    task_id = services.create_task(name, kind, repeat, time, content, weekday or None, source_ids, length)
    t = next(t for t in services.list_tasks() if t["id"] == task_id)
    return f"已创建任务「{t['name']}」，{services.describe_schedule(t)}。"


@mcp.tool(name="list_sources", description="列出定时任务可用的内容来源（微博热搜、财联社电报、万得财经早餐等）。")
@_say
async def list_sources() -> str:
    items = srcmod.list_sources()
    if not items:
        return "还没有内容来源。"
    return "现有来源：" + "、".join(s["name"] for s in items) + "。"


@mcp.tool(name="add_newsnow_source", description=(
    "把 NewsNow 的某个平台热榜加为内容来源。platform 填平台中文名，例如 知乎热榜、IT 之家、金十数据、雪球热股。"))
@_say
async def add_newsnow_source(platform: str) -> str:
    platform = platform.strip()
    hit = next((pid for pid, n in srcmod.NEWSNOW_NAMES.items() if platform in n or n in platform or platform == pid), None)
    if not hit:
        raise services.ServiceError("NewsNow 里没有这个平台。可选：" + "、".join(srcmod.NEWSNOW_NAMES.values()) + "。")
    srcmod.save_source("", "newsnow", hit)
    return f"已添加来源「{srcmod.NEWSNOW_NAMES[hit]}」。"


@mcp.tool(name="read_source_now", description="立刻读取某个内容来源并播报要点，比如「现在播一下财联社电报」。source 填来源名称。")
@_say
async def read_source_now(source: str, instruction: str = "挑最重要的几条播报") -> str:
    from .scheduler import _run_source_digest
    ids = srcmod.resolve_names(source)
    return await _run_source_digest({"id": 0, "name": source, "length": "short"}, get_settings(), instruction, ids)


@mcp.tool(name="set_voice", description=(
    "更换音箱说话的音色或语速。voice 填音色名或特点，例如 晓晓、云扬、东北话、粤语男声、温柔女声、小爱原声；"
    "rate 可填 slow、normal、fast，不改就留空。用户问有哪些音色时先调用 list_voices。"))
@_say
async def set_voice(voice: str = "", rate: str = "") -> str:
    from . import tts
    updates = {}
    if voice.strip():
        vid = tts.NATIVE if ("小爱" in voice or "原" in voice) else tts.find_voice(voice)
        if not vid:
            raise ServiceError(f"没有找到「{voice}」这个音色。可以说 list_voices 里的名字。")
        problem = tts.voice_problem(vid)
        if problem:
            raise ServiceError(problem)
        updates["tts_voice"] = vid
    if rate.strip():
        if rate not in tts.RATES:
            raise ServiceError("语速只能是 slow、normal 或 fast。")
        updates["tts_rate"] = rate
    if not updates:
        raise ServiceError("请告诉我要换成哪个音色，或者调快还是调慢。")
    from .db import set_settings
    set_settings(updates)
    label = tts.LABELS.get(updates.get("tts_voice", get_settings().get("tts_voice")), "")
    return f"好的，已换成{label}。" if "tts_voice" in updates else "好的，语速已调整。"


@mcp.tool(name="list_voices", description=(
    "列出可选的音色。回答用户时不要全部念出来：挑四五个有代表性的说（比如一个女声、一个男声、一个方言、一个更像真人的），"
    "再告诉用户还有方言、粤语、台湾腔可以选，让他说想要哪种。"))
@_say
async def list_voices() -> str:
    from . import tts
    current = tts.LABELS.get(get_settings().get("tts_voice", tts.NATIVE), "小爱原声")
    groups = []
    for g, items in tts.VOICES:
        usable = [l for v, l in items if not tts.voice_problem(v)]
        if usable:
            groups.append(f"{g}：" + "、".join(usable))
    return f"现在用的是{current}。可选音色——" + "；".join(groups) + "。"


@mcp.tool(name="remember", description=(
    "记住关于用户的一件事，长期有效。用户说「记住……」「以后……」「我喜欢/不喜欢……」这类希望你记下来的话时调用。"
    "content 用第三人称写成一句话，如「用户不吃香菜」「用户的女儿叫朵朵」。"))
@_say
async def remember(content: str) -> str:
    from . import memory
    memory.add_memory(content, "其他", "语音")
    return "好，记住了。"


@mcp.tool(name="recall_memories", description=(
    "查看你记得的关于用户的事。用户问「你记得我什么」「你知道我喜欢什么吗」时调用；回答时挑重要的几条说，不要全念。"))
@_say
async def recall_memories() -> str:
    from . import memory
    mems = memory.list_memories()
    if not mems:
        return "还没有记住关于用户的事。"
    return "记得的事：" + "；".join(m["content"] for m in mems[:30]) + "。"


@mcp.tool(name="forget", description=(
    "忘掉关于用户的某件事。用户说「忘掉……」「那个记错了」「别记这个」时调用。about 填要忘掉的内容关键词。"))
@_say
async def forget(about: str) -> str:
    from . import memory
    hits = memory.find_memories(about)
    if not hits:
        raise ServiceError(f"我没有记过关于「{about}」的事。")
    for m in hits[:3]:
        memory.delete_memory(m["id"])
    return "好，已经忘掉了：" + "；".join(m["content"] for m in hits[:3]) + "。"


@mcp.tool(name="list_music_sources", description="列出可用的歌曲音源（网易云、QQ 音乐、酷狗、曲库等），以及优先用哪个。电台不在这里。")
@_say
async def list_music_sources() -> str:
    from . import music
    srcs = music.song_sources(enabled_only=True)
    if not srcs:
        return "还没有启用的音源。"
    return "现在按这个顺序找歌：" + "、".join(s["name"] for s in srcs) + "。"


@mcp.tool(name="use_music_source", description=(
    "切换优先使用的歌曲音源。用户说「用网易云放」「以后先从酷狗找」时调用。source 填音源名。电台不是音源，不要用它切到电台。"
    "切换后再调用播放音乐的工具。"))
@_say
async def use_music_source(source: str) -> str:
    from . import music
    try:
        hit = music.use_source(source)
    except music.MusicError as exc:
        raise ServiceError(str(exc))
    return f"好，以后先从{hit['name']}找歌。"


@mcp.tool(name="what_can_you_do", description=(
    "介绍你能做什么。用户问「你会做什么」「有什么功能」「怎么用」时调用，挑几个场景用口语说，别全念。"))
@_say
async def what_can_you_do() -> str:
    from . import guide
    return guide.spoken_summary()


@mcp.tool(name="favorite_station", description="收藏正在放的电台。用户说「收藏这个台」「这个台不错，记下来」时调用。")
@_say
async def favorite_station() -> str:
    from . import radio
    cur = radio.current_station()
    if not cur:
        raise ServiceError("现在没有在放电台。")
    radio.favourite(cur["id"], True)
    return f"收藏了「{cur['name']}」。"


@mcp.tool(name="unfavorite_station", description="取消收藏正在放的电台，或名字里含 name 的收藏电台。")
@_say
async def unfavorite_station(name: str = "") -> str:
    from . import radio
    favs = radio.favourites()
    hit = next((f for f in favs if name and name in f["name"]), None)
    if not hit:
        cur = radio.current_station()
        hit = next((f for f in favs if cur and f["station_id"] == cur["id"]), None)
    if not hit:
        raise ServiceError("收藏里没有这个台。")
    radio.favourite(hit["station_id"], False)
    return f"取消收藏「{hit['name']}」了。"


@mcp.tool(name="play_favorite_stations", description=(
    "放用户收藏的电台。用户说「放我收藏的电台」「放我喜欢的台」时调用；name 可填收藏里某个台的名字。"))
@_say
async def play_favorite_stations(name: str = "") -> str:
    from . import commands, radio
    favs = radio.favourites()
    if not favs:
        raise ServiceError("还没有收藏的电台。听到喜欢的台时说「收藏这个台」就行。")
    hit = next((f for f in favs if name and name in f["name"]), favs[0])
    commands.push({"type": "play", "id": hit["station_id"]})
    return f"好，放「{hit['name']}」。"


@mcp.tool(name="list_favorite_stations", description="列出收藏的电台。")
@_say
async def list_favorite_stations() -> str:
    from . import radio
    favs = radio.favourites()
    return ("收藏的电台：" + "、".join(f["name"] for f in favs[:10]) + "。") if favs else "还没有收藏的电台。"


@mcp.tool(name="set_task_enabled", description="启用或停用某个定时任务。task 填任务名或任务内容里的关键词；enabled 为 true 启用、false 停用。")
@_say
async def set_task_enabled(task: str, enabled: bool) -> str:
    t = services.find_task(task)
    services.set_task_enabled(t["id"], enabled)
    return f"已{'启用' if enabled else '停用'}「{t['name']}」。"


@mcp.tool(name="delete_task", description="删除某个定时任务。task 填任务名或任务内容里的关键词。")
@_say
async def delete_task(task: str) -> str:
    t = services.find_task(task)
    services.delete_task(t["id"])
    return f"已删除「{t['name']}」。"


@mcp.tool(name="run_task_now", description="立刻执行一次某个定时任务，比如用户说「现在播一下早间新闻」。task 填任务名或关键词。")
@_say
async def run_task_now(task: str) -> str:
    from .scheduler import queue_task_now  # imported late: scheduler imports providers
    t = services.find_task(task)
    await queue_task_now(t, get_settings())
    return f"好的，「{t['name']}」马上开始播报。"


@mcp.tool(name="set_web_search", description="开启或关闭联网搜索。enabled 为 true 开启、false 关闭。")
@_say
async def set_web_search(enabled: bool) -> str:
    services.set_web_search(enabled)
    return f"已{'开启' if enabled else '关闭'}联网搜索，{APPLY_NOTE}"


@mcp.tool(name="set_search_provider", description="切换联网搜索用的服务。provider 填 Tavily 或 Firecrawl。")
@_say
async def set_search_provider(provider: str) -> str:
    name = services.set_search_provider(provider)
    return f"好的，联网搜索改用 {name}，{APPLY_NOTE}"


@mcp.tool(name="set_answer_length", description="调整回答长短。level 取值：短、中、长。用户说「回答简短一点」「说详细一点」时调用。")
@_say
async def set_answer_length(level: str) -> str:
    tokens = services.set_answer_length(level)
    return f"已把回答长度调成{level}（上限 {tokens} token），{APPLY_NOTE}"


@mcp.tool(name="list_apps", description="列出服务器上可以安装的应用（开源软件），以及哪些已经装好、是否在运行。")
@_say
async def list_apps() -> str:
    from . import installer_client as inst
    try:
        apps = await inst.list_apps()
    except inst.InstallerUnavailable as exc:
        return f"{exc}暂时不能安装软件。"
    if not apps:
        return "应用商店里还没有任何应用。"
    parts = []
    for a in apps:
        st = a.get("state") or {}
        state = ("已安装，运行中" if st.get("running") else "已安装，已停止") if st.get("installed") else "未安装"
        parts.append(f"{a.get('name', a['id'])}：{a.get('description', '')}（{state}）")
    return "；".join(parts) + "。"


@mcp.tool(name="install_app", description="安装应用商店里的某个应用。app 填应用名或关键词。安装需要一两分钟，装好后音箱会播报。")
@_say
async def install_app(app: str) -> str:
    from . import apps_service
    target = await apps_service.find_app(app)
    await apps_service.start_install(target, {})
    return f"开始安装{target.get('name', target['id'])}，大约需要一两分钟，装好后我会告诉你。"


@mcp.tool(name="uninstall_app", description="卸载某个已安装的应用。app 填应用名或关键词。数据会保留。")
@_say
async def uninstall_app(app: str) -> str:
    from . import apps_service
    target = await apps_service.find_app(app)
    await apps_service.start_uninstall(target)
    return f"开始卸载{target.get('name', target['id'])}，数据会保留。"


def build_mcp_app(allowed_hosts: list[str]):
    """Streamable HTTP endpoint, stateless JSON so short polls never hang.

    The Host check only understands exact names and "name:*", so "*" means
    "any address" here and has to turn the check off rather than be passed
    through as a host name. Only the speaker reaches this endpoint anyway:
    DeviceTokenAuth rejects every request without the device token.
    """
    any_host = "*" in allowed_hosts
    return mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=not any_host,
            allowed_hosts=[] if any_host else allowed_hosts,
            allowed_origins=[],
        ),
    )


class DeviceTokenAuth:
    """Only the speaker (holding the device token) may call the console's tools."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        supplied = headers.get("authorization", "").removeprefix("Bearer ").strip()
        expected = get_settings().get("device_token", "")
        ok = bool(expected) and hmac.compare_digest(
            hashlib.sha256(supplied.encode()).digest(), hashlib.sha256(expected.encode()).digest())
        if not ok:
            body = b'{"error":"forbidden"}'
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
