use std::sync::Arc;

use anyhow::Context;
use rig_core::client::CompletionClient;
use futures::StreamExt;
use rig_core::agent::{HookAction, MultiTurnStreamItem, PromptHook, ToolCallHookAction};
use rig_core::streaming::{StreamedAssistantContent, StreamingPrompt};
use tokio::sync::mpsc::UnboundedSender;
use rig_core::completion::CompletionModel;
use rig_core::providers::{anthropic, openai};
use rig_core::tool::server::{ToolServer, ToolServerHandle};
use serde_json::{json, Value};
use tokio::sync::Mutex;
use tracing::{debug, info, warn};

use crate::config::{timeout_duration, AppConfig, LlmConfig, LlmProtocol, LlmThinkingMode};
use crate::failover::Failover;
use crate::sentences::SentenceSplitter;
use crate::device::Device;
use crate::mcp::McpConnections;
use crate::music::MusicService;
use crate::tools::*;

/// Only while the microphone stays open without a wake word can a sentence
/// be someone else's. With follow-up listening off, every sentence arrived
/// right after "小爱同学", so ignoring one means ignoring the user.
const IGNORE_RULE: &str = "音箱会继续听用户接着说的话，所以会听到旁边人的聊天、电视声、自言自语和半句话。如果这句话明显不是对你说的、或者没有能执行的意思（家人间的方言聊天、零碎片段、与上文无关的话），只输出【忽略】这三个字，不要追问、不要猜。";

/// 听错一个字是常事（「下一曲」听成「下一句」）：按意思去理解，不要因为字面
/// 奇怪就当成不是对自己说的。
const MISHEARD_RULE: &str = "用户每句话都是刚喊过「小爱同学」之后说的，所以一定是对你说的，必须回应。语音识别偶尔会听错字（「下一曲」听成「下一句」，「停止」听成「天资」），遇到奇怪的词先按读音和当前在做的事去猜它的意思；实在猜不出来，就问一句「你是说……吗」，绝不要沉默或当作没听见。";

fn instructions(config: &AppConfig) -> String {
    let mut out = SPEAKER_AGENT_INSTRUCTIONS.to_string();
    out.push_str(if config.runtime.follow_up { IGNORE_RULE } else { MISHEARD_RULE });
    out
}

