mod agent;
mod airplay;
mod asr;
mod sentences;
mod failover;
mod base;
mod capture;
mod config;
mod device;
mod mcp;
mod mcp_legacy_sse;
mod music;
mod shell;
mod speech;
mod tools;
mod vad;
mod weather;

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use anyhow::Context;
use clap::Parser;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio::time::{interval, sleep, timeout, MissedTickBehavior};
use tracing::{debug, error, info, warn};
use tracing_subscriber::EnvFilter;

use crate::agent::AgentRuntime;
use crate::airplay::AirPlayService;
use crate::asr::AsrClient;
use crate::capture::{
    record_utterance_during, record_utterance_server_vad_streaming, record_utterance_streaming,
};
use crate::config::{AppConfig, DeviceConfig};
use crate::device::Device;
use crate::music::MusicService;
use crate::speech::{SpeechHandle, SpeechService, SpeechServiceEvent, SpeechTurn};

const ASR_SERVICE_ERROR_PROMPT: &str = "没听清，网络有点慢，再说一次吧。";
const LLM_SERVICE_ERROR_PROMPT: &str = "抱歉，大模型服务遇到问题，请稍后重试";

#[derive(Debug, Parser)]
#[command(name = "xiaoai-agent")]
#[command(about = "Standalone XiaoAI on-device agent using the native mipns speech frontend")]
struct Cli {
    #[arg(short, long, default_value = "/data/open-xiaoai/agent.yaml")]
    config: PathBuf,
    /// Transcribe a 16 kHz mono WAV with the configured ASR services and exit
    /// (diagnostics; doesn't touch the speech socket).
    #[arg(long)]
    asr_test: Option<std::path::PathBuf>,
    /// Send TEXT to the running agent as if it had just been heard, print
    /// what it did, and exit. Nothing is spoken unless --aloud is given;
    /// actions (music, volume, devices) are real.
    #[arg(long)]
    text: Option<String>,
    #[arg(long)]
    aloud: bool,
}

/// Local-only text entry (root-owned socket in /tmp): the same handling as
/// a spoken sentence, for testing without talking to the speaker.
const TEXT_SOCKET: &str = "/tmp/xiaoai-agent-text.sock";

/// Third-party crates (model client, MCP client, HTTP stack) log in English;
/// keep them quiet unless something goes wrong. Our own messages are Chinese.
const QUIET_DEPENDENCIES: &[&str] = &[
    "rig_core=error", "rig=error", "rmcp=error", "h2=error", "hyper=error",
    "hyper_util=error", "reqwest=error", "rustls=error", "rustls_platform_verifier=error",
    "tungstenite=error", "tokio_tungstenite=error", "shairplay=warn",
];

fn log_filter() -> EnvFilter {
    let base = std::env::var("RUST_LOG").unwrap_or_else(|_| "info".to_string());
    let mut filter = EnvFilter::new(&base);
    for directive in QUIET_DEPENDENCIES {
        let target = directive.split('=').next().unwrap_or_default();
        if !base.contains(target) {
            if let Ok(d) = directive.parse() {
                filter = filter.add_directive(d);
            }
        }
    }
    filter
}

/// "2026-11-14 21:38:45 信息 已连接扩展工具「console」" in local (Beijing) time.
struct ChineseLogFormat;

