use std::sync::Arc;

use anyhow::Context;
use rand::RngCore;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::sync::Mutex;
use tracing::{debug, warn};
use url::Url;

use crate::config::{require_music_enabled, timeout_duration, AppConfig};
use crate::device::Device;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Song {
    pub id: String,
    pub name: String,
    pub artists: Vec<String>,
    pub album: String,
    pub duration_ms: Option<u64>,
    pub fee: Option<Value>,
}

#[derive(Debug, Clone, Serialize)]
struct QueueItem {
    song: Song,
    url: String,
}

#[derive(Debug, Default)]
struct MusicState {
    current: Option<QueueItem>,
    queue: Vec<QueueItem>,
    history: Vec<QueueItem>,
    paused: bool,
    interruption: Option<InterruptionAction>,
    /// When the current track was started (the end watcher ignores the first seconds).
    started_at: Option<std::time::Instant>,
    /// Speaker volume as last read (for the console's player bar).
    volume: Option<i64>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum InterruptionAction {
    Pause,
    Duck,
    None,
}

pub struct MusicService {
    config: Arc<AppConfig>,
    device: Device,
    client: Client,
    state: Mutex<MusicState>,
    netease_cookie: Mutex<Option<String>>,
    /// The console's note about the last URL ("preview only"), for the reply.
    last_note: Mutex<Option<String>>,
    /// Native content just started in this turn: the reply stays silent.
    native_started: std::sync::atomic::AtomicBool,
    /// Music is turned down while the mic stays open after a command.
    listening_duck: std::sync::atomic::AtomicBool,
}

impl MusicService {
    pub fn new(config: Arc<AppConfig>, device: Device) -> anyhow::Result<Self> {
        Ok(Self {
            config,
            device,
            client: Client::new(),
            state: Mutex::new(MusicState::default()),
            netease_cookie: Mutex::new(None),
            last_note: Mutex::new(None),
            native_started: std::sync::atomic::AtomicBool::new(false),
            listening_duck: std::sync::atomic::AtomicBool::new(false),
        })
    }

    pub async fn search(&self, query: &str, limit: usize) -> Value {
        match self.search_inner(query.trim(), limit.clamp(1, 20)).await {
            Ok(songs) => json!(songs),
            Err(err) => json!({"error": err.to_string()}),
        }
    }

    pub async fn request_login_code(&self) -> Value {
        match self
            .netease_request("/captcha/sent", &[("phone", self.netease_phone())])
            .await
        {
            Ok(data) => data,
            Err(err) => json!({"error": err.to_string()}),
        }
    }

    pub async fn submit_login_code(&self, code: &str) -> Value {
        let phone = self.netease_phone();
        let verified = self
            .netease_request(
                "/captcha/verify",
                &[("phone", phone.clone()), ("captcha", code.to_string())],
            )
            .await;
        if let Err(err) = verified {
            return json!({"error": err.to_string(), "verified": false});
        }
        let data = self
            .netease_request(
                "/login/cellphone",
                &[("phone", phone), ("captcha", code.to_string())],
            )
            .await;
        match data {
            Ok(data) => {
                self.remember_cookie_from_response(&data).await;
                data
            }
            Err(err) => json!({"error": err.to_string(), "verified": true}),
        }
    }

