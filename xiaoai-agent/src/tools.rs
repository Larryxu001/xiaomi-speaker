use std::sync::Arc;

use anyhow::Context;
use chrono::Datelike;
use chrono_tz::Tz;
use rig_core::completion::ToolDefinition;
use rig_core::tool::Tool;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use tokio::sync::Mutex;

use crate::agent::AgentControl;
use crate::config::{timeout_duration, AppConfig};
use crate::music::MusicService;
use crate::weather::WeatherService;

#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct ToolCallError(String);

impl From<anyhow::Error> for ToolCallError {
    fn from(value: anyhow::Error) -> Self {
        Self(value.to_string())
    }
}

fn definition(name: &str, description: &str, properties: Value) -> ToolDefinition {
    ToolDefinition {
        name: name.to_string(),
        description: description.to_string(),
        parameters: json!({
            "type": "object",
            "properties": properties,
        }),
    }
}

fn as_json_text(value: Value) -> String {
    serde_json::to_string(&value).unwrap_or_else(|_| "{}".to_string())
}

#[derive(Clone)]
pub struct GetCurrentTime {
    config: Arc<AppConfig>,
}

impl GetCurrentTime {
    pub fn new(config: Arc<AppConfig>) -> Self {
        Self { config }
    }
}

#[derive(Debug, Deserialize)]
pub struct TimeArgs {
    #[serde(default)]
    timezone: String,
}

impl Tool for GetCurrentTime {
    const NAME: &'static str = "get_current_time";
    type Error = ToolCallError;
    type Args = TimeArgs;
    type Output = String;

    async fn definition(&self, _: String) -> ToolDefinition {
        definition(
            Self::NAME,
            "查询当前时间。timezone 可以传 IANA 时区名，例如 Asia/Shanghai。",
            json!({"timezone": {"type": "string"}}),
        )
    }

    async fn call(&self, args: Self::Args) -> Result<Self::Output, Self::Error> {
        let tz_name = if args.timezone.trim().is_empty() {
            self.config.agent.timezone.clone()
        } else {
            args.timezone
        };
        let tz: Tz = tz_name.parse().unwrap_or(chrono_tz::Asia::Shanghai);
        let now = chrono::Utc::now().with_timezone(&tz);
        Ok(as_json_text(json!({
            "timezone": tz.name(),
            "date": now.format("%Y-%m-%d").to_string(),
            "time": now.format("%H:%M:%S").to_string(),
            "weekday": format!("{:?}", now.weekday()),
            "iso": now.to_rfc3339(),
        })))
    }
}

#[derive(Clone)]
pub struct GetWeather {
    service: WeatherService,
}

impl GetWeather {
    pub fn new(config: Arc<AppConfig>) -> Self {
        Self {
            service: WeatherService::new(config),
        }
    }
}

#[derive(Debug, Deserialize)]
pub struct WeatherArgs {
    #[serde(default)]
    location: Option<String>,
}

impl Tool for GetWeather {
    const NAME: &'static str = "get_weather";
    type Error = ToolCallError;
    type Args = WeatherArgs;
    type Output = String;

    async fn definition(&self, _: String) -> ToolDefinition {
        definition(
            Self::NAME,
            "查询天气预报。location 可传 QWeather location，例如 114.1,30.52；留空按当前 IP 定位。",
            json!({"location": {"type": ["string", "null"]}}),
        )
    }

    async fn call(&self, args: Self::Args) -> Result<Self::Output, Self::Error> {
        Ok(as_json_text(
            self.service
                .get_weather(args.location.as_deref().unwrap_or(""))
                .await,
        ))
    }
}

#[derive(Clone)]
pub struct WebSearch {
    config: Arc<AppConfig>,
    client: reqwest::Client,
}

impl WebSearch {
    pub fn new(config: Arc<AppConfig>) -> Self {
        Self {
            config,
            client: reqwest::Client::new(),
        }
    }
}

#[derive(Debug, Deserialize)]
pub struct WebSearchArgs {
    query: String,
    #[serde(default)]
    topic: String,
    #[serde(default)]
    time_range: String,
    #[serde(default)]
    max_results: Option<usize>,
}

impl Tool for WebSearch {
    const NAME: &'static str = "web_search";
    type Error = ToolCallError;
    type Args = WebSearchArgs;
    type Output = String;