impl<S, N> tracing_subscriber::fmt::FormatEvent<S, N> for ChineseLogFormat
where
    S: tracing::Subscriber + for<'a> tracing_subscriber::registry::LookupSpan<'a>,
    N: for<'a> tracing_subscriber::fmt::FormatFields<'a> + 'static,
{
    fn format_event(
        &self,
        ctx: &tracing_subscriber::fmt::FmtContext<'_, S, N>,
        mut writer: tracing_subscriber::fmt::format::Writer<'_>,
        event: &tracing::Event<'_>,
    ) -> std::fmt::Result {
        let level = match *event.metadata().level() {
            tracing::Level::ERROR => "错误",
            tracing::Level::WARN => "警告",
            tracing::Level::INFO => "信息",
            tracing::Level::DEBUG => "调试",
            tracing::Level::TRACE => "跟踪",
        };
        let now = chrono::Utc::now().with_timezone(&chrono_tz::Asia::Shanghai);
        write!(writer, "{} {} ", now.format("%Y-%m-%d %H:%M:%S"), level)?;
        ctx.field_format().format_fields(writer.by_ref(), event)?;
        writeln!(writer)
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(log_filter())
        .event_format(ChineseLogFormat)
        .init();

    let cli = Cli::parse();
    let config = Arc::new(
        AppConfig::load(&cli.config)
            .with_context(|| format!("failed to load config {}", cli.config.display()))?,
    );
    if let Some(text) = &cli.text {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let mut stream = tokio::net::UnixStream::connect(TEXT_SOCKET)
            .await
            .context("the agent is not running (no text socket)")?;
        let line = format!("{}{}\n", if cli.aloud { "aloud:" } else { "" }, text.replace('\n', " "));
        stream.write_all(line.as_bytes()).await?;
        let mut out = String::new();
        stream.read_to_string(&mut out).await?;
        println!("{}", out.trim_end());
        return Ok(());
    }
    if let Some(path) = &cli.asr_test {
        let wav = std::fs::read(path)?;
        let asr = AsrClient::new(config.asr.clone())?;
        println!("{:?}", asr.transcribe_pcm(&wav[44.min(wav.len())..], 16_000).await);
        return Ok(());
    }
    let mut speech = SpeechService::bind(&config.runtime.speech_socket).await?;
    let speech_handle = speech.handle();

    let device = Device::new(config.device.clone());
    let asr = AsrClient::new(config.asr.clone())?;
    let music = Arc::new(MusicService::new(config.clone(), device.clone())?);
    music.clear_leftover_native().await;
    tokio::spawn(music.clone().watch_track_end());
    tokio::spawn(music.clone().run_console_commands());
    tokio::spawn(music.clone().report_now_playing());
    tokio::spawn(music.clone().prefetch_next_url());
    let airplay = AirPlayService::start(config.airplay.clone()).await?;
    let agent = Arc::new(AgentRuntime::new(config.clone(), device.clone(), music.clone()).await?);

    {
        let state = TurnState {
            config: config.clone(),
            device: device.clone(),
            asr: asr.clone(),
            agent: agent.clone(),
            music: music.clone(),
            airplay: airplay.clone(),
        };
        tokio::spawn(async move {
            if let Err(err) = serve_text(state).await {
                warn!("文字入口没有启动：{err:?}");
            }
        });
    }

    info!("语音服务已就绪");
    device
        .blink_ready(config.device.led_listening, Duration::from_millis(250))
        .await;

    let mut active_session: Option<ActiveSession> = None;
    let mut session_check = interval(Duration::from_millis(250));
    session_check.set_missed_tick_behavior(MissedTickBehavior::Delay);

    loop {
        tokio::select! {
            event = speech.recv() => {
                let Some(event) = event else {
                    anyhow::bail!("speech.usock service stopped unexpectedly");
                };
                match event {
                    SpeechServiceEvent::Registered { .. } => {
                        info!("麦克风已连接");
                    }
                    SpeechServiceEvent::Fatal(message) => anyhow::bail!(message),
                    SpeechServiceEvent::Turn(turn) if turn.activation().is_wakeup() => {
                        info!("被「小爱同学」唤醒");
                        if let Some(active) = active_session.take() {
                            if !active.task.is_finished() {
                                info!("上一轮对话还没结束，已打断");
                                active.task.abort();
                            }
                            let _ = active.task.await;
                        }

                        // Music pauses for the conversation; any reply still being
                        // spoken stops (it no longer takes the music with it).
                        music.interrupt_for_wake().await;
                        music.end_listening_duck().await; // an interrupted session can't restore it
                        airplay.interrupt_for_wake().await;
                        device.abort_current_output().await;
                        cleanup_turn_leds(&device, &config.device).await;
                        agent.reset_session("wake keyword").await;
                        agent.refresh_memories();

                        let state = TurnState {
                            config: config.clone(),
                            device: device.clone(),
                            asr: asr.clone(),
                            agent: agent.clone(),
                            music: music.clone(),
                            airplay: airplay.clone(),
                        };
                        let (followup_tx, followup_rx) = mpsc::channel(2);
                        let handle = speech_handle.clone();
                        let task = tokio::spawn(async move {
                            if let Err(err) = run_turn(state, handle, turn, followup_rx).await {
                                error!("本轮对话出错：{err:?}");
                            }
                        });
                        active_session = Some(ActiveSession { task, followup_tx });
                    }
                    SpeechServiceEvent::Turn(turn) => {
                        info!("继续聆听下一句");
                        let receiver_ready = active_session
                            .as_ref()
                            .is_some_and(|active| !active.task.is_finished());
                        if receiver_ready {
                            let send_result = active_session
                                .as_ref()
                                .expect("active session was checked")
                                .followup_tx
                                .send(turn)
                                .await;
                            if let Err(err) = send_result {
                                reject_orphaned_turn(err.0, &speech_handle).await;
                            }
                        } else {
                            reject_orphaned_turn(turn, &speech_handle).await;
                        }
                    }
                }
            }
            _ = session_check.tick() => {
                let session_finished = active_session
                    .as_ref()
                    .is_some_and(|active| active.task.is_finished());
                if session_finished {
                    if let Some(active) = active_session.take() {
                        if let Err(err) = active.task.await {
                            warn!("对话任务意外结束：{err:?}");
                        }
                    }
                    device
                        .blink_ready(config.device.led_listening, Duration::from_millis(250))
                        .await;
                }
            }
        }
    }
}

struct ActiveSession {
    task: JoinHandle<()>,
    followup_tx: mpsc::Sender<SpeechTurn>,
}

#[derive(Clone)]
struct TurnState {
    config: Arc<AppConfig>,
    device: Device,
    asr: AsrClient,
    agent: Arc<AgentRuntime>,
    music: Arc<MusicService>,
    airplay: AirPlayService,
}

async fn reject_orphaned_turn(mut turn: SpeechTurn, speech: &SpeechHandle) {
    warn!("收到语音但当前没有进行中的对话，已忽略");
    let turn_id = turn.id();
    let _ = turn.stop_capture().await;
    let _ = speech.finish_dialog(turn_id).await;
}

async fn run_turn(
    state: TurnState,
    speech: SpeechHandle,
    first_turn: SpeechTurn,
    followup_rx: mpsc::Receiver<SpeechTurn>,
) -> anyhow::Result<()> {
    let active_turn_id = Arc::new(AtomicU64::new(first_turn.id()));
    let result = run_session(
        state.clone(),
        speech.clone(),
        first_turn,
        followup_rx,
        active_turn_id.clone(),
    )
    .await;
    if result.is_err() {
        let _ = speech
            .finish_dialog(active_turn_id.load(Ordering::Relaxed))
            .await;
        state.agent.reset_session("turn failed").await;
    }
    cleanup_turn_leds(&state.device, &state.config.device).await;
    state.music.end_listening_duck().await;
    state.music.restore_after_interruption().await;
    state.airplay.restore_after_interruption().await;
    result
}

async fn run_session(
    state: TurnState,
    speech: SpeechHandle,
    mut turn: SpeechTurn,
    mut followup_rx: mpsc::Receiver<SpeechTurn>,
    active_turn_id: Arc<AtomicU64>,
) -> anyhow::Result<()> {
    let led = &state.config.device;
    let idle_timeout =
        Duration::from_secs_f64(state.config.runtime.session_idle_timeout_s.max(1.0));
    // The previous reply, if it is still being spoken while we listen.
    let mut playback: Option<Playback> = None;
    // Whether this session has had a real exchange (then going quiet gets a
    // short sign-off instead of just stopping).
    let mut exchanged = false;
    // Set while a reply is frozen because the user may be talking over it.
    let held = Arc::new(std::sync::atomic::AtomicBool::new(false));
    // After a music command the music plays quietly and the mic stays open
    // until this moment; then the conversation ends and the volume returns.
    let mut music_window: Option<std::time::Instant> = None;
    let session_idle = idle_timeout;

    loop {
        let turn_id = turn.id();
        active_turn_id.store(turn_id, Ordering::Relaxed);
        let idle_timeout = match music_window {
            Some(until) => until.saturating_duration_since(std::time::Instant::now()).max(Duration::from_secs(1)),
            None => session_idle,
        };
        state.device.show_led(led.led_listening).await;
        let device_for_speech = state.device.clone();
        let maybe_stream = match state
            .asr
            .start_streaming_transcription(state.config.capture.sample_rate)
            .await
        {
            Ok(stream) => stream,
            Err(err) => {
                asr_failed(&state, led, exchanged).await;
                return Err(err.context("ASR failed after retries"));
            }
        };
        let mut heard_at = std::time::Instant::now();
        // What the microphone actually gave us, so an empty transcript can be
        // told apart from a microphone that heard nothing.
        let mut captured: Option<(f64, f64)> = None;
        let text = if let Some(mut stream) = maybe_stream {
            if stream.server_vad_enabled() {
                let device_for_server_start = device_for_speech.clone();
                let device_for_server_stop = state.device.clone();
                let led_user_speaking = led.led_user_speaking;
                let led_thinking = led.led_thinking;
                match record_utterance_server_vad_streaming(
                    state.config.capture.clone(),
                    idle_timeout,
                    &mut turn,
                    &mut stream,
                    move || {
                        let device = device_for_server_start.clone();
                        async move {
                            device.show_led(led_user_speaking).await;
                        }
                    },
                    move || {
                        let device = device_for_server_stop.clone();
                        async move {
                            device.show_led(led_thinking).await;
                        }
                    },
                )
                .await
                {
                    Ok(text) => text,
                    Err(err) if is_capture_timeout(&err) => {
                        stream.close().await;
                        info!("一段时间没说话，对话收尾");
                        if exchanged && !state.music.is_active().await { say_farewell(&state.device).await; }
                        speech.finish_dialog(turn_id).await?;
                        state.agent.reset_session("session idle timeout").await;
                        return Ok(());
                    }
                    Err(err) => {
                        stream.close().await;
                        asr_failed(&state, led, exchanged).await;
                        return Err(err.context("ASR failed after retries"));
                    }
                }
            } else {
                let appender = stream.appender();
                let appender_for_chunk = appender.clone();
                let appender_for_reject = appender.clone();
                let led_user_speaking = led.led_user_speaking;
                let _pcm = match record_utterance_streaming(
                    state.config.capture.clone(),
                    idle_timeout,
                    &mut turn,
                    move || {
                        let device = device_for_speech.clone();
                        async move {
                            device.show_led(led_user_speaking).await;
                        }
                    },
                    move |bytes| {
                        let appender = appender_for_chunk.clone();
                        async move { appender.append_pcm(bytes).await }
                    },
                    move || {
                        let appender = appender_for_reject.clone();
                        async move { appender.clear().await }
                    },
                )
                .await
                {
                    Ok(pcm) => pcm,
                    Err(err) if is_capture_timeout(&err) => {
                        stream.close().await;
                        info!("一段时间没说话，对话收尾");
                        if exchanged && !state.music.is_active().await { say_farewell(&state.device).await; }
                        speech.finish_dialog(turn_id).await?;
                        state.agent.reset_session("session idle timeout").await;
                        return Ok(());
                    }
                    Err(err) => {
                        stream.close().await;
                        return Err(err);
                    }
                };

                state.device.show_led(led.led_thinking).await;
                state.device.play_heard_sound().await;
                heard_at = std::time::Instant::now();
                match stream.commit_and_transcribe().await {
                    Ok(text) => text,
                    Err(err) => {
                        asr_failed(&state, led, exchanged).await;
                        return Err(err.context("ASR failed after retries"));
                    }
                }
            }
        } else {
            let playing_flag = playback.as_ref().map(|p| p.playing.clone());
            let held_flag = held.clone();
            let pcm =
                match record_utterance_during(
                    state.config.capture.clone(),
                    idle_timeout,
                    &mut turn,
                    playing_flag.clone(),
                    state.config.runtime.barge_in_boost.max(1.0),
                    {
                        let led_user_speaking = led.led_user_speaking;
                        let held = held_flag.clone();
                        move || {
                            let device = device_for_speech.clone();
                            let playing = playing_flag.clone();
                            let held = held.clone();
                            async move {
                                // Maybe talking over the reply: hold it until
                                // recognition says it really was speech.
                                if playing.as_ref().is_some_and(|p| p.load(Ordering::Relaxed))
                                    && !held.swap(true, Ordering::Relaxed)
                                {
                                    device.hold_speech(true).await;
                                }
                                device.show_led(led_user_speaking).await;
                            }
                        }
                    },
                    {
                        let device = state.device.clone();
                        let held = held_flag.clone();
                        move || {
                            let device = device.clone();
                            let held = held.clone();
                            async move {
                                // Too short to be speech: carry on talking.
                                if held.swap(false, Ordering::Relaxed) {
                                    device.hold_speech(false).await;
                                }
                            }
                        }
                    },
                )
                .await
                {
                    Ok(pcm) => pcm,
                    Err(err) if is_capture_timeout(&err) => {
                        info!("一段时间没说话，对话收尾");
                        if exchanged && !state.music.is_active().await { say_farewell(&state.device).await; }
                        speech.finish_dialog(turn_id).await?;
                        state.agent.reset_session("session idle timeout").await;
                        return Ok(());
                    }
                    Err(err) => return Err(err),
                };

            state.device.show_led(led.led_thinking).await;
            state.device.play_heard_sound().await;
            heard_at = std::time::Instant::now();
            captured = Some((
                pcm.len() as f64 / (state.config.capture.sample_rate as f64 * 2.0),
                crate::vad::peak_norm(&pcm),
            ));
            match state
                .asr
                .transcribe_pcm(&pcm, state.config.capture.sample_rate)
                .await
            {
                Ok(text) => text,
                Err(err) => {
                    asr_failed(&state, led, exchanged).await;
                    return Err(err.context("ASR failed after retries"));
                }
            }
        };

        let command = text.trim();
        let was_held = held.swap(false, Ordering::Relaxed);
        if command.is_empty() {
            match captured {
                Some((secs, peak)) => info!("没识别出内容：录到 {secs:.1} 秒，峰值音量 {peak:.4}"),
                None => info!("没识别出内容"),
            }
            if was_held {
                state.device.hold_speech(false).await; // noise, not the user: go on
            }
            if !exchanged {
                info!("没听到说话，本轮对话结束");
                speech.finish_dialog(turn_id).await?;
                state.agent.reset_session("empty ASR result").await;
                return Ok(());
            }
            if music_window.is_some_and(|until| std::time::Instant::now() >= until) {
                info!("音乐在放，这段时间没有新指令，对话结束");
                speech.finish_dialog(turn_id).await?;
                state.agent.reset_session("session idle timeout").await;
                return Ok(());
            }
            // Mid-conversation noise: keep listening instead of hanging up.
            info!("这一段没识别出话（多半是环境声），继续听");
            speech.continue_dialog(turn_id).await?;
            request_followup_capture().await?;
            let next = match &playback {
                Some(current) => wait_followup(&mut followup_rx, idle_timeout, current).await,
                None => timeout(idle_timeout, followup_rx.recv()).await.ok().flatten(),
            };
            match next {
                Some(next) => {
                    turn = next;
                    continue;
                }
                None => {
                    info!("一段时间没说话，对话收尾");
                    if !state.music.is_active().await {
                        say_farewell(&state.device).await;
                    }
                    speech.finish_dialog(turn_id).await?;
                    state.agent.reset_session("session idle timeout").await;
                    return Ok(());
                }
            }
        }
        if was_held {
            if let Some(current) = playback.as_ref() {
                current.playing.store(false, Ordering::Relaxed);
            }
            info!("被打断，停止播报");
            state.device.abort_current_output().await;
        }
        let asr_secs = heard_at.elapsed().as_secs_f64();
        info!("听到：{command}");
        // A real command: full volume again while it is handled.
        music_window = None;
        state.music.end_listening_duck().await;
        let expanded;
        let command = match shortcut_for(&state.config.agent.shortcuts, command) {
            Some(sc) => {
                info!("快捷指令「{}」→ {}", sc.say, sc.run);
                expanded = sc.run.clone();
                expanded.as_str()
            }
            None => command,
        };
        // "关闭" / "没了" / "再见" end the conversation for sure (the model
        // sometimes only *said* goodbye and kept listening).
        // "暂停" pauses the music and "继续" resumes it; either way we stop
        // listening right away (no model round trip).
        if let Some(resume) = pause_intent(command) {
            if let Some(previous) = playback.take() {
                previous.playing.store(false, Ordering::Relaxed);
                state.device.abort_current_output().await;
            }
            if resume {
                state.music.resume().await;
                info!("继续播放，对话结束");
            } else {
                state.music.pause().await;
                info!("暂停播放，对话结束");
            }
            speech.finish_dialog(turn_id).await?;
            state.agent.reset_session("agent ended conversation").await;
            return Ok(());
        }
        // Everything else goes to the model: one decision maker, so any
        // wording works and the behaviour is the same every time. Only the
        // two safety actions (pause above, stop below) are decided here,
        // because they must never wait for the network and never fail.
        if let Some(stop_music) = exit_intent(command) {
            if let Some(previous) = playback.take() {
                previous.playing.store(false, Ordering::Relaxed);
                state.device.abort_current_output().await;
            }
            if stop_music {
                // Always: the sound may be a story, an alarm or a briefing
                // that we did not start (see MusicService::stop).
                state.music.stop().await;
            }
            info!("用户要结束，对话结束");
            if let Err(err) = state.device.speak("好的").await {
                warn!("播报失败：{err:?}");
            }
            speech.finish_dialog(turn_id).await?;
            state.agent.reset_session("agent ended conversation").await;
            return Ok(());
        }
        let think_started = std::time::Instant::now();

        // The previous reply is over (finished, or stopped by this utterance).
        if let Some(previous) = playback.take() {
            previous.finish().await;
        }
        // Start the player now: it speaks each sentence as the model writes it.
        let (reply_tx, mut raw_rx) = mpsc::unbounded_channel();
        let (fwd_tx, reply_rx) = mpsc::unbounded_channel();
        {
            // Native content (故事、喜马拉雅…) starts playing at once: drop
            // anything the model says after starting it.
            let music = state.music.clone();
            tokio::spawn(async move {
                while let Some(piece) = raw_rx.recv().await {
                    if music.silent_reply() {
                        continue;
                    }
                    if fwd_tx.send(piece).is_err() {
                        break;
                    }
                }
            });
        }
        state.music.take_native_started();
        state.music.take_silent_reply();
        let current = Playback::start(state.device.clone(), state.config.music.console.clone(), reply_rx);
        let reply = match state.agent.run_turn(command, reply_tx).await {
            Ok(reply) => reply,
            Err(err) => {
                current.finish().await;
                error!("大模型调用失败：{err:?}");
                // The model is the only decision maker, so when it cannot be
                // reached the speaker still obeys the handful of commands
                // that work without it (volume, skip, lights, the time).
                if let Some(fast) = fast_intent(command, state.music.has_track().await) {
                    let said = run_fast(&state, &fast).await;
                    info!("大模型连不上，本地执行 {fast:?} → {said}");
                    let _ = state.device.speak(if said.is_empty() { "好的" } else { &said }).await;
                } else {
                    speak_service_error(&state.device, led, LLM_SERVICE_ERROR_PROMPT).await;
                }
                speech.finish_dialog(turn_id).await?;
                state.agent.reset_session("llm unavailable").await;
                return Ok(());
            }
        };
        info!(
            "回答：{}（识别 {:.1} 秒，思考到开口 {:.1} 秒，写完 {:.1} 秒）",
            reply.text.replace('\n', " "),
            asr_secs,
            current.first_piece_secs(think_started),
            think_started.elapsed().as_secs_f64()
        );
        state.device.shut_led(led.led_thinking).await;
        if state.music.take_silent_reply() {
            state.music.take_native_started();
            current.finish().await;
            info!("声音本身就是回答，本轮对话结束");
            speech.finish_dialog(turn_id).await?;
            return Ok(());
        }
        if reply.text.contains(IGNORE_MARK) {
            // Not meant for us (people talking nearby): say nothing, stop listening.
            current.finish().await;
            info!("这句不是对我说的，结束聆听");
            speech.finish_dialog(turn_id).await?;
            state.agent.reset_session("not addressed").await;
            return Ok(());
        }
        state.device.show_led(led.led_speaking).await;
        exchanged = true;
        // With music on: the song carries on right after the answer, and the
        // same follow-up window as any other answer stays open.
        // With music on, the conversation ends with the answer. An open mic
        // over music mostly hears the music (commands were missed, and the
        // next wake word could find the microphone busy); the wake word is
        // the reliable way in, and it pauses the music first.
        // One command per wake word: the answer ends the conversation (what
        // was said is still remembered for the next wake). The wake word
        // also interrupts a long answer.
        if !state.config.runtime.follow_up {
            current.finish().await;
            info!("回答完毕，本轮对话结束");
            speech.finish_dialog(turn_id).await?;
            state.agent.reset_session("session idle timeout").await;
            return Ok(());
        }
        // Music that this turn started (or changed) plays on quietly while
        // the mic stays open for the follow-up window. Music that was only
        // paused for the conversation stays paused until the conversation
        // ends, so follow-ups are heard in silence.
        if state.music.is_sounding().await && !reply.should_end {
            current.finish().await;
            match open_followup(&state, &speech, turn_id, &mut followup_rx, session_idle).await? {
                Some((next, window)) => {
                    music_window = window;
                    turn = next;
                    continue;
                }
                None => return Ok(()),
            }
        }
        let barge_in = state.config.runtime.barge_in && !reply.should_end && !reply.text.trim().is_empty();
        if barge_in {
            // Open the mic while the reply is still playing, so talking over
            // it interrupts it (see record_utterance_during).
            speech.continue_dialog(turn_id).await?;
            request_followup_capture().await?;
            turn = match wait_followup(&mut followup_rx, idle_timeout, &current).await {
                Some(next) => next,
                None => {
                    current.finish().await;
                    info!("等待下一句超时，对话收尾");
                        if exchanged && !state.music.is_active().await { say_farewell(&state.device).await; }
                    speech.finish_dialog(turn_id).await?;
                    state.agent.reset_session("session idle timeout").await;
                    return Ok(());
                }
            };
            playback = Some(current);
            continue;
        }
        current.finish().await;
        if reply.should_end {
            info!("对话结束：{}", reply.end_reason);
            speech.finish_dialog(turn_id).await?;
            state.agent.reset_session("agent ended conversation").await;
            return Ok(());
        }

        speech.continue_dialog(turn_id).await?;
        request_followup_capture().await?;
        state.device.shut_led(led.led_speaking).await;
        state.device.show_led(led.led_listening).await;
        turn = match timeout(idle_timeout, followup_rx.recv()).await {
            Ok(Some(turn)) => turn,
            Ok(None) => {
                speech.finish_dialog(turn_id).await?;
                state.agent.reset_session("speech frontend stopped").await;
                return Ok(());
            }
            Err(_) => {
                info!("等待下一句超时，对话收尾");
                        if exchanged && !state.music.is_active().await { say_farewell(&state.device).await; }
                speech.finish_dialog(turn_id).await?;
                state.agent.reset_session("session idle timeout").await;
                return Ok(());
            }
        };
    }
}

/// Opens the mic for the next sentence without a wake word. With music
/// sounding it is turned down first and the window gets a hard end (the
/// music itself can keep triggering the mic). None: nobody spoke, the dialog
/// was finished.
async fn open_followup(
    state: &TurnState,
    speech: &SpeechHandle,
    turn_id: u64,
    followup_rx: &mut mpsc::Receiver<SpeechTurn>,
    window: Duration,
) -> anyhow::Result<Option<(SpeechTurn, Option<std::time::Instant>)>> {
    let led = &state.config.device;
    let over_music = state.music.is_sounding().await;
    if over_music {
        state.music.duck_for_listening().await;
    }
    let until = over_music.then(|| std::time::Instant::now() + window);
    speech.continue_dialog(turn_id).await?;
    request_followup_capture().await?;
    state.device.shut_led(led.led_speaking).await;
    state.device.show_led(led.led_listening).await;
    match timeout(window, followup_rx.recv()).await {
        Ok(Some(next)) => Ok(Some((next, until))),
        _ => {
            info!("等待下一句超时，对话收尾");
            speech.finish_dialog(turn_id).await?;
            state.agent.reset_session("session idle timeout").await;
            Ok(None)
        }
    }
}

/// A reply being spoken in the background, sentence by sentence as the
/// model writes it, while the mic may already listen for a barge-in.
/// A reply longer than this is spoken in parts rather than in one go.
const LONG_REPLY_CHARS: usize = 70;

struct Playback {
    playing: Arc<std::sync::atomic::AtomicBool>,
    first_piece_at: Arc<std::sync::Mutex<Option<std::time::Instant>>>,
    task: JoinHandle<()>,
}

impl Playback {
    fn start(
        device: Device,
        console: crate::config::ConsoleMusicConfig,
        mut pieces: mpsc::UnboundedReceiver<String>,
    ) -> Self {
        let playing = Arc::new(std::sync::atomic::AtomicBool::new(true));
        let first_piece_at = Arc::new(std::sync::Mutex::new(None));
        let flag = playing.clone();
        let first = first_piece_at.clone();
        let task = tokio::spawn(async move {
            let mut stream: Option<(String, JoinHandle<()>)> = None;
            let mut pending = String::new();
            let mut spoken_pieces = 0_u32;
            let client = http_client();
            let base = console.base_url.trim_end_matches('/').to_string();
            while let Some(piece) = pieces.recv().await {
                if piece.contains(IGNORE_MARK) {
                    continue; // "not meant for me": nothing to say
                }
                if !flag.load(Ordering::Relaxed) {
                    break; // interrupted: drop the rest of the reply
                }
                first.lock().unwrap().get_or_insert_with(std::time::Instant::now);
                if stream.is_none() {
                    // One streamed MP3 for the whole reply when the voice allows it.
                    let mode = if voice_is_native() { None } else { open_reply_stream(&client, &base, &console.token).await };
                    if let Some(sid) = mode {
                        let url = format!("{base}/api/device/tts/stream/{sid}.mp3");
                        let _ = push_reply_piece(&client, &base, &console.token, &sid, &piece).await;
                        let dev = device.clone();
                        let player = tokio::spawn(async move {
                            if let Err(err) = dev.play_stream(&url).await {
                                debug!("流式播报结束：{err:?}");
                            }
                        });
                        stream = Some((sid, player));
                        continue;
                    }
                    stream = Some((String::new(), tokio::spawn(async {})));
                }
                let (sid, _) = stream.as_ref().expect("set above");
                if sid.is_empty() {
                    // Native or WAV voices: say each sentence in turn. Each
                    // piece is synthesized alone, so a short opener ("好的，")
                    // would sound off; wait for a whole sentence first.
                    // One utterance for an ordinary reply: each piece is a
                    // separate synthesis request with a gap before it, so
                    // splitting a short answer makes it sound chopped up.
                    // Only a long one (a briefing) is broken into parts, so
                    // it can start before the whole text is written.
                    pending.push_str(&piece);
                    let whole = pending.trim_end().ends_with(['。', '！', '？', '!', '?', '；', '…']);
                    let len = pending.chars().count();
                    // Hold a short reply back until it is complete, then say
                    // it in one go. Break a long one at a sentence end.
                    if !(whole && len >= LONG_REPLY_CHARS) && len < LONG_REPLY_CHARS * 2 {
                        continue;
                    }
                    let piece = std::mem::take(&mut pending);
                    spoken_pieces += 1;
                    let spoken = if voice_is_native() { device.speak_native(&piece).await } else { device.speak(&piece).await };
                    if let Err(err) = spoken {
                        if flag.load(Ordering::Relaxed) {
                            warn!("播报失败：{err:?}");
                        }
                    }
                } else {
                    let _ = push_reply_piece(&client, &base, &console.token, sid, &piece).await;
                }
            }
            if !pending.trim().is_empty() && flag.load(Ordering::Relaxed) {
                spoken_pieces += 1;
                // The rest of the reply, said in one go.
                let _ = if voice_is_native() {
                    device.speak_native(&pending).await
                } else {
                    device.speak(&pending).await
                };
            }
            if spoken_pieces > 1 {
                // Each piece is a separate synthesis with a gap before it.
                info!("这次回答分 {spoken_pieces} 段念出来");
            }
            if let Some((sid, player)) = stream {
                if !sid.is_empty() {
                    let _ = push_reply_piece(&client, &base, &console.token, &sid, "").await;
                }
                let _ = player.await;
            }
            flag.store(false, Ordering::Relaxed);
        });
        Self { playing, first_piece_at, task }
    }

    fn first_piece_secs(&self, since: std::time::Instant) -> f64 {
        self.first_piece_at
            .lock()
            .unwrap()
            .map(|t| t.duration_since(since).as_secs_f64())
            .unwrap_or(0.0)
    }

    async fn finish(self) {
        let _ = self.task.await;
    }
}

/// Ticket for a streamed reply, or None when the console says to speak
/// sentence by sentence (native / WAV voices) or can't be reached.
/// One HTTPS client for the whole process: no TLS handshake per reply
/// (slow on the speaker's CPU).
fn http_client() -> reqwest::Client {
    static CLIENT: std::sync::OnceLock<reqwest::Client> = std::sync::OnceLock::new();
    CLIENT
        .get_or_init(|| {
            reqwest::Client::builder()
                .pool_idle_timeout(Duration::from_secs(600))
                .tcp_keepalive(Duration::from_secs(30))
                .build()
                .unwrap_or_default()
        })
        .clone()
}

/// The console said "native voice" recently: skip asking again for five minutes.
static NATIVE_VOICE_AT: std::sync::Mutex<Option<std::time::Instant>> = std::sync::Mutex::new(None);

fn voice_is_native() -> bool {
    NATIVE_VOICE_AT.lock().unwrap().is_some_and(|t| t.elapsed() < Duration::from_secs(300))
}

async fn open_reply_stream(client: &reqwest::Client, base: &str, token: &str) -> Option<String> {
    if base.is_empty() {
        return None;
    }
    let resp = client
        .post(format!("{base}/api/device/tts/stream"))
        .header("X-Device-Token", token)
        .timeout(Duration::from_secs(3))
        .send()
        .await
        .ok()?;
    let text = resp.text().await.ok()?;
    let sid = text.trim();
    if sid == "native" {
        *NATIVE_VOICE_AT.lock().unwrap() = Some(std::time::Instant::now());
    }
    (sid.len() >= 16 && sid.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_'))
        .then(|| sid.to_string())
}

async fn push_reply_piece(client: &reqwest::Client, base: &str, token: &str, sid: &str, piece: &str) -> anyhow::Result<()> {
    client
        .post(format!("{base}/api/device/tts/stream/{sid}"))
        .header("X-Device-Token", token)
        .timeout(Duration::from_secs(5))
        .body(piece.to_string())
        .send()
        .await?
        .error_for_status()?;
    Ok(())
}

/// The next capture turn, waiting as long as the reply is still playing plus
/// the normal idle timeout afterwards.
async fn wait_followup(
    followup_rx: &mut mpsc::Receiver<SpeechTurn>,
    idle_timeout: Duration,
    playback: &Playback,
) -> Option<SpeechTurn> {
    loop {
        match timeout(Duration::from_millis(500), followup_rx.recv()).await {
            Ok(turn) => return turn,
            Err(_) if playback.playing.load(Ordering::Relaxed) => continue,
            Err(_) => return timeout(idle_timeout, followup_rx.recv()).await.ok().flatten(),
        }
    }
}

pub const IGNORE_MARK: &str = "【忽略】";

/// "暂停" -> Some(false) (pause), "继续(播放)" -> Some(true) (resume).
fn shortcut_core(text: &str) -> String {
    text.chars()
        .filter(|c| c.is_alphanumeric() && !"啊嗯哦呃哎呀吧了的嘛呢哈请".contains(*c))
        .collect::<String>()
        .to_lowercase()
}

/// A console shortcut matching what was said: the whole sentence, or a
/// sentence containing a phrase of four or more characters.
fn shortcut_for<'a>(shortcuts: &'a [crate::config::Shortcut], text: &str) -> Option<&'a crate::config::Shortcut> {
    let said = shortcut_core(text);
    if said.is_empty() {
        return None;
    }
    shortcuts
        .iter()
        .filter(|sc| !sc.run.trim().is_empty())
        .find(|sc| {
            let key = shortcut_core(&sc.say);
            !key.is_empty() && (said == key || (key.chars().count() >= 4 && said.contains(&key)))
        })
}