    /// Hands a content request (喜马拉雅、得到、蜻蜓 FM、小宇宙、故事、相声…)
    /// to XiaoAI's own cloud brain as text, which plays it with the licences
    /// bound to the user's Xiaomi account. Our own playback stops first, and
    /// from then on pause/stop/next/wake treat it like our own music.
    pub async fn play_native(&self, request: &str) -> Value {
        let request = request.trim();
        if request.is_empty() {
            return json!({"error": "没有说要听什么"});
        }
        if self.has_current().await {
            self.stop().await;
        } else {
            let _ = self.device.stop_audio().await;
        }
        let cmd = format!("/data/open-xiaoai/home.sh {}", shell_quote(request));
        let said = match crate::shell::run_shell(&cmd).await {
            Ok(out) => unescape_unicode(out.stdout.trim()),
            Err(err) => return json!({"error": err.to_string()}),
        };
        let mut started = false;
        for _ in 0..8 {
            if native_status().await == Some(1) {
                started = true;
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        }
        if !started {
            return json!({"playing": false, "native_reply": said,
                          "note": "小宇宙播客原生不支持；得到需要在小爱音箱 app 里绑定得到账号",
                          "hint": "原生小爱没有开始播放，把 native_reply 的意思简短告诉用户"});
        }
        // It plays right away; the turn ends without a spoken reply.
        self.native_started.store(true, std::sync::atomic::Ordering::SeqCst);
        mark_native_playing(true);
        let title = said
            .lines()
            .next()
            .and_then(|l| l.split("播放").nth(1))
            .map(|t| t.split(['，', ',', '。']).next().unwrap_or(t).trim().to_string())
            .filter(|t| !t.is_empty())
            .unwrap_or_else(|| request.to_string());
        let mut state = self.state.lock().await;
        state.current = Some(QueueItem {
            song: Song {
                id: format!("native:{request}"),
                name: title.clone(),
                artists: vec!["小爱原生".to_string()],
                album: String::new(),
                duration_ms: None,
                fee: None,
            },
            url: String::new(),
        });
        state.queue.clear();
        state.history.clear();
        state.paused = false;
        state.interruption = None;
        state.started_at = Some(std::time::Instant::now());
        json!({"playing": true, "title": title, "native_reply": said,
               "hint": "已经开始播放，本轮不用再说话"})
    }

    pub fn native_started(&self) -> bool {
        self.native_started.load(std::sync::atomic::Ordering::SeqCst)
    }

    pub fn take_native_started(&self) -> bool {
        self.native_started.swap(false, std::sync::atomic::Ordering::SeqCst)
    }

    async fn native_now(&self) -> bool {
        self.state.lock().await.current.as_ref().is_some_and(|i| i.song.id.starts_with("native:"))
    }

    pub async fn play_query(&self, query: &str, song_id: &str) -> Value {
        self.play_found(query, song_id, false).await
    }

    /// Radio is its own module on the console: stations are searched there
    /// (empty query = the stations the user likes most), never among songs.
    pub async fn play_radio(&self, query: &str) -> Value {
        self.play_found(query, "", true).await
    }

    async fn play_found(&self, query: &str, song_id: &str, radio: bool) -> Value {
        // A search plays its best match and queues the rest, so "换一台" /
        // "下一首" continue with similar stations or songs.
        let mut alternatives = Vec::new();
        let resolved = if song_id.trim().is_empty() {
            let found = if radio {
                self.search_radio(query.trim()).await
            } else {
                self.search_inner(query.trim(), 6).await
            };
            match found {
                Ok(songs) if !songs.is_empty() => {
                    // The best match may be unplayable (members-only, taken
                    // down): fall through to the next candidates.
                    let mut found = None;
                    let mut last_err = None;
                    let mut rest = songs.into_iter();
                    for song in rest.by_ref() {
                        match self.song_url(&song.id).await {
                            Ok(url) => {
                                found = Some(QueueItem { song, url });
                                break;
                            }
                            Err(err) => last_err = Some(err),
                        }
                    }
                    // Other versions of the same song (remix, cover) are not
                    // "next"; other songs (an artist or mood search) are.
                    let played = found.as_ref().map(|item| title_key(&item.song.name)).unwrap_or_default();
                    let is_station = found
                        .as_ref()
                        .is_some_and(|item| item.song.artists.iter().any(|a| a.contains("电台")));
                    alternatives = rest
                        .filter(|_| is_station) // "换一台" needs other stations; songs are recommended later
                        .filter(|song| {
                            let key = title_key(&song.name);
                            played.is_empty() || !(key.contains(&played) || played.contains(&key))
                        })
                        .collect::<Vec<_>>();
                    found.ok_or_else(|| last_err.unwrap_or_else(|| anyhow::anyhow!("no playable songs")))
                }
                Ok(_) => Err(anyhow::anyhow!("no songs found")),
                Err(err) => Err(err),
            }
        } else {
            self.resolve_one(query, song_id).await
        };
        match resolved {
            Ok(item) => {
                if self.native_now().await {
                    native_op("stop").await;
                    self.state.lock().await.current = None;
                }
                let play = self.device.play_url(&item.url).await;
                match play {
                    Ok(()) => {
                        let queue = alternatives
                            .into_iter()
                            .map(|song| QueueItem { song, url: String::new() })
                            .collect();
                        let mut state = self.state.lock().await;
                        state.started_at = Some(std::time::Instant::now());
                        state.interruption = None;
                        state.current = Some(item.clone());
                        state.queue = queue;
                        state.history.clear();
                        state.paused = false;
                        let note = self.last_note.lock().await.take().unwrap_or_default();
                        json!({"playing": true, "song": item.song, "note": note,
                               "hint": "确认时歌手名用 song.artists 里的真实歌手；有 note 时如实说一句；放完会自动推荐下一首"})
                    }
                    Err(err) => json!({"error": err.to_string(), "song": item.song}),
                }
            }
            Err(err) => json!({"error": err.to_string()}),
        }
    }

    pub async fn add_to_queue(&self, queries: &str, song_ids: &str) -> Value {
        let mut added = Vec::new();
        for id in split_many(song_ids) {
            if let Ok(item) = self.resolve_one("", &id).await {
                added.push(item);
            }
        }
        for query in split_many(queries) {
            if let Ok(item) = self.resolve_one(&query, "").await {
                added.push(item);
            }
        }
        if added.is_empty() {
            return json!({"error": "no playable songs resolved"});
        }
        let start_now;
        {
            let mut state = self.state.lock().await;
            start_now = state.current.is_none();
            state.queue.extend(added.clone());
        }
        if start_now {
            if let Err(err) = self.play_next_from_queue().await {
                return json!({"error": err.to_string()});
            }
        }
        json!({"added": added.iter().map(|item| &item.song).collect::<Vec<_>>()})
    }

    pub async fn add_random(&self, count: usize) -> Value {
        match self.random_queue_items(count).await {
            Ok(items) => {
                let mut state = self.state.lock().await;
                let start_now = state.current.is_none();
                state.queue.extend(items.clone());
                drop(state);
                if start_now {
                    if let Err(err) = self.play_next_from_queue().await {
                        return json!({"error": err.to_string()});
                    }
                }
                json!({"added": items.iter().map(|item| &item.song).collect::<Vec<_>>()})
            }
            Err(err) => json!({"error": err.to_string()}),
        }
    }

    pub async fn play_random(&self, count: usize) -> Value {
        match self.random_queue_items(count).await {
            Ok(items) => {
                {
                    let mut state = self.state.lock().await;
                    state.current = None;
                    state.history.clear();
                    state.queue = items;
                    state.paused = false;
                    state.interruption = None;
                }

                match self.play_next_from_queue().await {
                    Ok(item) => {
                        json!({
                            "playing": true,
                            "song": item.song,
                        })
                    }
                    Err(err) => json!({"error": err.to_string()}),
                }
            }
            Err(err) => json!({"error": err.to_string()}),
        }
    }

    pub async fn next(&self) -> Value {
        if self.native_now().await {
            native_op("next").await;
            return json!({"playing": true, "note": "原生小爱换到了下一集/下一首"});
        }
        // Nothing lined up: pick something like the current song.
        if self.state.lock().await.queue.is_empty() {
            self.refill_queue().await;
        }
        match self.play_next_from_queue().await {
            Ok(item) => json!({"playing": true, "song": item.song}),
            Err(err) => json!({"error": err.to_string()}),
        }
    }

    pub async fn previous(&self) -> Value {
        if self.native_now().await {
            native_op("prev").await;
            return json!({"playing": true});
        }
        match self.play_previous_from_history().await {
            Ok(item) => json!({"playing": true, "song": item.song}),
            Err(err) => json!({"error": err.to_string()}),
        }
    }

    pub async fn pause(&self) -> Value {
        if !self.has_current().await {
            // Something else is audible (a story, an announcement): pause it
            // rather than telling the user nothing is playing.
            if native_status().await == Some(1) {
                native_op("pause").await;
                return json!({"paused": true, "note": "暂停了音箱正在播的内容"});
            }
            self.device.abort_current_output().await;
            return json!({"paused": false, "error": "no current song"});
        }
        let paused = if self.native_now().await { native_op("pause").await; Ok(()) } else { self.device.pause_audio().await };
        match paused {
            Ok(()) => {
                let mut state = self.state.lock().await;
                state.paused = true;
                // Asked to pause: don't resume it when the conversation ends.
                state.interruption = None;
                json!({"paused": true})
            }
            Err(err) => json!({"paused": false, "error": err.to_string()}),
        }
    }

    pub async fn resume(&self) -> Value {
        if !self.has_current().await {
            // Nothing paused: continue with something like the last song.
            let last = self.state.lock().await.history.last().cloned();
            if let Some(last) = last {
                self.state.lock().await.current = Some(last);
                self.refill_queue().await;
                return match self.play_next_from_queue().await {
                    Ok(item) => json!({"playing": true, "song": item.song, "note": "接着放了一首相似的歌"}),
                    Err(err) => json!({"error": err.to_string()}),
                };
            }
            return json!({"playing": false, "error": "还没有放过歌，问用户想听什么"});
        }
        let resumed = if self.native_now().await { native_op("play").await; Ok(()) } else { self.device.resume_audio().await };
        match resumed {
            Ok(()) => {
                self.state.lock().await.paused = false;
                json!({"playing": true})
            }
            Err(err) => json!({"playing": false, "error": err.to_string()}),
        }
    }

    /// Stops everything audible, whatever started it: our own music, the
    /// firmware's player (stories, alarms, content it resumed by itself) and
    /// any announcement being spoken. Never conditional on our own state —
    /// "停止" has to mean stop even when we did not start the sound.
    pub async fn stop(&self) -> Value {
        self.listening_duck.store(false, std::sync::atomic::Ordering::SeqCst);
        mark_native_playing(false);
        self.restore_after_interruption().await;
        let stopped = self.device.stop_audio().await;
        // "stop", not "pause": a paused item is resumed by the firmware after
        // the next announcement, which is how a story started by itself.
        native_op("stop").await;
        self.device.abort_current_output().await;
        self.cancel_announcements().await;
        let mut state = self.state.lock().await;
        state.current = None;
        state.queue.clear();
        state.history.clear();
        state.paused = false;
        state.interruption = None;
        drop(state);
        match stopped {
            Ok(()) => json!({"stopped": true}),
            Err(err) => json!({"stopped": false, "error": err.to_string()}),
        }
    }

    pub async fn interrupt_for_wake(&self) -> bool {
        // Content the firmware plays (a story, something it resumed) must go
        // quiet while the user talks, even though we did not start it.
        if !self.has_track().await && native_status().await == Some(1) {
            native_op("pause").await;
            return true;
        }
        let native = self.native_now().await;
        let mode = if native { InterruptionAction::Pause } else { self.interruption_mode() };
        {
            let state = self.state.lock().await;
            if state.interruption.is_some() {
                return true;
            }
            if state.current.is_none() || state.paused {
                return false;
            }
        }

        match mode {
            InterruptionAction::Pause if native => native_op("pause").await,
            InterruptionAction::Pause => {
                if let Err(err) = self.device.pause_audio().await {
                    warn!("唤醒时暂停音乐失败：{err:?}");
                    return false;
                }
            }
            InterruptionAction::Duck => {
                if let Err(err) = self.device.duck_audio().await {
                    warn!("唤醒时调低音乐音量失败：{err:?}");
                    return false;
                }
            }
            InterruptionAction::None => {}
        }

        let mut state = self.state.lock().await;
        if state.current.is_none() {
            return false;
        }
        if mode == InterruptionAction::Pause {
            state.paused = true;
        }
        state.interruption = Some(mode);
        true
    }

    pub async fn restore_after_interruption(&self) {
        let action = {
            let mut state = self.state.lock().await;
            state.interruption.take()
        };
        match action {
            Some(InterruptionAction::Pause) if self.native_now().await => {
                native_op("play").await;
                let mut state = self.state.lock().await;
                if state.current.is_some() {
                    state.paused = false;
                }
            }
            Some(InterruptionAction::Pause) => {
                if let Err(err) = self.device.resume_audio().await {
                    warn!("对话结束后恢复音乐失败：{err:?}");
                    return;
                }
                let mut state = self.state.lock().await;
                if state.current.is_some() {
                    state.paused = false;
                }
            }
            Some(InterruptionAction::Duck) => {
                if let Err(err) = self.device.unduck_audio().await {
                    warn!("对话结束后恢复音乐音量失败：{err:?}");
                }
            }
            Some(InterruptionAction::None) | None => {}
        }
    }

    pub async fn status(&self) -> Value {
        let state = self.state.lock().await;
        json!({
            "playing": state.current.is_some(),
            "paused": state.paused,
            "interrupted": state.interruption.is_some(),
            "current": state.current.as_ref().map(|item| &item.song),
            "queue": state.queue.iter().take(10).map(|item| &item.song).collect::<Vec<_>>(),
        })
    }

    async fn resolve_one(&self, query: &str, song_id: &str) -> anyhow::Result<QueueItem> {
        let song = if !song_id.trim().is_empty() {
            self.song_detail(song_id.trim()).await?
        } else {
            self.search_inner(query, 1)
                .await?
                .into_iter()
                .next()
                .context("no songs found")?
        };
        let url = self.song_url(&song.id).await?;
        Ok(QueueItem { song, url })
    }

    async fn search_radio(&self, query: &str) -> anyhow::Result<Vec<Song>> {
        require_music_enabled(&self.config)?;
        if self.config.music.provider != "console" {
            anyhow::bail!("电台需要控制台音源");
        }
        let data = self
            .console_request("/api/device/music/radio", &[("q", query.to_string()), ("limit", "8".to_string())])
            .await?;
        Ok(serde_json::from_value(data)?)
    }

    async fn search_inner(&self, query: &str, limit: usize) -> anyhow::Result<Vec<Song>> {
        require_music_enabled(&self.config)?;
        match self.config.music.provider.as_str() {
            "netease" => self.search_netease(query, limit).await,
            "navidrome" => self.search_navidrome(query, limit).await,
            "console" => {
                let data = self
                    .console_request("/api/device/music/search", &[("q", query.to_string()), ("limit", limit.to_string())])
                    .await?;
                Ok(serde_json::from_value(data)?)
            }
            other => anyhow::bail!("Unsupported music provider: {other}"),
        }
    }

    async fn random_songs(&self, limit: usize) -> anyhow::Result<Vec<Song>> {
        require_music_enabled(&self.config)?;
        match self.config.music.provider.as_str() {
            "navidrome" => {
                let data = self
                    .navidrome_request("/rest/getRandomSongs.view", &[("size", limit.to_string())])
                    .await?;
                let songs = data
                    .get("randomSongs")
                    .and_then(|value| value.get("song"))
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                Ok(songs.iter().map(parse_navidrome_song).collect())
            }
            "console" => {
                let data = self
                    .console_request("/api/device/music/random", &[("limit", limit.to_string())])
                    .await?;
                Ok(serde_json::from_value(data)?)
            }
            _ => self.search_netease("", limit).await,
        }
    }

    async fn random_queue_items(&self, count: usize) -> anyhow::Result<Vec<QueueItem>> {
        // URLs are resolved when each track comes up (play_next_from_queue).
        let songs = self.random_songs(count.clamp(1, 100)).await?;
        anyhow::ensure!(!songs.is_empty(), "no random songs available");
        Ok(songs.into_iter().map(|song| QueueItem { song, url: String::new() }).collect())
    }

    async fn song_detail(&self, song_id: &str) -> anyhow::Result<Song> {
        match self.config.music.provider.as_str() {
            "console" => {
                let data = self
                    .console_request("/api/device/music/song", &[("id", song_id.to_string())])
                    .await?;
                Ok(serde_json::from_value(data)?)
            }
            "navidrome" => {
                let data = self
                    .navidrome_request("/rest/getSong.view", &[("id", song_id.to_string())])
                    .await?;
                let item = data.get("song").context("song detail not found")?;
                Ok(parse_navidrome_song(item))
            }
            _ => Ok(Song {
                id: song_id.to_string(),
                name: song_id.to_string(),
                artists: vec![],
                album: String::new(),
                duration_ms: None,
                fee: None,
            }),
        }
    }

    async fn song_url(&self, song_id: &str) -> anyhow::Result<String> {
        match self.config.music.provider.as_str() {
            "navidrome" => self.navidrome_stream_url(song_id),
            "console" => {
                let data = self
                    .console_request("/api/device/music/url", &[("id", song_id.to_string())])
                    .await?;
                let note = data.get("note").and_then(Value::as_str).unwrap_or_default();
                *self.last_note.lock().await = (!note.is_empty()).then(|| note.to_string());
                data.get("url")
                    .and_then(Value::as_str)
                    .map(ToString::to_string)
                    .context("song has no playable url")
            }
            _ => {
                let data = self
                    .netease_request(
                        "/song/url/v1",
                        &[
                            ("id", song_id.to_string()),
                            ("level", self.config.music.netease.default_level.clone()),
                        ],
                    )
                    .await?;
                data.get("data")
                    .and_then(Value::as_array)
                    .and_then(|items| items.first())
                    .and_then(|item| item.get("url"))
                    .and_then(Value::as_str)
                    .map(ToString::to_string)
                    .context("song has no playable url")
            }
        }
    }

    /// Plays the next lined-up song, skipping ones that can't be played
    /// (members-only, taken down) instead of stopping.
    async fn play_next_from_queue(&self) -> anyhow::Result<QueueItem> {
        let mut last_err = None;
        for _ in 0..6 {
            match self.play_next_once().await {
                Ok(item) => return Ok(item),
                Err(err) => {
                    let empty = self.state.lock().await.queue.is_empty();
                    debug!("跳过放不了的歌：{err}");
                    last_err = Some(err);
                    if empty {
                        break;
                    }
                }
            }
        }
        Err(last_err.unwrap_or_else(|| anyhow::anyhow!("queue is empty")))
    }

    async fn play_next_once(&self) -> anyhow::Result<QueueItem> {
        let item = {
            let mut state = self.state.lock().await;
            let item = state.queue.first().cloned().context("queue is empty")?;
            state.queue.remove(0);
            if let Some(current) = state.current.take() {
                state.history.push(current);
            }
            state.paused = false;
            item
        };
        // Alternatives are queued without a URL; resolve when they come up.
        let mut item = item;
        if item.url.is_empty() {
            item.url = self.song_url(&item.song.id).await?;
        }
        {
            let mut state = self.state.lock().await;
            state.current = Some(item.clone());
            state.started_at = Some(std::time::Instant::now());
        }
        self.device.play_url(&item.url).await?;
        Ok(item)
    }

    /// Keep the music going like the native speaker: when the queue runs out,
    /// queue songs similar to the one that just finished (console provider).
    /// Recently played songs are skipped. Returns whether anything was added.
    async fn refill_queue(&self) -> bool {
        if self.config.music.provider != "console" {
            return false;
        }
        let (current, recent) = {
            let state = self.state.lock().await;
            let recent: std::collections::HashSet<String> = state
                .history
                .iter()
                .rev()
                .take(50)
                .map(|item| item.song.id.clone())
                .chain(state.current.iter().map(|item| item.song.id.clone()))
                .collect();
            (state.current.as_ref().map(|item| item.song.id.clone()), recent)
        };
        let Some(current) = current else { return false };
        let data = match self
            .console_request("/api/device/music/more", &[("id", current), ("limit", "10".to_string())])
            .await
        {
            Ok(data) => data,
            Err(err) => {
                warn!("取相似歌曲失败：{err:?}");
                return false;
            }
        };
        let songs: Vec<Song> = serde_json::from_value(data).unwrap_or_default();
        let fresh: Vec<QueueItem> = songs
            .into_iter()
            .filter(|song| !recent.contains(&song.id))
            .map(|song| QueueItem { song, url: String::new() })
            .collect();
        if fresh.is_empty() {
            return false;
        }
        self.state.lock().await.queue.extend(fresh);
        true
    }

    /// Long-polls the console for instant commands ("play this station" from
    /// the console page or a voice tool) and runs them. Runs forever.
    pub async fn run_console_commands(self: std::sync::Arc<Self>) {
        let cfg = self.config.music.console.clone();
        if self.config.music.provider != "console" || cfg.base_url.is_empty() {
            return;
        }
        let url = format!("{}/api/device/command", cfg.base_url.trim_end_matches('/'));
        loop {
            let resp = self
                .client
                .get(&url)
                .header("X-Device-Token", &cfg.token)
                .timeout(std::time::Duration::from_secs(40))
                .send()
                .await;
            let command: Value = match resp {
                Ok(resp) if resp.status().is_success() => resp.json().await.unwrap_or(Value::Null),
                _ => {
                    tokio::time::sleep(std::time::Duration::from_secs(5)).await;
                    continue;
                }
            };
            let kind = command.get("type").and_then(Value::as_str).unwrap_or_default();
            let result = match kind {
                "play" => {
                    let id = command.get("id").and_then(Value::as_str).unwrap_or_default();
                    self.play_query("", id).await
                }
                "stop" => self.stop().await,
                "pause" => {
                    mark_native_playing(false);
                    self.pause().await
                }
                "resume" => self.resume().await,
                "next" => self.next().await,
                "previous" => self.previous().await,
                "volume" => {
                    let delta = command.get("delta").and_then(Value::as_i64).unwrap_or(0);
                    let now = crate::tools::step_volume(delta).await;
                    self.state.lock().await.volume = now;
                    tracing::info!("控制台指令：音量 {delta:+} → {now:?}");
                    continue;
                }
                _ => continue,
            };
            let name = result
                .get("song")
                .and_then(|song| song.get("name"))
                .and_then(|n| n.as_str())
                .unwrap_or("")
                .to_string();
            tracing::info!("控制台指令：{kind} {name}");
        }
    }

    /// Tells the console what is playing (for its player bar): on every
    /// change, and every 30 s so a restarted console catches up.
    pub async fn report_now_playing(self: std::sync::Arc<Self>) {
        let cfg = self.config.music.console.clone();
        if self.config.music.provider != "console" || cfg.base_url.is_empty() {
            return;
        }
        let url = format!("{}/api/device/nowplaying", cfg.base_url.trim_end_matches('/'));
        let mut last = String::new();
        let mut sent_at = std::time::Instant::now();
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(2)).await;
            let playing = self.state.lock().await.current.is_some();
            if playing && sent_at.elapsed() >= std::time::Duration::from_secs(10) {
                let v = crate::tools::current_volume().await;
                self.state.lock().await.volume = v;
            }
            let (body, key) = {
                let state = self.state.lock().await;
                let elapsed = state.started_at.map(|t| t.elapsed().as_secs()).unwrap_or(0);
                let body = json!({
                    "current": state.current.as_ref().map(|item| &item.song),
                    "paused": state.paused || state.interruption.is_some(),
                    "elapsed_s": elapsed,
                    "has_previous": !state.history.is_empty(),
                    "volume": state.volume,
                });
                let key = format!("{:?}|{:?}|{}", state.volume, state.current.as_ref().map(|i| &i.song.id), state.paused || state.interruption.is_some());
                (body, key)
            };
            if key == last && sent_at.elapsed() < std::time::Duration::from_secs(30) {
                continue;
            }
            let ok = self
                .client
                .post(&url)
                .header("X-Device-Token", &cfg.token)
                .json(&body)
                .timeout(std::time::Duration::from_secs(5))
                .send()
                .await
                .is_ok_and(|r| r.status().is_success());
            if ok {
                last = key;
                sent_at = std::time::Instant::now();
            }
        }
    }