pub const SPEAKER_AGENT_INSTRUCTIONS: &str = concat!(
    "你是小爱，住在用户家里音箱上的私人助理。说话像一个熟悉、靠谱、有温度的朋友：自然、简洁、有分寸。",
    "你说的每句话都会被直接读出来，所以要用口语，不用 Markdown、不用列表符号、不读网址，也不要输出思考过程。",
    "话要短：普通回答一句话说完；执行操作后的确认不超过十个字（「好，放上了」「调小了」「暂停了」）；用户要详细讲解、讲故事或播报时才说长。",
    "不要每句话都用「嗯」「好嘞」开头，不要重复用户的话，不要在结尾加「有需要随时叫我」这类客套。",
    "交流方式：先接住用户的话再回答——用户分享心情或经历时，先回应一句再给建议；用户提问时直接给答案，不要绕。",
    "做完一件事（换音色、建提醒、开灯、放音乐）用一句话确认结果，比如「好了，明早七点叫你」；不要每次都追问「还有什么需要吗」，只在自然的时候问。",
    "有合理的默认做法就直接做，不要让用户选：「放王菲的歌」就直接放她的歌，「放点音乐」就直接放。只有会改动设置或设备、且确实不知道要改成什么时，才问一个问题。",
    "没听清或听不懂时，只说一句「没听清，再说一遍？」，不要猜着列出几种可能让用户选。",
    "不知道原因的事就说不知道，绝不编造理由（比如问「你的声音为什么怪」，不能编一个说法，要说「这个我不清楚，可以在控制台换个音色试试」）。",
    "给选项时最多说三个有代表性的，不要把长清单全念出来。",
    "记住这次对话里用户说过的事（名字、喜好、刚才在聊什么），后面直接用，不要重复问。用户说「换成那个」「再大点」这类省略的话时，根据上文理解。",
    "可以偶尔用「好嘞」「嗯」「行」这类口语词，但不要每句都用，不要油腻、不要卖萌过度。",
    "不要提自己是 AI 模型、不要提工具名、接口、系统等技术细节；做不到的事坦率说做不到，并给一个可行的替代办法。",
    "用户说「没有了」「不用了」「就这样」「好的谢谢」「再见」「没事了」这类表示结束的话时，必须调用 end_conversation，",
    "并用一句温暖简短的话收尾，比如「好的，有需要随时叫我」「行，那我先退下了」，每次说法可以不一样。",
    "用户只说「谢谢」但没有要结束的意思时，回一句「不客气」就好。",
    "当前日期和时间已经写在每轮消息开头，问几点、星期几、几号直接回答，不要调用工具。问天气或预报时必须调用天气工具。",
    "用户话里有任何让你停下的意思（停止、别放了、关掉、闭嘴、安静、你听不懂吗），立刻调用停止播放工具，再回一句很短的话。这种时候绝不能回「没听清」「再说一遍」，也不能只暂停。",
    "用户说换一首、下一个、切歌、跳过、换个台这类意思，直接调用下一首工具；说大点声、小一点、调到多少，直接调用音量工具。不要反问，不要先查播放状态。",
    "能一步做完的就一步做完：控制音乐（下一首、换台、暂停、停止）直接调用对应工具，不要先查询播放状态；不要为了确认而多调用工具。需要多个互不相关的工具时，在同一轮里一起调用。",
    "当用户询问新闻、最近发生的事、实时资料，或明确要求上网搜索时，必须调用 web_search 工具。",
    "要尽力把用户交代的事做完整：要十件就给十件，要列清单就列全。信息不够时换关键词、换中英文再搜，或者用 read_source_now 读新闻热榜，而不是直接说资料零碎；只有真的尽力了还完不成，才说明完成了多少、差在哪里。",
    "联网搜索用宽一些的查询（比如「本周国际要闻」），盘点类问题 max_results 填 10；一次回答最多搜五次，资料够了就立即作答。",
    "基于搜索结果回答时，简短说明信息来源；某一条互相矛盾时，只对那一条说不确定，其余照常回答。",
    "闹钟、提醒、定时播报都用 create_task（一次性用 once，每天用 daily），绝不要交给 control_smart_home。",
    "用户只是今晚或某几天不想被播报吵到（「明早别吵我」「我要睡觉」「这周末别播」）时，用 mute_tasks_until 静音到那天，绝不要用 set_task_enabled 永久停用；只有用户明确说以后都不要了，才停用或删除。",
    "调音箱自己的音量（大点声、小点声、调到多少）用 set_volume，不要用智能家居工具。",
    "当用户要求控制或查询家里的米家设备（灯、空调、窗帘、插座、扫地机等）时，调用 control_smart_home，",
    "把用户的话整理成一句完整的中文指令传给它，例如「把客厅灯调到一半亮度」；",
    "如果结果显示设备离线或找不到设备，要如实告诉用户，不要假装成功。",
    "只有工具返回明确成功时才可以说操作完成。",
    "如果工具返回错误、没有匹配设备或结果不明确，要说明无法确认，不要假装成功。",
    "当用户要求播放、搜索、停止音乐或询问当前播放内容时，必须调用音乐工具。",
    "用户让你定时播报、盯某类信息但说得模糊时，先替他想全（来源、时间、长度），用一句话说出方案问「这样行吗」，他同意后再建任务；说得具体就直接建。",
    "喜马拉雅、得到、蜻蜓FM、小宇宙、有声书、故事、相声评书这类有声内容用 play_native_content，交给小爱原生服务播放。",
    "电台和音乐是分开的：用户要听电台、广播、某个台（中国之声、交通台、爵士电台）时只用 play_radio，不要切换音源、不要用歌曲工具；放着电台说「换一台」用 play_next_music。",
    "用户只说播放某首歌或某位歌手时，优先调用 play_music_query，",
    "这会替换当前队列并开始播放；用户说放点音乐、随便放点歌、随机播放时，调用 play_random_music；",
    "用户说加入、添加到队列、稍后播放时，调用 add_music_to_play_queue；",
    "用户明确说随机加几首到队列时，调用 add_random_music_to_play_queue。",
    "用户说下一首、上一首、换一首、换一台、暂停、继续播放时，调用对应的音乐播放控制工具；不确定在放什么时先查播放状态。",
    "音乐播放后只需一句话确认（比如「放上了，等什么君的春庭雪」），不要再追问。",
    "确认播放时，歌手名必须用播放结果里 song.artists 的真实歌手，不能照搬用户说的歌手。",
    "播放结果里歌曲的 album 是「非原版」时，说明用户要的原版暂时放不了（多半需要会员），确认时如实说一句，比如「王菲的原版需要会员，先放的是某某的版本」，歌名里带 DJ、翻唱等字样就照实说是 DJ 版或翻唱。",
    "不要提「队列」：一首放完会自动推荐下一首，一直放到用户说停；用户说下一首、换一首时调用 play_next_music，它会自己找推荐。",
    "有音乐在放时，做完调音量、暂停、切歌这类操作只说一句结果，不要反问「合适吗」「还要什么」——对话会马上结束让音乐继续。",
    "如果你已经说过「好嘞」，确认时不要再重复「好嘞」。",
    "如果候选不明确或播放失败，再简短追问或说明原因。",
    "当音乐工具提示需要网易云验证码登录，或用户要求登录网易云时，",
    "先调用 request_music_login_code 发送验证码；用户报出短信数字后，",
    "调用 submit_music_login_code 完成登录；登录 cookie 只在本次进程内保存。",
    "如果工具返回错误或信息不足，要坦率说明无法确认。"
);