    async fn definition(&self, _: String) -> ToolDefinition {
        definition(
            Self::NAME,
            "联网搜索网页信息。用于用户询问新闻、最近发生的事、实时资料，或明确要求上网搜索时。简单问题 max_results 填 3；盘点、汇总、列出多条（比如一周十大新闻）时填 10。topic 可传 general 或 news；time_range 可传 day、week、month、year。",
            json!({
                "query": {"type": "string"},
                "topic": {"type": "string", "enum": ["general", "news"]},
                "time_range": {"type": "string", "enum": ["", "day", "week", "month", "year"]},
                "max_results": {"type": ["integer", "null"], "minimum": 1, "maximum": 10}
            }),
        )
    }

    async fn call(&self, args: Self::Args) -> Result<Self::Output, Self::Error> {
        let settings = &self.config.agent.web_search;
        if !settings.enabled {
            return Err(anyhow::anyhow!("agent.web_search.enabled is false").into());
        }
        if settings.api_key.trim().is_empty() {
            return Err(anyhow::anyhow!("agent.web_search.api_key is empty").into());
        }

        let query = args.query.trim();
        if query.is_empty() {
            return Err(anyhow::anyhow!("web search query is empty").into());
        }
        let topic = match args.topic.trim() {
            "news" => "news",
            _ => "general",
        };
        let max_results = args
            .max_results
            .unwrap_or(settings.max_results)
            .clamp(1, 10);
        let mut request = Map::new();
        request.insert("query".to_string(), json!(query));
        request.insert("topic".to_string(), json!(topic));
        request.insert(
            "search_depth".to_string(),
            json!(normalize_search_depth(&settings.search_depth)),
        );
        request.insert("max_results".to_string(), json!(max_results));
        request.insert("include_answer".to_string(), json!(true));
        request.insert("include_raw_content".to_string(), json!(false));
        request.insert("include_images".to_string(), json!(false));
        if matches!(args.time_range.trim(), "day" | "week" | "month" | "year") {
            request.insert(
                "time_range".to_string(),
                json!(args.time_range.trim().to_string()),
            );
        }

        let response = self
            .client
            .post(settings.tavily_url.trim())
            .bearer_auth(settings.api_key.trim())
            .json(&request)
            .timeout(timeout_duration(settings.timeout_s))
            .send()
            .await
            .context("Tavily search request failed")?;

        let status = response.status();
        let body = response
            .text()
            .await
            .context("failed to read Tavily search response")?;
        if !status.is_success() {
            return Err(anyhow::anyhow!(
                "Tavily search request failed with status {status}: {}",
                truncate_chars(&body, 300)
            )
            .into());
        }
        let data: Value =
            serde_json::from_str(&body).context("invalid Tavily search response JSON")?;
        Ok(as_json_text(summarize_tavily_response(
            query,
            data,
            max_results,
        )))
    }
}

fn normalize_search_depth(value: &str) -> &'static str {
    match value.trim() {
        "advanced" => "advanced",
        _ => "basic",
    }
}

fn summarize_tavily_response(query: &str, data: Value, max_results: usize) -> Value {
    let results = data
        .get("results")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .take(max_results)
                .map(|item| {
                    json!({
                        "title": item.get("title").and_then(Value::as_str).unwrap_or(""),
                        "url": item.get("url").and_then(Value::as_str).unwrap_or(""),
                        "content": truncate_chars(
                            item.get("content").and_then(Value::as_str).unwrap_or(""),
                            500,
                        ),
                        "score": item.get("score"),
                        "published_date": item.get("published_date"),
                    })
                })
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    json!({
        "query": query,
        "answer": data.get("answer").and_then(Value::as_str).unwrap_or(""),
        "results": results,
    })
}

fn truncate_chars(text: &str, max_chars: usize) -> String {
    let mut output = String::new();
    for ch in text.trim().chars().take(max_chars) {
        output.push(ch);
    }
    if text.trim().chars().count() > max_chars {
        output.push_str("...");
    }
    output
}

#[derive(Clone)]
pub struct EndConversation {
    control: Arc<Mutex<AgentControl>>,
}

impl EndConversation {
    pub fn new(control: Arc<Mutex<AgentControl>>) -> Self {
        Self { control }
    }
}

#[derive(Debug, Deserialize)]
pub struct EndArgs {
    #[serde(default)]
    reason: String,
}

impl Tool for EndConversation {
    const NAME: &'static str = "end_conversation";
    type Error = ToolCallError;
    type Args = EndArgs;
    type Output = String;

    async fn definition(&self, _: String) -> ToolDefinition {
        definition(
            Self::NAME,
            "当用户表示再见、拜拜、不用帮忙了、结束对话、先这样时，结束当前音箱对话 session。",
            json!({"reason": {"type": "string"}}),
        )
    }