async fn serve_text(state: TurnState) -> anyhow::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    let _ = std::fs::remove_file(TEXT_SOCKET);
    let listener = tokio::net::UnixListener::bind(TEXT_SOCKET)?;
    std::fs::set_permissions(TEXT_SOCKET, std::fs::Permissions::from_mode(0o600))?;
    loop {
        let (stream, _) = listener.accept().await?;
        let state = state.clone();
        tokio::spawn(async move {
            let (read, mut write) = stream.into_split();
            let mut line = String::new();
            if BufReader::new(read).read_line(&mut line).await.is_err() {
                return;
            }
            let line = line.trim();
            let (aloud, text) = match line.strip_prefix("aloud:") {
                Some(rest) => (true, rest.trim()),
                None => (false, line),
            };
            if text.is_empty() {
                return;
            }
            info!("文字入口：{text}");
            let out = handle_text(&state, text, aloud).await;
            info!("文字入口结果：{}", out.replace('\n', " "));
            let _ = write.write_all(format!("{out}\n").as_bytes()).await;
        });
    }
}

/// One sentence through the same steps as a spoken one: shortcut, pause /
/// resume, fast command, goodbye, then the model.
async fn handle_text(state: &TurnState, text: &str, aloud: bool) -> String {
    let started = std::time::Instant::now();
    let expanded;
    let mut notes = String::new();
    let command = match shortcut_for(&state.config.agent.shortcuts, text) {
        Some(sc) => {
            notes = format!("快捷指令「{}」→ {}\n", sc.say, sc.run);
            expanded = sc.run.clone();
            expanded.as_str()
        }
        None => text,
    };
    if let Some(resume) = pause_intent(command) {
        let result = if resume { state.music.resume().await } else { state.music.pause().await };
        return format!("{notes}{}：{result}", if resume { "继续播放" } else { "暂停播放" });
    }
    if let Some(stop_music) = exit_intent(command) {
        if stop_music {
            state.music.stop().await;
        }
        return format!("{notes}结束对话（停止音乐：{stop_music}）");
    }
    let (tx, mut rx) = mpsc::unbounded_channel::<String>();
    let reply = state.agent.run_turn(command, tx).await;
    let mut first_piece = String::new();
    while let Ok(piece) = rx.try_recv() {
        if first_piece.is_empty() {
            first_piece = piece;
        }
    }
    let native = state.music.take_silent_reply();
    state.music.take_native_started();
    match reply {
        Ok(reply) => {
            if aloud && !native && !reply.text.contains(IGNORE_MARK) {
                let _ = state.device.speak(&reply.text).await;
            }
            format!(
                "{notes}回答：{}（写完 {:.1} 秒{}{}）",
                reply.text.replace('\n', " "),
                started.elapsed().as_secs_f64(),
                if reply.should_end { "，结束对话" } else { "" },
                if native { "，用声音回答，没有说话" } else { "" }
            )
        }
        Err(err) => {
            // Same safety net as the voice path.
            match fast_intent(command, state.music.has_track().await) {
                Some(fast) => {
                    let said = run_fast(state, &fast).await;
                    format!("{notes}大模型连不上，本地执行 {fast:?} → {said}")
                }
                None => format!("{notes}大模型调用失败：{err}"),
            }
        }
    }
}