#[derive(Debug, Clone)]
pub struct AgentTurnResult {
    pub text: String,
    pub should_end: bool,
    pub end_reason: String,
}

#[derive(Default)]
pub struct AgentControl {
    pub should_end: bool,
    pub end_reason: String,
}

/// Streams one model reply: text deltas are cut into sentences and handed to
/// the speaker as they arrive; returns the whole text. (A macro because the
/// OpenAI and Anthropic agents are different generic types.)
macro_rules! stream_reply {
    ($self:ident, $agent:ident, $prompt:ident) => {{
        let mut stream = $agent
            .stream_prompt($prompt)
            .multi_turn(MAX_TOOL_TURNS)
            .with_hook($self.tool_hook())
            .await;
        let mut full = String::new();
        let mut splitter = SentenceSplitter::default();
        while let Some(item) = stream.next().await {
            let item = item.map_err(|err| anyhow::anyhow!("{err}"))?;
            if let MultiTurnStreamItem::StreamAssistantItem(StreamedAssistantContent::Text(text)) = item {
                full.push_str(&text.text);
                for piece in splitter.push(&text.text) {
                    $self.emit(piece).await;
                }
            }
        }
        if let Some(piece) = splitter.finish() {
            $self.emit(piece).await;
        }
        Ok(full.trim().to_string())
    }};
}

pub struct AgentRuntime {
    config: Arc<AppConfig>,
    device: Device,
    /// "稍等，我查一下" spoken while a slow tool runs; awaited before the answer.
    filler: Arc<Mutex<Option<tokio::task::JoinHandle<()>>>>,
    tool_server: ToolServerHandle,
    _mcp: McpConnections,
    history: Mutex<Vec<(String, String)>>,
    /// When the last turn finished; history older than MEMORY_WINDOW is dropped.
    last_turn: Mutex<Option<std::time::Instant>>,
    control: Arc<Mutex<AgentControl>>,
    llm_failover: Failover,
    http_clients: Mutex<std::collections::HashMap<String, reqwest::Client>>,
    /// Counts tool calls so a failed turn that already acted isn't replayed.
    tool_calls: Arc<std::sync::atomic::AtomicUsize>,
    /// Tool calls / searches in the current reply (reset each turn).
    turn_calls: Arc<std::sync::atomic::AtomicUsize>,
    turn_searches: Arc<std::sync::atomic::AtomicUsize>,
    /// Where this turn's reply goes, sentence by sentence, as it is written.
    reply_sink: Arc<Mutex<Option<UnboundedSender<String>>>>,
    /// Long-term memories about the user, fetched from the console on wake.
    memories: Arc<Mutex<String>>,
    http: reqwest::Client,
    /// Pieces already handed to the speaker this attempt (no failover after that).
    emitted: std::sync::atomic::AtomicUsize,
}

impl AgentRuntime {
    pub async fn new(
        config: Arc<AppConfig>,
        device: Device,
        music: Arc<MusicService>,
    ) -> anyhow::Result<Self> {
        let control = Arc::new(Mutex::new(AgentControl::default()));
        let tool_server = ToolServer::new()
            .tool(GetCurrentTime::new(config.clone()))
            .tool(GetWeather::new(config.clone()))
            .tool(WebSearch::new(config.clone()))
            .tool(EndConversation::new(control.clone()))
            .tool(ControlSmartHome)
            .tool(SetVolume)
            .tool(SearchMusic::new(music.clone()))
            .tool(RequestMusicLoginCode::new(music.clone()))
            .tool(SubmitMusicLoginCode::new(music.clone()))
            .tool(PlayMusicQuery::new(music.clone()))
            .tool(PlayRadio::new(music.clone()))
            .tool(PlayNative::new(music.clone()))
            .tool(AddMusicToQueue::new(music.clone()))
            .tool(AddRandomMusicToQueue::new(music.clone()))
            .tool(PlayRandomMusic::new(music.clone()))
            .tool(StopMusicPlayback::new(music.clone()))
            .tool(PauseMusicPlayback::new(music.clone()))
            .tool(ResumeMusicPlayback::new(music.clone()))
            .tool(PlayNextMusic::new(music.clone()))
            .tool(PlayPreviousMusic::new(music.clone()))
            .tool(GetMusicStatus::new(music))
            .run();

        let mcp = McpConnections::connect(config.clone(), tool_server.clone()).await;
        if let Err(err) = device.stop_audio().await {
            warn!("启动时停止播放失败，继续运行：{err:?}");
        }
        info!("对话模型已就绪：{}", config.llm.model);
        if !config.llm_fallbacks.is_empty() {
            let names: Vec<&str> = config.llm_fallbacks.iter().map(|l| l.model.as_str()).collect();
            info!("备用对话模型：{}", names.join("、"));
        }

        Ok(Self {
            device: device.clone(),
            filler: Arc::new(Mutex::new(None)),
            config,
            tool_server,
            _mcp: mcp,
            history: Mutex::new(Vec::new()),
            last_turn: Mutex::new(None),
            control,
            llm_failover: Failover::default(),
            http_clients: Mutex::new(std::collections::HashMap::new()),
            tool_calls: Arc::new(std::sync::atomic::AtomicUsize::new(0)),
            turn_calls: Arc::new(std::sync::atomic::AtomicUsize::new(0)),
            turn_searches: Arc::new(std::sync::atomic::AtomicUsize::new(0)),
            reply_sink: Arc::new(Mutex::new(None)),
            memories: Arc::new(Mutex::new(String::new())),
            http: reqwest::Client::new(),
            emitted: std::sync::atomic::AtomicUsize::new(0),
        })
    }