    async fn call(&self, args: Self::Args) -> Result<Self::Output, Self::Error> {
        let mut control = self.control.lock().await;
        control.should_end = true;
        control.end_reason = args.reason.clone();
        Ok(as_json_text(json!({
            "should_end": true,
            "reason": if args.reason.is_empty() { "user ended the conversation" } else { &args.reason },
        })))
    }
}

macro_rules! music_tool {
    ($tool:ident, $args:ty, $name:literal, $desc:literal, $schema:expr, $body:expr) => {
        #[derive(Clone)]
        pub struct $tool {
            music: Arc<MusicService>,
        }

        impl $tool {
            pub fn new(music: Arc<MusicService>) -> Self {
                Self { music }
            }
        }

        impl Tool for $tool {
            const NAME: &'static str = $name;
            type Error = ToolCallError;
            type Args = $args;
            type Output = String;

            async fn definition(&self, _: String) -> ToolDefinition {
                definition(Self::NAME, $desc, $schema)
            }

            async fn call(&self, args: Self::Args) -> Result<Self::Output, Self::Error> {
                let value = ($body)(self.music.clone(), args).await;
                Ok(as_json_text(value))
            }
        }
    };
}

#[derive(Debug, Deserialize)]
pub struct SearchMusicArgs {
    query: String,
    #[serde(default = "default_limit")]
    limit: usize,
}

fn default_limit() -> usize {
    5
}

music_tool!(
    SearchMusic,
    SearchMusicArgs,
    "search_music_library",
    "搜索音乐曲库，返回候选歌曲。用于用户问有没有某首歌、想听某歌手或需要先确认版本时。",
    json!({
        "query": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20}
    }),
    |music: Arc<MusicService>, args: SearchMusicArgs| async move {
        music.search(&args.query, args.limit).await
    }
);

#[derive(Debug, Deserialize)]
pub struct EmptyArgs {}

music_tool!(
    RequestMusicLoginCode,
    EmptyArgs,
    "request_music_login_code",
    "向配置的网易云手机号发送短信验证码。用户想登录网易云、授权音乐会员或获取验证码时调用。",
    json!({}),
    |music: Arc<MusicService>, _args: EmptyArgs| async move { music.request_login_code().await }
);

#[derive(Debug, Deserialize)]
pub struct LoginCodeArgs {
    code: String,
}

music_tool!(
    SubmitMusicLoginCode,
    LoginCodeArgs,
    "submit_music_login_code",
    "提交用户报出的网易云短信验证码，完成登录；cookie 只在本次进程内保存，不写磁盘。",
    json!({"code": {"type": "string"}}),
    |music: Arc<MusicService>, args: LoginCodeArgs| async move {
        music.submit_login_code(&args.code).await
    }
);

#[derive(Debug, Deserialize)]
pub struct PlayMusicArgs {
    #[serde(default)]
    query: String,
    #[serde(default)]
    song_id: String,
}

music_tool!(
    PlayMusicQuery,
    PlayMusicArgs,
    "play_music_query",
    "播放歌曲（不含电台，电台用 play_radio）。可以传自然语言查询，例如 周杰伦 晴天；也可以传 search_music_library 返回的 song_id。",
    json!({
        "query": {"type": "string"},
        "song_id": {"type": "string"}
    }),
    |music: Arc<MusicService>, args: PlayMusicArgs| async move { music.play_query(&args.query, &args.song_id).await }
);

#[derive(Debug, Deserialize)]
pub struct PlayRadioArgs {
    #[serde(default)]
    query: String,
}

music_tool!(
    PlayRadio,
    PlayRadioArgs,
    "play_radio",
    "播放网络电台/广播。用户说打开电台、放个电台、听广播、放中国之声、放点爵士电台时调用。query 填台名或心情（中国之声、经济之声、爵士、放松、BBC），只说「打开电台」就留空，会放用户最常听、最喜欢的台。不要用歌曲播放工具放电台。换一台用 play_next_music。",
    json!({"query": {"type": "string"}}),
    |music: Arc<MusicService>, args: PlayRadioArgs| async move { music.play_radio(&args.query).await }
);

#[derive(Debug, Deserialize)]
pub struct PlayNativeArgs {
    request: String,
}

music_tool!(
    PlayNative,
    PlayNativeArgs,
    "play_native_content",
    "交给小爱原生服务播放有声内容：喜马拉雅、得到、蜻蜓FM、小宇宙播客、有声书、讲故事/儿童故事、相声评书、新闻节目等。\
     用户点名这些平台，或者要听故事、有声书、相声、某个播客/节目时调用。request 写成一句完整的播放指令，带上平台名（例如「播放喜马拉雅的三体」「在得到上播放薛兆丰的经济学课」「播放小宇宙的忽左忽右」「讲一个儿童故事」）。\
     调用后音箱直接开始播放，你不用再说话。普通的歌曲用 play_music_query，网络电台用 play_radio，不要用这个。播放中的暂停、继续、停止、下一集用平常的音乐控制工具即可。",
    json!({"request": {"type": "string"}}),
    |music: Arc<MusicService>, args: PlayNativeArgs| async move { music.play_native(&args.request).await }
);