    /// Our own music is audible right now (not paused, not held for the
    /// conversation, not native content).
    pub async fn is_sounding(&self) -> bool {
        let state = self.state.lock().await;
        state.current.as_ref().is_some_and(|i| !i.song.id.starts_with("native:"))
            && !state.paused
            && state.interruption.is_none()
    }

    /// Turns the music down so the open mic can hear a follow-up command.
    pub async fn duck_for_listening(&self) {
        if !self.listening_duck.swap(true, std::sync::atomic::Ordering::SeqCst) {
            if let Err(err) = self.device.duck_audio().await {
                warn!("压低音乐音量失败：{err:?}");
            }
        }
    }

    pub async fn end_listening_duck(&self) {
        if self.listening_duck.swap(false, std::sync::atomic::Ordering::SeqCst) {
            if let Err(err) = self.device.unduck_audio().await {
                warn!("恢复音乐音量失败：{err:?}");
            }
        }
    }

    /// Drops announcements the console has queued but not delivered, so
    /// stopping a briefing does not just move on to the next section.
    pub async fn cancel_announcements(&self) {
        let cfg = &self.config.music.console;
        if cfg.base_url.is_empty() {
            return;
        }
        let url = format!("{}/api/device/announcements/clear", cfg.base_url.trim_end_matches('/'));
        match self
            .client
            .post(&url)
            .header("X-Device-Token", &cfg.token)
            .timeout(std::time::Duration::from_secs(5))
            .send()
            .await
        {
            Ok(resp) => {
                let dropped = resp.text().await.unwrap_or_default();
                if dropped.trim() != "0" {
                    tracing::info!("已取消还没播的定时播报：{}", dropped.trim());
                }
            }
            Err(err) => warn!("取消定时播报失败：{err}"),
        }
    }