    async fn emit(&self, piece: String) {
        if let Some(sink) = self.reply_sink.lock().await.as_ref() {
            if sink.send(piece).is_ok() {
                self.emitted.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            }
        }
    }

    fn tool_hook(&self) -> ToolLogHook {
        ToolLogHook {
            turn_calls: self.turn_calls.clone(),
            turn_searches: self.turn_searches.clone(),
            device: self.device.clone(),
            filler: self.filler.clone(),
            calls: self.tool_calls.clone(),
            sink: self.reply_sink.clone(),
        }
    }

    /// Runs one turn. The reply is streamed into `sink` sentence by sentence
    /// while the model writes it (the speaker starts talking at the first
    /// one); the full text is returned at the end.
    pub async fn run_turn(&self, message: &str, sink: UnboundedSender<String>) -> anyhow::Result<AgentTurnResult> {
        {
            let mut control = self.control.lock().await;
            control.should_end = false;
            control.end_reason.clear();
        }
        self.turn_calls.store(0, std::sync::atomic::Ordering::Relaxed);
        self.turn_searches.store(0, std::sync::atomic::Ordering::Relaxed);
        *self.reply_sink.lock().await = Some(sink);

        let prompt = self.prompt_with_history(message).await;
        let result = self.prompt_once(prompt).await;
        *self.reply_sink.lock().await = None; // closes the channel: the reply is complete
        // Never let the answer talk over "稍等，我查一下".
        if let Some(filler) = self.filler.lock().await.take() {
            let _ = filler.await;
        }
        let text = result?;

        let control = self.control.lock().await;
        self.push_history(message, &text).await;
        Ok(AgentTurnResult {
            text,
            should_end: control.should_end,
            end_reason: control.end_reason.clone(),
        })
    }

    async fn prompt_once(&self, prompt: String) -> anyhow::Result<String> {
        let models: Vec<&LlmConfig> = std::iter::once(&self.config.llm)
            .chain(self.config.llm_fallbacks.iter())
            .collect();
        let mut last_error = None;
        for index in self.llm_failover.order(models.len()) {
            let llm = models[index];
            let before = self.tool_calls.load(std::sync::atomic::Ordering::Relaxed);
            self.emitted.store(0, std::sync::atomic::Ordering::Relaxed);
            match self.prompt_with(llm, prompt.clone()).await {
                Ok(text) => {
                    if index != 0 {
                        info!("对话模型改用备用：{}", llm.model);
                    }
                    self.llm_failover.succeeded(index);
                    return Ok(text);
                }
                Err(err) => {
                    let ran_tools = self.tool_calls.load(std::sync::atomic::Ordering::Relaxed) != before
                        || self.emitted.load(std::sync::atomic::Ordering::Relaxed) > 0;
                    warn!("对话模型「{}」出错：{}", llm.model, format!("{err:#}").chars().take(200).collect::<String>());
                    if ran_tools {
                        // A tool already acted (created a task, switched a light);
                        // re-running the turn on another model could do it twice.
                        return Err(err);
                    }
                    last_error = Some(err);
                }
            }
        }
        Err(last_error.unwrap_or_else(|| anyhow::anyhow!("没有可用的对话模型")))
    }