#[derive(Debug, PartialEq)]
enum Fast {
    Volume(i64, bool), // value, absolute
    Next,
    Previous,
    Home(String),
    Time,
}

fn fast_core(text: &str) -> String {
    text.chars()
        .filter(|c| !c.is_whitespace() && !"，。！？、,.!?呃嗯啊吧呀哦".contains(*c))
        .collect::<String>()
        .trim_start_matches("请")
        .trim_start_matches("帮我")
        .trim_start_matches("给我")
        .to_string()
}

/// 七十 / 70 / 一百 / 三十五 -> number.
fn cn_number(text: &str) -> Option<i64> {
    let digits: String = text.chars().filter(|c| c.is_ascii_digit()).collect();
    if !digits.is_empty() {
        return digits.parse().ok();
    }
    let d = |c: char| "零一二三四五六七八九".chars().position(|x| x == c).map(|n| n as i64);
    let t: Vec<char> = text.chars().filter(|c| "零一二两三四五六七八九十百".contains(*c)).map(|c| if c == '两' { '二' } else { c }).collect();
    if t.is_empty() {
        return None;
    }
    if t == ['一', '百'] || t == ['百'] {
        return Some(100);
    }
    match t.iter().position(|&c| c == '十') {
        Some(i) => {
            let tens = if i == 0 { 1 } else { d(t[i - 1])? };
            let ones = t.get(i + 1).and_then(|&c| d(c)).unwrap_or(0);
            Some(tens * 10 + ones)
        }
        None if t.len() == 1 => d(t[0]),
        None => None,
    }
}