#[derive(Debug, Deserialize)]
pub struct AddMusicArgs {
    #[serde(default)]
    queries: String,
    #[serde(default)]
    song_ids: String,
}

music_tool!(
    AddMusicToQueue,
    AddMusicArgs,
    "add_music_to_play_queue",
    "把一个或多个指定歌曲加入当前播放队列，不打断正在播放的歌曲。queries 用换行或分号分隔；song_ids 也用换行或分号分隔。",
    json!({
        "queries": {"type": "string"},
        "song_ids": {"type": "string"}
    }),
    |music: Arc<MusicService>, args: AddMusicArgs| async move { music.add_to_queue(&args.queries, &args.song_ids).await }
);

#[derive(Debug, Deserialize)]
pub struct RandomMusicArgs {
    #[serde(default = "default_random_count")]
    count: usize,
}

fn default_random_count() -> usize {
    10
}

music_tool!(
    AddRandomMusicToQueue,
    RandomMusicArgs,
    "add_random_music_to_play_queue",
    "从当前音乐库随机选择指定数量的歌曲加入播放队列，不打断正在播放的歌曲。",
    json!({"count": {"type": "integer", "minimum": 1, "maximum": 100}}),
    |music: Arc<MusicService>, args: RandomMusicArgs| async move { music.add_random(args.count).await }
);

music_tool!(
    PlayRandomMusic,
    RandomMusicArgs,
    "play_random_music",
    "从当前音乐库随机选择歌曲，替换当前队列，并立即开始播放。用于用户说放点音乐、随便放点歌、随机播放。",
    json!({"count": {"type": "integer", "minimum": 1, "maximum": 100}}),
    |music: Arc<MusicService>, args: RandomMusicArgs| async move { music.play_random(args.count).await }
);

music_tool!(
    StopMusicPlayback,
    EmptyArgs,
    "stop_music_playback",
    "停止当前音乐播放并清空播放队列。用于用户说停止、别放了、关掉音乐、清空队列。",
    json!({}),
    |music: Arc<MusicService>, _args: EmptyArgs| async move { music.stop().await }
);

music_tool!(
    PauseMusicPlayback,
    EmptyArgs,
    "pause_music_playback",
    "暂停当前音乐播放，但保留当前歌曲和播放队列。用于用户说暂停、停一下、先暂停。",
    json!({}),
    |music: Arc<MusicService>, _args: EmptyArgs| async move { music.pause().await }
);

music_tool!(
    ResumeMusicPlayback,
    EmptyArgs,
    "resume_music_playback",
    "继续播放刚才暂停的音乐。用于用户说继续播放、恢复播放、接着放。",
    json!({}),
    |music: Arc<MusicService>, _args: EmptyArgs| async move { music.resume().await }
);

music_tool!(
    PlayNextMusic,
    EmptyArgs,
    "play_next_music",
    "播放队列里的下一首歌。用于用户说下一首、换一首、跳过这首。",
    json!({}),
    |music: Arc<MusicService>, _args: EmptyArgs| async move { music.next().await }
);

music_tool!(
    PlayPreviousMusic,
    EmptyArgs,
    "play_previous_music",
    "播放历史里的上一首歌。用于用户说上一首、回到刚才那首。",
    json!({}),
    |music: Arc<MusicService>, _args: EmptyArgs| async move { music.previous().await }
);

music_tool!(
    GetMusicStatus,
    EmptyArgs,
    "get_music_status",
    "查询音箱现在是否在出声，以及当前歌曲信息。native_playing 为真表示在放的是闹钟、原生早报或喜马拉雅这类不是我们放的内容，用户要停就照样调用停止播放工具。",
    json!({}),
    |music: Arc<MusicService>, _args: EmptyArgs| async move { music.status().await }
);

#[allow(dead_code)]
#[derive(Debug, Serialize)]
struct ToolOutput(Value);

/// Smart-home control through XiaoAI's own cloud brain (deploy/home.sh):
/// the Mi Home devices bound to the speaker's Xiaomi account keep working
/// even though conversations now go to the agent's own model.
#[derive(Clone, Default)]
pub struct ControlSmartHome;

#[derive(Debug, Deserialize)]
pub struct SmartHomeArgs {
    command: String,
}