    async fn prompt_with(&self, llm: &LlmConfig, prompt: String) -> anyhow::Result<String> {
        // One client per model (kept for the process) so turns reuse the
        // TLS connection instead of handshaking with the provider every time.
        let http_client = {
            let mut clients = self.http_clients.lock().await;
            match clients.get(&llm.base_url) {
                Some(client) => client.clone(),
                None => {
                    let client = reqwest::Client::builder()
                        .timeout(timeout_duration(llm.timeout_s))
                        .pool_idle_timeout(std::time::Duration::from_secs(600))
                        .tcp_keepalive(std::time::Duration::from_secs(30))
                        .build()
                        .context("failed to create LLM HTTP client")?;
                    clients.insert(llm.base_url.clone(), client.clone());
                    client
                }
            }
        };

        match llm.protocol {
            LlmProtocol::OpenAiChat => self.prompt_openai(llm, http_client, prompt).await,
            LlmProtocol::Anthropic => self.prompt_anthropic(llm, http_client, prompt).await,
        }
    }

    async fn prompt_openai(
        &self,
        llm: &LlmConfig,
        http_client: reqwest::Client,
        prompt: String,
    ) -> anyhow::Result<String> {
        let client = openai::Client::builder()
            .api_key(llm.api_key.as_str())
            .base_url(llm.base_url.as_str())
            .http_client(http_client)
            .build()
            .context("failed to create OpenAI-compatible Rig client")?
            .completions_api();

        let agent = client
            .agent(llm.model.as_str())
            .preamble(&instructions(&self.config))
            .temperature(llm.temperature)
            .max_tokens(llm.max_tokens)
            .default_max_turns(MAX_TOOL_TURNS)
            .additional_params(openai_thinking_params(&llm.thinking.mode, &llm.base_url))
            .tool_server_handle(self.tool_server.clone())
            .build();

        stream_reply!(self, agent, prompt)
    }

    async fn prompt_anthropic(
        &self,
        llm: &LlmConfig,
        http_client: reqwest::Client,
        prompt: String,
    ) -> anyhow::Result<String> {
        if llm.thinking.mode == LlmThinkingMode::Enabled {
            anyhow::ensure!(
                llm.thinking.budget_tokens < llm.max_tokens,
                "llm.thinking.budget_tokens ({}) must be lower than llm.max_tokens ({})",
                llm.thinking.budget_tokens,
                llm.max_tokens
            );
        }

        let client = anthropic::Client::builder()
            .api_key(llm.api_key.as_str())
            .base_url(llm.base_url.as_str())
            .http_client(http_client)
            .anthropic_version("2023-06-01")
            .build()
            .context("failed to create Anthropic-compatible Rig client")?;

        let mut builder = client
            .agent(llm.model.as_str())
            .preamble(&instructions(&self.config))
            .max_tokens(llm.max_tokens)
            .default_max_turns(MAX_TOOL_TURNS)
            .additional_params(anthropic_thinking_params(
                &llm.thinking.mode,
                llm.thinking.budget_tokens,
            ));
        if llm.thinking.mode != LlmThinkingMode::Enabled {
            builder = builder.temperature(llm.temperature);
        }
        let agent = builder.tool_server_handle(self.tool_server.clone()).build();

        stream_reply!(self, agent, prompt)
    }

    /// Called when a voice session ends (wake word, idle, goodbye...). The
    /// conversation is remembered across sessions for MEMORY_WINDOW so a
    /// follow-up after re-waking ("小爱同学，换成 Claire") still has context;
    /// only an explicit goodbye forgets it right away.
    pub async fn reset_session(&self, reason: &str) {
        if reason == "agent ended conversation" {
            self.history.lock().await.clear();
            *self.last_turn.lock().await = None;
        }
        let mut control = self.control.lock().await;
        control.should_end = false;
        control.end_reason = reason.to_string();
        debug!("对话段结束（{reason}），记忆保留 {} 分钟", MEMORY_WINDOW.as_secs() / 60);
    }

    /// Current date and time plus the user's notes, so answers fit the moment
    /// and the person ("下午好", knowing they follow finance...).
    fn turn_context(&self) -> String {
        let tz: chrono_tz::Tz = self.config.agent.timezone.parse().unwrap_or(chrono_tz::Asia::Shanghai);
        let now = chrono::Utc::now().with_timezone(&tz);
        let weekday = ["一", "二", "三", "四", "五", "六", "日"][chrono::Datelike::weekday(&now).num_days_from_monday() as usize];
        let mut ctx = format!("（现在是 {} 星期{}）\n", now.format("%Y年%-m月%-d日 %H:%M"), weekday);
        if let Ok(memories) = self.memories.try_lock() {
            if !memories.trim().is_empty() {
                ctx.push_str(&format!("（你记得的关于用户的事，自然地用上，不要逐条念出来：\n{}）\n", memories.trim()));
            }
        }
        let notes = self.config.agent.profile_notes.trim();
        if !notes.is_empty() {
            ctx.push_str(&format!("（关于用户，供你参考、不要照念：{notes}）\n"));
        }
        ctx
    }