fn fast_intent(text: &str, music_on: bool) -> Option<Fast> {
    let t = fast_core(text);
    let n = t.chars().count();
    if n == 0 || n > 14 {
        return None;
    }
    let vol_word = t.contains("音量") || t.contains("声音") || t.starts_with("调");
    if vol_word && t.contains(['到', '至', '为', '成']) {
        if let Some(v) = cn_number(t.split(['到', '至', '为', '成']).last().unwrap_or("")) {
            if (0..=100).contains(&v) {
                return Some(Fast::Volume(v, true));
            }
        }
    }
    let up = ["大点声", "大声点", "声音大点", "声音大一点", "大声一点", "音量调大", "调大音量", "调大声音", "音量大点", "音量大一点", "调大一点", "大点儿声"];
    let down = ["小点声", "小声点", "声音小点", "声音小一点", "小声一点", "音量调小", "调小音量", "调小声音", "音量小点", "音量小一点", "调小一点", "小点儿声"];
    if up.iter().any(|w| t.contains(w)) && n <= 8 {
        return Some(Fast::Volume(10, false));
    }
    if down.iter().any(|w| t.contains(w)) && n <= 8 {
        return Some(Fast::Volume(-10, false));
    }
    if music_on {
        // 下一曲 / 换个台 / 切歌 / 播放下一首歌 / 换一首吧 / 跳过 ... in any wording.
        let mut k = t.clone();
        for w in ["歌曲", "音乐", "电台", "频道", "节目", "播放", "切换到", "切换", "切到", "换到", "来", "放", "这首", "这个", "的", "了", "一下", "歌"] {
            k = k.replace(w, "");
        }
        let k = k.trim_end_matches('吧');
        let next = ["下一首", "下一曲", "下一个", "下一台", "下一集", "下首", "下个", "下一", "换一首", "换一曲", "换一个", "换一台",
            "换台", "换首", "换个", "换个台", "换一个台", "换", "切", "跳过", "不想听", "不好听", "后一首", "后一个"];
        let prev = ["上一首", "上一曲", "上一个", "上一台", "上一集", "上首", "上个", "前一首", "前一个", "上一"];
        if next.contains(&k) {
            return Some(Fast::Next);
        }
        if prev.contains(&k) {
            return Some(Fast::Previous);
        }
    }
    if ["几点了", "现在几点", "现在几点了", "几点钟了", "现在是几点", "现在什么时间"].contains(&t.as_str()) {
        return Some(Fast::Time);
    }
    // 打开空调 / 关掉客厅灯 / 空调调到26度: a device word, short, nothing about music.
    let media = ["音乐", "歌", "电台", "广播", "故事", "播放", "节目", "喜马拉雅", "闹钟", "提醒", "音量", "声音"];
    let devices = ["空调", "灯", "窗帘", "风扇", "电视", "插座", "加湿器", "净化器", "扫地", "热水器", "地暖", "暖气", "台灯", "开关"];
    if n <= 12
        && devices.iter().any(|d| t.contains(d))
        && !media.iter().any(|m| t.contains(m))
        && ["打开", "关闭", "关掉", "开一下", "关一下", "调到", "调高", "调低", "设为", "开启", "把"].iter().any(|v| t.contains(v))
    {
        return Some(Fast::Home(t));
    }
    None
}