    /// Leftover content in the firmware's player (a story from before, an
    /// item it resumed) is cleared at startup: otherwise the next
    /// announcement makes it start playing on its own.
    pub async fn clear_leftover_native(&self) {
        mark_native_playing(false);
        if native_status().await.is_some_and(|s| s != 0) {
            native_op("stop").await;
            tracing::info!("清掉了原生播放器里残留的内容");
        }
    }

    /// A track or station is loaded (playing or paused): 下一首 has something to act on.
    pub async fn has_track(&self) -> bool {
        self.state.lock().await.current.is_some()
    }

    /// Something of ours is playing, or paused only for the conversation.
    pub async fn is_active(&self) -> bool {
        let state = self.state.lock().await;
        state.current.is_some() && (!state.paused || state.interruption.is_some())
    }

    /// Plays the next queued track when one ends; clears the state when the
    /// queue runs out (so "what's playing" is never stale). Runs forever.
    pub async fn watch_track_end(self: std::sync::Arc<Self>) {
        let mut failures = 0u32;
        let mut native_idle = 0u32;
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(1)).await;
            let (playing, fresh, has_queue) = {
                let state = self.state.lock().await;
                (
                    state.current.is_some() && !state.paused && state.interruption.is_none(),
                    state.started_at.is_some_and(|t| t.elapsed() < std::time::Duration::from_secs(4)),
                    !state.queue.is_empty(),
                )
            };
            if playing && !fresh && self.native_now().await {
                // Native playback: it runs its own playlist. Clear our state
                // once its player has stopped for a while (ended, or the user
                // stopped it some other way).
                if native_status().await == Some(1) {
                    native_idle = 0;
                } else {
                    native_idle += 1;
                    if native_idle >= 5 {
                        native_idle = 0;
                        self.state.lock().await.current = None;
                        mark_native_playing(false);
                        tracing::info!("原生播放结束");
                    }
                }
                tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                continue;
            }
            if !playing || fresh || music_process_alive() {
                continue;
            }
            if !has_queue && self.refill_queue().await {
                tracing::info!("队列放完了，接着放相似的歌");
            }
            let has_queue = !self.state.lock().await.queue.is_empty();
            if has_queue {
                match self.play_next_from_queue().await {
                    Ok(item) => {
                        failures = 0;
                        tracing::info!("播放下一首：{}", item.song.name)
                    }
                    Err(err) if failures < 3 => {
                        // Usually the console is restarting or the network
                        // blinked: try again shortly instead of going silent.
                        failures += 1;
                        warn!("自动播放下一首失败，10 秒后重试：{err}");
                        tokio::time::sleep(std::time::Duration::from_secs(10)).await;
                    }
                    Err(err) => {
                        failures = 0;
                        warn!("自动播放下一首失败：{err:?}");
                        self.state.lock().await.current = None;
                    }
                }
            } else {
                let mut state = self.state.lock().await;
                if let Some(done) = state.current.take() {
                    state.history.push(done);
                }
            }
        }
    }

    async fn has_current(&self) -> bool {
        self.state.lock().await.current.is_some()
    }

    async fn play_previous_from_history(&self) -> anyhow::Result<QueueItem> {
        let item = {
            let mut state = self.state.lock().await;
            let item = state.history.pop().context("history is empty")?;
            if let Some(current) = state.current.take() {
                state.queue.insert(0, current);
            }
            state.paused = false;
            item
        };
        let mut item = item;
        if item.url.is_empty() {
            item.url = self.song_url(&item.song.id).await?;
        }
        {
            let mut state = self.state.lock().await;
            state.current = Some(item.clone());
            state.started_at = Some(std::time::Instant::now());
        }
        self.device.play_url(&item.url).await?;
        Ok(item)
    }

    fn interruption_mode(&self) -> InterruptionAction {
        match self.config.music.interruption.mode.trim() {
            "duck" | "lower_volume" | "volume" => InterruptionAction::Duck,
            "none" | "keep_playing" => InterruptionAction::None,
            _ => InterruptionAction::Pause,
        }
    }

    async fn search_netease(&self, query: &str, limit: usize) -> anyhow::Result<Vec<Song>> {
        let data = self
            .netease_request(
                "/cloudsearch",
                &[
                    ("keywords", query.to_string()),
                    ("limit", limit.to_string()),
                ],
            )
            .await?;
        let songs = data
            .get("result")
            .and_then(|value| value.get("songs"))
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        Ok(songs.iter().map(parse_netease_song).collect())
    }

    async fn search_navidrome(&self, query: &str, limit: usize) -> anyhow::Result<Vec<Song>> {
        let data = self
            .navidrome_request(
                "/rest/search3.view",
                &[
                    ("query", query.to_string()),
                    ("songCount", limit.to_string()),
                    ("artistCount", "3".to_string()),
                    ("albumCount", "3".to_string()),
                ],
            )
            .await?;
        let songs = data
            .get("searchResult3")
            .and_then(|value| value.get("song"))
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        Ok(songs.iter().map(parse_navidrome_song).collect())
    }

    async fn netease_request(
        &self,
        path: &str,
        params: &[(&str, String)],
    ) -> anyhow::Result<Value> {
        require_music_enabled(&self.config)?;
        self.ensure_netease_server().await?;
        let cookie = self.read_cookie().await;
        let mut url = Url::parse(&self.config.music.netease.api_base_url)?;
        url.set_path(path);
        {
            let mut pairs = url.query_pairs_mut();
            for (key, value) in params {
                if !value.is_empty() {
                    pairs.append_pair(key, value);
                }
            }
            if let Some(cookie) = cookie {
                pairs.append_pair("cookie", &cookie);
            }
        }
        Ok(self
            .client
            .get(url)
            .timeout(timeout_duration(self.config.music.netease.timeout_s))
            .send()
            .await?
            .json::<Value>()
            .await?)
    }

    async fn console_request(&self, path: &str, params: &[(&str, String)]) -> anyhow::Result<Value> {
        let cfg = &self.config.music.console;
        let mut url = Url::parse(cfg.base_url.trim_end_matches('/'))?;
        url.set_path(path);
        url.query_pairs_mut().extend_pairs(params.iter().map(|(k, v)| (*k, v.as_str())));
        let data = self
            .client
            .get(url)
            .header("X-Device-Token", &cfg.token)
            .timeout(timeout_duration(cfg.timeout_s))
            .send()
            .await?
            .json::<Value>()
            .await?;
        if let Some(err) = data.get("error").and_then(Value::as_str) {
            anyhow::bail!("{err}");
        }
        Ok(data)
    }

    async fn navidrome_request(
        &self,
        path: &str,
        params: &[(&str, String)],
    ) -> anyhow::Result<Value> {
        let mut url = Url::parse(&self.config.music.navidrome.base_url)?;
        url.set_path(path);
        let (salt, token) = subsonic_token(&self.config.music.navidrome.password);
        {
            let mut pairs = url.query_pairs_mut();
            pairs
                .append_pair("u", &self.config.music.navidrome.username)
                .append_pair("t", &token)
                .append_pair("s", &salt)
                .append_pair("v", &self.config.music.navidrome.api_version)
                .append_pair("c", "xiaoai-agent")
                .append_pair("f", "json");
            for (key, value) in params {
                pairs.append_pair(key, value);
            }
        }
        let data = self
            .client
            .get(url)
            .timeout(timeout_duration(self.config.music.navidrome.timeout_s))
            .send()
            .await?
            .json::<Value>()
            .await?;
        let response = data
            .get("subsonic-response")
            .context("Invalid Navidrome response")?;
        if response.get("status").and_then(Value::as_str) != Some("ok") {
            anyhow::bail!(
                "{}",
                response
                    .get("error")
                    .and_then(|err| err.get("message"))
                    .and_then(Value::as_str)
                    .unwrap_or("Navidrome request failed")
            );
        }
        Ok(response.clone())
    }

    fn navidrome_stream_url(&self, song_id: &str) -> anyhow::Result<String> {
        let mut url = Url::parse(&self.config.music.navidrome.base_url)?;
        url.set_path("/rest/stream.view");
        let (salt, token) = subsonic_token(&self.config.music.navidrome.password);
        url.query_pairs_mut()
            .append_pair("u", &self.config.music.navidrome.username)
            .append_pair("t", &token)
            .append_pair("s", &salt)
            .append_pair("v", &self.config.music.navidrome.api_version)
            .append_pair("c", "xiaoai-agent")
            .append_pair("f", "json")
            .append_pair("id", song_id);
        Ok(url.to_string())
    }

    async fn ensure_netease_server(&self) -> anyhow::Result<()> {
        if self
            .client
            .get(format!(
                "{}/login/status",
                self.config.music.netease.api_base_url.trim_end_matches('/')
            ))
            .timeout(timeout_duration(3.0))
            .send()
            .await
            .is_ok()
        {
            return Ok(());
        }
        anyhow::bail!(
            "Netease API is not reachable at {}; run the API service externally and set music.netease.api_base_url",
            self.config.music.netease.api_base_url
        );
    }

    async fn read_cookie(&self) -> Option<String> {
        if let Some(cookie) = self.netease_cookie.lock().await.clone() {
            return Some(cookie);
        }
        let path = self.config.music.netease.cookie_file.as_ref()?;
        let text = tokio::fs::read_to_string(path).await.ok()?;
        serde_json::from_str::<Value>(&text)
            .ok()
            .and_then(|value| {
                value
                    .get("cookie")
                    .and_then(Value::as_str)
                    .map(ToString::to_string)
            })
            .or_else(|| Some(text.trim().to_string()))
            .filter(|text| !text.is_empty())
    }

    async fn remember_cookie_from_response(&self, data: &Value) {
        let Some(cookie) = data.get("cookie").and_then(Value::as_str) else {
            return;
        };
        *self.netease_cookie.lock().await = Some(cookie.to_string());
    }

    fn netease_phone(&self) -> String {
        if !self.config.music.netease.phone.trim().is_empty() {
            self.config.music.netease.phone.trim().to_string()
        } else {
            self.config.music.netease.account.trim().to_string()
        }
    }
}