    async fn prompt_with_history(&self, message: &str) -> String {
        let context = self.turn_context();
        let expired = self
            .last_turn
            .lock()
            .await
            .map(|t| t.elapsed() > MEMORY_WINDOW)
            .unwrap_or(false);
        if expired {
            self.history.lock().await.clear();
        }
        let history = self.history.lock().await;
        if history.is_empty() {
            return format!("{context}{message}");
        }
        let mut prompt = format!("{context}最近对话历史：\n");
        for (role, text) in history.iter() {
            prompt.push_str(role);
            prompt.push('：');
            prompt.push_str(text);
            prompt.push('\n');
        }
        prompt.push_str("当前用户：");
        prompt.push_str(message);
        prompt
    }

    /// Fetch what the console remembers about the user (on wake; in the
    /// background, so it never delays the first answer).
    pub fn refresh_memories(&self) {
        let cfg = self.config.music.console.clone();
        if cfg.base_url.is_empty() {
            return;
        }
        let (client, memories) = (self.http.clone(), self.memories.clone());
        tokio::spawn(async move {
            let resp = client
                .get(format!("{}/api/device/memory", cfg.base_url.trim_end_matches('/')))
                .header("X-Device-Token", &cfg.token)
                .timeout(std::time::Duration::from_secs(3))
                .send()
                .await;
            if let Ok(resp) = resp {
                if resp.status().is_success() {
                    if let Ok(text) = resp.text().await {
                        *memories.lock().await = text;
                    }
                }
            }
        });
    }

    /// Hand a finished exchange to the console, which learns from it later.
    fn report_exchange(&self, user: &str, assistant: &str) {
        let cfg = self.config.music.console.clone();
        if cfg.base_url.is_empty() {
            return;
        }
        let client = self.http.clone();
        let body = json!({"user": user, "assistant": assistant});
        tokio::spawn(async move {
            let _ = client
                .post(format!("{}/api/device/memory/exchange", cfg.base_url.trim_end_matches('/')))
                .header("X-Device-Token", &cfg.token)
                .timeout(std::time::Duration::from_secs(5))
                .json(&body)
                .send()
                .await;
        });
    }

    async fn push_history(&self, user: &str, assistant: &str) {
        if assistant.contains("【忽略】") {
            return; // background chatter: neither context nor memory
        }
        self.report_exchange(user, assistant);
        let mut history = self.history.lock().await;
        history.push(("用户".to_string(), user.to_string()));
        history.push(("助手".to_string(), assistant.to_string()));
        let excess = history.len().saturating_sub(MEMORY_TURNS * 2);
        history.drain(..excess);
        *self.last_turn.lock().await = Some(std::time::Instant::now());
    }
}

fn openai_thinking_params(mode: &LlmThinkingMode, base_url: &str) -> Value {
    match mode {
        LlmThinkingMode::Auto => json!({}),
        // DeepSeek ignores the generic switches below and keeps thinking
        // (slower replies); it only honours `thinking: {type: disabled}`,
        // which other OpenAI-compatible APIs may reject, so it's DeepSeek-only.
        LlmThinkingMode::Disabled if base_url.contains("deepseek.com") => json!({
            "thinking": {"type": "disabled"}
        }),
        LlmThinkingMode::Disabled => json!({
            "reasoning_effort": "low",
            "enable_thinking": false,
            "chat_template_kwargs": {"enable_thinking": false}
        }),
        LlmThinkingMode::Enabled => json!({
            "enable_thinking": true,
            "chat_template_kwargs": {"enable_thinking": true}
        }),
    }
}