async fn run_fast(state: &TurnState, fast: &Fast) -> String {
    match fast {
        Fast::Volume(v, absolute) => {
            let now = crate::tools::current_volume().await.unwrap_or(50);
            let target = if *absolute { *v } else { now + v };
            let after = crate::tools::step_volume(target - now).await.unwrap_or(target.clamp(5, 100));
            format!("音量{after}")
        }
        Fast::Next | Fast::Previous => {
            let result = if *fast == Fast::Next { state.music.next().await } else { state.music.previous().await };
            match result.get("song").and_then(|s| s.get("name")).and_then(|n| n.as_str()) {
                Some(name) => {
                    info!("切到：{name}");
                    String::new()
                }
                None if result.get("playing").is_some() => String::new(),
                None => {
                    warn!("切换失败：{result}");
                    if *fast == Fast::Next { "没有下一个了".to_string() } else { "前面没有了".to_string() }
                }
            }
        }
        Fast::Time => {
            let now = chrono::Utc::now().with_timezone(&chrono_tz::Asia::Shanghai);
            format!("{}点{:02}分", now.format("%-H"), now.format("%M"))
        }
        Fast::Home(cmd) => {
            let quoted = format!("'{}'", cmd.replace('\'', ""));
            match crate::shell::run_shell(&format!("/data/open-xiaoai/home.sh {quoted}")).await {
                Ok(out) => {
                    let said = crate::music::unescape_unicode(out.stdout.trim());
                    let first = said.lines().next().unwrap_or("").trim();
                    if first.starts_with("小爱已经执行") || first.is_empty() {
                        "好了".to_string()
                    } else if first.chars().count() > 40 {
                        first.chars().take(40).collect()
                    } else {
                        first.to_string()
                    }
                }
                Err(_) => "家电那边没有响应".to_string(),
            }
        }
    }
}