fn split_many(text: &str) -> Vec<String> {
    text.split(['\n', ';', '；'])
        .map(str::trim)
        .filter(|item| !item.is_empty())
        .map(ToString::to_string)
        .collect()
}

fn parse_netease_song(item: &Value) -> Song {
    Song {
        id: value_string(&item["id"]),
        name: value_string(&item["name"]),
        artists: item
            .get("ar")
            .or_else(|| item.get("artists"))
            .and_then(Value::as_array)
            .map(|artists| {
                artists
                    .iter()
                    .filter_map(|artist| artist.get("name").and_then(Value::as_str))
                    .map(ToString::to_string)
                    .collect()
            })
            .unwrap_or_default(),
        album: item
            .get("al")
            .or_else(|| item.get("album"))
            .and_then(|album| album.get("name"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        duration_ms: item
            .get("dt")
            .or_else(|| item.get("duration"))
            .and_then(Value::as_u64),
        fee: item.get("fee").cloned(),
    }
}

fn parse_navidrome_song(item: &Value) -> Song {
    let mut artists = item
        .get("artists")
        .and_then(Value::as_array)
        .map(|artists| {
            artists
                .iter()
                .filter_map(|artist| artist.get("name").and_then(Value::as_str))
                .map(ToString::to_string)
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    if artists.is_empty() {
        if let Some(artist) = item.get("artist").and_then(Value::as_str) {
            artists.push(artist.to_string());
        }
    }
    Song {
        id: value_string(&item["id"]),
        name: item
            .get("title")
            .or_else(|| item.get("name"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        artists,
        album: item
            .get("album")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        duration_ms: item
            .get("duration")
            .and_then(Value::as_u64)
            .map(|seconds| seconds * 1000),
        fee: None,
    }
}

fn value_string(value: &Value) -> String {
    value
        .as_str()
        .map(ToString::to_string)
        .unwrap_or_else(|| value.to_string().trim_matches('"').to_string())
}

fn subsonic_token(password: &str) -> (String, String) {
    let mut bytes = [0_u8; 8];
    rand::thread_rng().fill_bytes(&mut bytes);
    let salt = bytes
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    let token = format!("{:x}", md5::compute(format!("{password}{salt}")));
    (salt, token)
}


/// Whether our music player (pid in /tmp/xiaoai-music.pid, see
/// device.play_url_command) is still running.
/// The system media player's state: 1 playing, 2 paused, 0 idle.
async fn native_status() -> Option<i64> {
    let out = crate::shell::run_shell("ubus call mediaplayer player_get_play_status").await.ok()?;
    let idx = out.stdout.find("status")?;
    out.stdout[idx..]
        .chars()
        .skip_while(|c| !c.is_ascii_digit())
        .take_while(|c| c.is_ascii_digit())
        .collect::<String>()
        .parse()
        .ok()
}

/// say.sh reads this: while it exists, content in the firmware's player is
/// ours on purpose and must not be silenced after we speak.
fn mark_native_playing(on: bool) {
    let path = "/tmp/xiaoai-native-playing";
    if on {
        let _ = std::fs::write(path, b"1");
    } else {
        let _ = std::fs::remove_file(path);
    }
}

/// pause | play | stop | next | prev on the system media player.
async fn native_op(action: &str) {
    let cmd = format!(
        "ubus call mediaplayer player_play_operation '{{\"action\":\"{action}\",\"media\":\"app_ios\"}}'"
    );
    if let Err(err) = crate::shell::run_shell(&cmd).await {
        warn!("原生播放器 {action} 失败：{err}");
    }
}

/// The native brain's log keeps text as JSON `\uXXXX` escapes.
pub(crate) fn unescape_unicode(text: &str) -> String {
    let mut out = String::new();
    let mut rest = text;
    while let Some(i) = rest.find("\\u") {
        out.push_str(&rest[..i]);
        let code = rest.get(i + 2..i + 6).and_then(|h| u32::from_str_radix(h, 16).ok());
        match code.and_then(char::from_u32) {
            Some(c) => {
                out.push(c);
                rest = &rest[i + 6..];
            }
            None => {
                out.push_str("\\u");
                rest = &rest[i + 2..];
            }
        }
    }
    out.push_str(rest);
    out
}

fn shell_quote(text: &str) -> String {
    format!("'{}'", text.replace('\'', "'\\''"))
}

fn music_process_alive() -> bool {
    std::fs::read_to_string("/tmp/xiaoai-music.pid")
        .ok()
        .and_then(|pid| pid.trim().parse::<u32>().ok())
        .is_some_and(|pid| std::path::Path::new(&format!("/proc/{pid}")).exists())
}

/// The CJK / alphanumeric core of a title: "《春庭雪》Remix - 等什么君" -> "春庭雪remix等什么君".
/// Versions of one song share the core of the shorter title.
fn title_key(name: &str) -> String {
    let core: String = name
        .split(['-', '(', '（', '['])
        .next()
        .unwrap_or(name)
        .chars()
        .filter(|c| c.is_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect();
    core.trim_end_matches("remix").to_string()
}

#[cfg(test)]
mod title_tests {
    use super::title_key;

    #[test]
    fn versions_share_a_key() {
        let a = title_key("春庭雪");
        let b = title_key("《春庭雪》 Remix - 等什么君");
        assert!(b.contains(&a));
        assert!(!title_key("七里香").contains(&a));
    }
}

#[cfg(test)]
mod native_tests {
    #[test]
    fn unescapes() {
        assert_eq!(super::unescape_unicode("\\u4e3a\\u4f60\\u64ad\\u653e\\u4e09\\u4f53\\uff0cok"), "为你播放三体，ok");
    }
}