fn anthropic_thinking_params(mode: &LlmThinkingMode, budget_tokens: u64) -> Value {
    match mode {
        LlmThinkingMode::Auto => json!({}),
        LlmThinkingMode::Disabled => json!({"thinking": {"type": "disabled"}}),
        LlmThinkingMode::Enabled => {
            json!({"thinking": {"type": "enabled", "budget_tokens": budget_tokens}})
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{anthropic_thinking_params, openai_thinking_params};
    use crate::config::LlmThinkingMode;
    use serde_json::json;

    #[test]
    fn disables_thinking_for_anthropic_messages() {
        assert_eq!(
            anthropic_thinking_params(&LlmThinkingMode::Disabled, 1024),
            json!({"thinking": {"type": "disabled"}})
        );
    }

    #[test]
    fn enables_thinking_with_budget_for_anthropic_messages() {
        assert_eq!(
            anthropic_thinking_params(&LlmThinkingMode::Enabled, 2048),
            json!({"thinking": {"type": "enabled", "budget_tokens": 2048}})
        );
    }

    #[test]
    fn deepseek_gets_its_own_thinking_switch() {
        let params = openai_thinking_params(&LlmThinkingMode::Disabled, "https://api.deepseek.com/v1");
        assert_eq!(params["thinking"]["type"], "disabled");
        assert!(params.get("enable_thinking").is_none());
    }

    #[test]
    fn disables_thinking_for_openai_chat() {
        let params = openai_thinking_params(&LlmThinkingMode::Disabled, "https://api.openai.com/v1");
        assert_eq!(params["enable_thinking"], false);
        assert_eq!(params["chat_template_kwargs"]["enable_thinking"], false);
    }
}


/// Logs every tool the model calls, in Chinese, for the console's activity log,
/// and says "稍等，我查一下" once per turn when a slow (online) tool starts, so
/// the user hears a response within a second or two instead of silence.
/// Tool rounds one reply may take before rig gives up with MaxTurnsError.
const MAX_TOOL_TURNS: usize = 8;
/// Searches allowed per reply; later ones are skipped with "answer now".
const MAX_SEARCHES: usize = 5;
/// Any tool calls allowed per reply before the rest are skipped.
const MAX_CALLS_PER_TURN: usize = 10;

#[derive(Clone)]
struct ToolLogHook {
    turn_calls: Arc<std::sync::atomic::AtomicUsize>,
    turn_searches: Arc<std::sync::atomic::AtomicUsize>,
    device: Device,
    filler: Arc<Mutex<Option<tokio::task::JoinHandle<()>>>>,
    calls: Arc<std::sync::atomic::AtomicUsize>,
    sink: Arc<Mutex<Option<UnboundedSender<String>>>>,
}

/// How long a conversation is remembered after its last turn, and how many
/// recent exchanges are sent to the model.
const MEMORY_WINDOW: std::time::Duration = std::time::Duration::from_secs(600);
const MEMORY_TURNS: usize = 8;

const FILLER_TEXT: &str = "稍等，我查一下";
/// Tools that take a moment, and what to say at once so the user isn't left
/// in silence ("好嘞" before music starts, "稍等" before a search).
fn filler_for(tool: &str) -> Option<&'static str> {
    match tool {
        "web_search" | "get_weather" | "read_source_now" => Some(FILLER_TEXT),
        // Music needs no "好嘞": the sound itself arrives within a second
        // or two and saying it first only splits the reply in two.
        "play_native_content" => Some("好嘞。"),
        _ => None,
    }
}

fn shorten(text: &str, max: usize) -> String {
    let mut out: String = text.chars().take(max).collect();
    if text.chars().count() > max {
        out.push('…');
    }
    out
}

/// Chinese names for the built-in and console tools; MCP tools from other
/// servers keep their own names.
fn tool_label(name: &str) -> &str {
    match name {
        "get_current_time" => "查询时间",
        "get_weather" => "查询天气",
        "web_search" => "联网搜索",
        "end_conversation" => "结束对话",
        "control_smart_home" => "控制智能家居",
        "set_volume" => "调音量",
        "search_music_library" => "搜索音乐",
        "play_music_query" => "播放音乐",
        "play_radio" => "播放电台",
        "play_native_content" => "原生播放",
        "play_random_music" => "随机播放音乐",
        "add_music_to_play_queue" => "加入播放队列",
        "add_random_music_to_play_queue" => "随机加歌到队列",
        "stop_music_playback" => "停止播放",
        "pause_music_playback" => "暂停播放",
        "resume_music_playback" => "继续播放",
        "play_next_music" => "下一首",
        "play_previous_music" => "上一首",
        "get_music_status" => "查询播放状态",
        "request_music_login_code" => "发送音乐登录验证码",
        "submit_music_login_code" => "提交音乐登录验证码",
        "console_status" => "查询控制台状态",
        "list_model_profiles" => "列出模型档案",
        "switch_model" => "切换模型",
        "list_tasks" => "列出定时任务",
        "create_task" => "新建定时任务",
        "set_task_enabled" => "启停定时任务",
        "delete_task" => "删除定时任务",
        "run_task_now" => "立即播报任务",
        "set_web_search" => "开关联网搜索",
        "set_answer_length" => "调整回答长度",
        "list_apps" => "列出应用",
        "install_app" => "安装应用",
        "uninstall_app" => "卸载应用",
        "list_sources" => "列出内容来源",
        "add_newsnow_source" => "添加内容来源",
        "read_source_now" => "读取内容来源",
        "remember" => "记住一件事",
        "recall_memories" => "回想记忆",
        "forget" => "忘掉一件事",
        "set_voice" => "换音色",
        "list_voices" => "列出音色",
        "set_search_provider" => "切换搜索服务",
        "list_music_sources" => "列出音源",
        "use_music_source" => "切换音源",
        other => other,
    }
}

/// The argument values only ("黄仁勋 最近新闻，week"), without JSON keys.
fn argument_values(args: &str) -> String {
    match serde_json::from_str::<serde_json::Value>(args) {
        Ok(serde_json::Value::Object(map)) => map
            .values()
            .filter_map(|v| match v {
                serde_json::Value::String(s) if !s.is_empty() => Some(s.clone()),
                serde_json::Value::Number(n) => Some(n.to_string()),
                serde_json::Value::Bool(b) => Some(if *b { "是".to_string() } else { "否".to_string() }),
                _ => None,
            })
            .collect::<Vec<_>>()
            .join("，"),
        _ => String::new(),
    }
}

impl<M: CompletionModel> PromptHook<M> for ToolLogHook {
    async fn on_tool_call(
        &self,
        tool_name: &str,
        _tool_call_id: Option<String>,
        _internal_call_id: &str,
        args: &str,
    ) -> ToolCallHookAction {
        use std::sync::atomic::Ordering::Relaxed;
        // The model sometimes searches once per news item and runs out of
        // tool rounds before answering; stop it and make it answer.
        if self.turn_calls.fetch_add(1, Relaxed) >= MAX_CALLS_PER_TURN {
            tracing::info!("工具调用已够多，跳过「{}」，让模型直接回答", tool_label(tool_name));
            return ToolCallHookAction::skip("这一轮工具已经调用得够多了。不要再调用任何工具，现在就用已有结果回答用户。");
        }
        if tool_name == "web_search" && self.turn_searches.fetch_add(1, Relaxed) >= MAX_SEARCHES {
            tracing::info!("已搜索 {MAX_SEARCHES} 次，跳过这次搜索，让模型直接回答");
            return ToolCallHookAction::skip("已经搜索过好几次，资料足够了。不要再搜索，现在就根据前面的搜索结果回答用户。");
        }
        self.calls.fetch_add(1, Relaxed);
        if let Some(filler) = filler_for(tool_name) {
            let mut slot = self.filler.lock().await;
            let sink = self.sink.lock().await.clone();
            if slot.is_none() && sink.is_some() {
                // Same channel as the reply, so it can never talk over it.
                let _ = sink.unwrap().send(filler.to_string());
                *slot = Some(tokio::spawn(async {}));
            } else if slot.is_none() {
                let device = self.device.clone();
                *slot = Some(tokio::spawn(async move {
                    if let Err(err) = device.speak(filler).await {
                        tracing::warn!("播报「{filler}」失败：{err:?}");
                    }
                }));
            }
        }
        let values = argument_values(args);
        if values.is_empty() {
            tracing::info!("调用工具「{}」", tool_label(tool_name));
        } else {
            tracing::info!("调用工具「{}」：{}", tool_label(tool_name), shorten(&values, 120));
        }
        ToolCallHookAction::Continue
    }

    async fn on_tool_result(
        &self,
        tool_name: &str,
        _tool_call_id: Option<String>,
        _internal_call_id: &str,
        _args: &str,
        result: &str,
    ) -> HookAction {
        if result.starts_with("Toolset error") || result.contains("ToolCallError") {
            let reason = result.rsplit("ToolCallError: ").next().unwrap_or(result);
            tracing::warn!("工具「{}」出错：{}", tool_label(tool_name), shorten(reason, 160));
        }
        HookAction::Continue
    }
}

#[cfg(test)]
mod live_stream_tests {
    use super::*;
    use rig_core::client::CompletionClient;

    /// DS_KEY=... cargo test live_stream -- --ignored --nocapture
    #[tokio::test]
    #[ignore]
    async fn live_stream() {
        let client = openai::Client::builder()
            .api_key(std::env::var("DS_KEY").unwrap().as_str())
            .base_url("https://api.deepseek.com/v1")
            .build()
            .unwrap()
            .completions_api();
        let agent = client.agent("deepseek-chat").preamble("用中文口语回答，三句话。").build();
        let started = std::time::Instant::now();
        let mut stream = agent.stream_prompt("介绍一下杭州").multi_turn(3).await;
        let mut splitter = SentenceSplitter::default();
        while let Some(item) = stream.next().await {
            if let MultiTurnStreamItem::StreamAssistantItem(StreamedAssistantContent::Text(t)) = item.unwrap() {
                for piece in splitter.push(&t.text) {
                    println!("{:.2}s PIECE {piece}", started.elapsed().as_secs_f64());
                }
            }
        }
        println!("{:.2}s DONE {:?}", started.elapsed().as_secs_f64(), splitter.finish());
    }
}