fn pause_intent(text: &str) -> Option<bool> {
    let core: String = text
        .chars()
        .filter(|c| c.is_alphanumeric() && !"啊嗯哦呃哎呀吧了的嘛呢哈好请先".contains(*c))
        .collect();
    match core.as_str() {
        "暂停" | "暂停播放" | "暂停一下" | "暂停音乐" | "暂停暂停" => Some(false),
        "继续" | "继续播放" | "继续放" | "接着放" | "接着播" | "继续听" => Some(true),
        _ => None,
    }
}

/// Whether an utterance only means "stop / that's all" (returns whether music
/// should stop too). Fillers and punctuation are ignored; anything longer is
/// left to the model.
/// The panic button. It runs before the model, so it is generous about
/// wording: "stopped when I did not mean it" is a small annoyance, "would
/// not stop" is what makes the speaker unusable.
fn exit_intent(text: &str) -> Option<bool> {
    let core: String = text
        .chars()
        .filter(|c| c.is_alphanumeric() && !"啊嗯哦呃哎呀吧了的嘛呢哈好".contains(*c))
        .collect();
    if core.is_empty() {
        return None;
    }
    // "别停" "继续" mean the opposite; leave those to the model.
    if ["不要停", "别停", "不用停", "继续", "接着", "不要关", "别关"].iter().any(|w| core.contains(w)) {
        return None;
    }
    // Said in frustration: "关闭你听不懂吗", "我要让你停止说了多少遍".
    let demand = ["停止", "停下", "停掉", "别放", "别念", "别播", "别说", "闭嘴", "安静",
                  "关掉", "关闭", "不要放", "不要播", "不要念", "不想听", "别唱"];
    let hits: usize = demand.iter().map(|w| core.matches(w).count()).sum();
    let tail: String = core.chars().rev().take(8).collect::<Vec<_>>().into_iter().rev().collect();
    // Short sentence: any such word is the command. Long one (said in
    // frustration): only when it ends on it or keeps repeating it, so a
    // question that merely mentions stopping is left to the model.
    if hits > 0 && (core.chars().count() <= 24 || hits >= 2 || demand.iter().any(|w| tail.contains(w))) {
        return Some(true);
    }
    if core.chars().count() > 10 {
        return None;
    }
    // "停止播放音乐" "关掉电台" "别放了": stop the music, not just pause it.
    let stop_word = ["停止", "停掉", "关掉", "关闭", "别放", "不听", "不要放"].iter().any(|w| core.contains(w));
    let target = ["播放", "音乐", "电台", "广播", "歌", "放"].iter().any(|w| core.contains(w));
    if stop_word && target {
        return Some(true);
    }
    const EXIT: &[&str] = &[
        "关闭", "关掉", "退出", "停止", "停", "停止播放", "别说", "闭嘴", "安静", "没", "没有", "没事",
        "不用", "就这样", "再见", "拜拜", "结束", "不聊",
    ];
    // "关闭关闭", "没有没有" count too.
    let hit = EXIT.iter().any(|w| core == *w || core.replace(w, "").is_empty());
    hit.then(|| ["关", "停", "安静", "闭嘴"].iter().any(|w| core.contains(w)))
}