impl Tool for ControlSmartHome {
    const NAME: &'static str = "control_smart_home";
    type Error = ToolCallError;
    type Args = SmartHomeArgs;
    type Output = String;

    async fn definition(&self, _: String) -> ToolDefinition {
        definition(
            Self::NAME,
            "控制米家智能家居，或查询设备状态。command 用一句完整、自然的中文指令，就像对原生小爱同学说的那样，\
             例如「打开客厅灯」「空调调到26度」「扫地机开始扫地」「卧室灯开着吗」「关掉所有灯」。\
             用户要求开关、调节、查询家里的灯、空调、窗帘、插座、扫地机等设备，或设置原生闹钟时调用。\
             返回的是小爱云端的执行结果，请据此简短告诉用户。",
            json!({"command": {"type": "string"}}),
        )
    }

    async fn call(&self, args: Self::Args) -> Result<Self::Output, Self::Error> {
        let script = format!(
            "/data/open-xiaoai/home.sh {}",
            shell_words::quote(args.command.trim())
        );
        let result = crate::shell::run_shell(&script)
            .await
            .map_err(|err| ToolCallError(err.to_string()))?;
        let text = result
            .stdout
            .lines()
            .map(|line| {
                serde_json::from_str::<String>(&format!("\"{line}\"")).unwrap_or_else(|_| line.to_string())
            })
            .collect::<Vec<_>>()
            .join("；");
        if text.trim().is_empty() {
            return Err(ToolCallError("小爱的云端没有返回结果".to_string()));
        }
        Ok(text)
    }
}


/// Speaker volume through the firmware's media player (0-100).
#[derive(Clone, Default)]
pub struct SetVolume;

#[derive(Debug, Deserialize)]
pub struct VolumeArgs {
    /// "up", "down", "set" or "get".
    action: String,
    #[serde(default)]
    value: Option<i64>,
}

pub async fn current_volume() -> Option<i64> {
    let out = crate::shell::run_shell("ubus call mediaplayer player_get_play_status").await.ok()?;
    let idx = out.stdout.find("volume")?;
    out.stdout[idx..]
        .chars()
        .skip_while(|c| !c.is_ascii_digit())
        .take_while(|c| c.is_ascii_digit())
        .collect::<String>()
        .parse()
        .ok()
}

/// Changes the speaker volume by `delta`; returns the new level.
pub async fn step_volume(delta: i64) -> Option<i64> {
    let target = (current_volume().await.unwrap_or(50) + delta).clamp(5, 100);
    let script = format!(
        "ubus call mediaplayer player_set_volume '{{\"volume\":{target},\"media\":\"app_ios\"}}'"
    );
    crate::shell::run_shell(&script).await.ok()?;
    current_volume().await
}

impl Tool for SetVolume {
    const NAME: &'static str = "set_volume";
    type Error = ToolCallError;
    type Args = VolumeArgs;
    type Output = String;

    async fn definition(&self, _: String) -> ToolDefinition {
        definition(
            Self::NAME,
            "调节或查询音箱音量（0 到 100）。用户说「声音大点/小点」「音量调到三十」「现在音量多少」时调用。\
             action：up 调大、down 调小、set 设为 value、get 查询。up/down 时 value 可省略（默认 10），\
             用户说「大一点点」用 5，「大很多」用 20。不要用智能家居工具调音量。",
            json!({
                "action": {"type": "string", "enum": ["up", "down", "set", "get"]},
                "value": {"type": "integer"}
            }),
        )
    }

    async fn call(&self, args: Self::Args) -> Result<Self::Output, Self::Error> {
        let now = current_volume().await.unwrap_or(50);
        let target = match args.action.as_str() {
            "get" => return Ok(format!("当前音量 {now}")),
            "up" => now + args.value.unwrap_or(10).abs(),
            "down" => now - args.value.unwrap_or(10).abs(),
            "set" => args.value.ok_or_else(|| ToolCallError("没有说要调到多少".to_string()))?,
            other => return Err(ToolCallError(format!("不认识的操作：{other}"))),
        }
        .clamp(5, 100);
        let script = format!(
            "ubus call mediaplayer player_set_volume '{{\"volume\":{target},\"media\":\"app_ios\"}}'"
        );
        let result = crate::shell::run_shell(&script)
            .await
            .map_err(|err| ToolCallError(err.to_string()))?;
        if result.exit_code != 0 {
            return Err(ToolCallError(format!("调音量失败：{}", result.stderr.trim())));
        }
        let after = current_volume().await.unwrap_or(target);
        Ok(format!("音量从 {now} 调到了 {after}"))
    }
}