#[cfg(test)]
mod exit_tests {
    use super::exit_intent;

    #[test]
    fn short_stop_words_end_the_conversation() {
        assert_eq!(exit_intent("关闭，关闭。"), Some(true));
        assert_eq!(exit_intent("啊，嗯，没了。"), Some(false));
        assert_eq!(exit_intent("不用了，再见"), None.or(exit_intent("不用了，再见")));
        assert_eq!(exit_intent("好的，拜拜"), Some(false));
        assert_eq!(exit_intent("停止播放。"), Some(true));
        assert_eq!(exit_intent("播放王菲的如愿"), None);
        assert_eq!(exit_intent("停止播放音乐。"), Some(true));
        assert_eq!(exit_intent("关掉电台"), Some(true));
        assert_eq!(exit_intent("别放了"), Some(true));
        assert_eq!(exit_intent("把音量调小一点"), None);
        assert_eq!(exit_intent("退出。"), Some(false));
        assert_eq!(super::pause_intent("暂停。"), Some(false));
        assert_eq!(super::pause_intent("继续播放吧"), Some(true));
        assert_eq!(super::pause_intent("暂停一下把音量调小"), None);
    }

    #[test]
    fn stop_wins_however_it_is_said() {
        // What the user actually said this morning, word for word.
        for say in [
            "关闭？你听不懂吗？",
            "你为什么听不到我的意思？我要让你停止，说了多少遍了？停止！停止！停止！",
            "别念了",
            "安静一点",
            "闭嘴",
            "我不想听这个了",
            "能不能别放了",
            "把这个关掉",
        ] {
            assert_eq!(exit_intent(say), Some(true), "{say}");
        }
        // The opposite, and ordinary sentences that merely contain the word.
        for say in ["别停", "不要停下来", "继续播放", "停车场在哪里", "放首歌"] {
            assert_eq!(exit_intent(say), None, "{say}");
        }
    }
}

/// A short, varied sign-off when the user has gone quiet after talking.
async fn say_farewell(device: &Device) {
    const LINES: &[&str] = &[
        "如果没有别的问题，我就先退下了，有需要再叫我。",
        "那我先不打扰啦，有事随时喊我。",
        "好，我先休息一下，需要的时候叫我就行。",
        "没别的事的话我先下线了，随时叫我。",
    ];
    let pick = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.subsec_nanos() as usize)
        .unwrap_or(0)
        % LINES.len();
    if let Err(err) = device.speak(LINES[pick]).await {
        warn!("收尾语播报失败：{err:?}");
    }
}

async fn request_followup_capture() -> anyhow::Result<()> {
    // Let mipns process DialogFinish and return to idle before asking its
    // public mic-open path to start a real multi-round VPM stream.
    sleep(Duration::from_millis(100)).await;
    let result = crate::shell::run_shell(
        r#"ubus -t 1 -S call pnshelper event_notify '{"src":1,"event":0}'"#,
    )
    .await
    .map_err(|err| anyhow::anyhow!("failed to request follow-up capture: {err}"))?;
    if result.exit_code != 0 {
        anyhow::bail!(
            "pnshelper follow-up request failed exit={} stderr={}",
            result.exit_code,
            result.stderr.trim()
        );
    }
    let response: serde_json::Value = serde_json::from_str(&result.stdout)
        .context("pnshelper returned an invalid follow-up response")?;
    if response.get("code").and_then(serde_json::Value::as_i64) != Some(0) {
        anyhow::bail!(
            "pnshelper rejected follow-up capture: {}",
            result.stdout.trim()
        );
    }
    debug!("已请求继续收音");
    Ok(())
}

/// A failed recognition on a follow-up (music playing, or after an answer)
/// is usually background sound: end quietly instead of announcing an error
/// over the music. Only a fresh "小爱同学" gets the spoken apology.
async fn asr_failed(state: &TurnState, led: &DeviceConfig, follow_up: bool) {
    if follow_up || state.music.is_active().await {
        warn!("跟进聆听时语音识别失败，安静结束这一轮");
        cleanup_turn_leds(&state.device, led).await;
    } else {
        speak_service_error(&state.device, led, ASR_SERVICE_ERROR_PROMPT).await;
    }
}

async fn speak_service_error(device: &Device, led: &DeviceConfig, text: &str) {
    device.shut_led(led.led_thinking).await;
    device.show_led(led.led_speaking).await;
    if let Err(err) = device.speak(text).await {
        warn!("播报服务出错提示失败：{err:?}");
    }
}

async fn cleanup_turn_leds(device: &Device, led: &DeviceConfig) {
    for id in [
        led.led_speaking,
        led.led_thinking,
        led.led_user_speaking,
        led.led_listening,
    ] {
        device.shut_led(id).await;
    }
}

fn is_capture_timeout(err: &anyhow::Error) -> bool {
    err.to_string()
        .contains("timed out waiting for user speech")
}

#[cfg(test)]
mod fast_tests {
    use super::*;
    #[test]
    fn fast() {
        assert_eq!(fast_intent("声音调小至70%。", false), Some(Fast::Volume(70, true)));
        assert_eq!(fast_intent("音量调到七十。", false), Some(Fast::Volume(70, true)));
        assert_eq!(fast_intent("调小至70。", false), Some(Fast::Volume(70, true)));
        assert_eq!(fast_intent("大点声", false), Some(Fast::Volume(10, false)));
        assert_eq!(fast_intent("打开空调。", false), Some(Fast::Home("打开空调".into())));
        assert_eq!(fast_intent("把客厅灯关掉", false), Some(Fast::Home("把客厅灯关掉".into())));
        assert_eq!(fast_intent("换台。", true), Some(Fast::Next));
        assert_eq!(fast_intent("换台。", false), None);
        for say in ["下一曲", "下一曲。", "换个台", "播放下一首歌", "切歌", "换一首吧", "下一首歌曲", "跳过这首", "换一个电台", "下一个"] {
            assert_eq!(fast_intent(say, true), Some(Fast::Next), "{say}");
        }
        assert_eq!(fast_intent("上一曲", true), Some(Fast::Previous));
        assert_eq!(fast_intent("换成周杰伦的歌", true), None);
        assert_eq!(fast_intent("下一首放周杰伦", true), None);
        assert_eq!(fast_intent("现在几点？", false), Some(Fast::Time));
        assert_eq!(fast_intent("播放张学友的歌曲", true), None);
        assert_eq!(fast_intent("设置一个每天七点钟的闹钟", false), None);
        assert_eq!(fast_intent("空调开着的时候播放音乐", false), None);
        assert_eq!(cn_number("三十五"), Some(35));
        assert_eq!(cn_number("一百"), Some(100));
    }
}
