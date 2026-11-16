use std::future::Future;
use std::time::Duration;

use tokio::time::{sleep, Instant};
use tracing::{debug, warn};

use crate::asr::{RealtimeAsrEvent, RealtimeAsrSession};
use crate::config::CaptureConfig;
use crate::speech::{SpeechInput, SpeechTurn};
use crate::vad::{SpeechCollector, SpeechEvent};

/// Records one utterance. `playing` tells whether our own reply is still
/// being spoken: meanwhile the idle timer is held and speech must be
/// `boost` times louder to count (the echo of our own voice must not).
pub async fn record_utterance_during<F, Fut, R, RFut>(
    config: CaptureConfig,
    idle_timeout: Duration,
    turn: &mut SpeechTurn,
    playing: Option<std::sync::Arc<std::sync::atomic::AtomicBool>>,
    boost: f64,
    on_speech_start: F,
    on_speech_rejected: R,
) -> anyhow::Result<Vec<u8>>
where
    F: Fn() -> Fut + Send + Sync,
    Fut: Future<Output = ()> + Send,
    R: Fn() -> RFut + Send + Sync,
    RFut: Future<Output = ()> + Send,
{
    let idle_timeout = idle_timeout.max(Duration::from_secs(1));
    let idle_timer = sleep(idle_timeout);
    tokio::pin!(idle_timer);
    let mut collector = SpeechCollector::new(&config);
    let mut speech_started = false;
    let is_playing = || {
        playing
            .as_ref()
            .is_some_and(|p| p.load(std::sync::atomic::Ordering::Relaxed))
    };

    debug!("开始收音（本地能量检测断句）");
    loop {
        if is_playing() {
            idle_timer.as_mut().reset(Instant::now() + idle_timeout);
            collector.set_playback_boost(Some(boost));
        } else {
            collector.set_playback_boost(None);
        }
        tokio::select! {
            _ = &mut idle_timer, if !speech_started => {
                collector.log_noise_floor("timeout");
                let _ = turn.stop_capture().await;
                anyhow::bail!("timed out waiting for user speech");
            }
            input = turn.recv() => {
                match input {
                    Some(SpeechInput::Pcm(bytes)) => {
                        for event in collector.push(&bytes) {
                            match event {
                                SpeechEvent::SpeechStart(_) => {
                                    speech_started = true;
                                    on_speech_start().await;
                                }
                                SpeechEvent::SpeechChunk(_) => {}
                                SpeechEvent::SpeechRejected => {
                                    speech_started = false;
                                    idle_timer.as_mut().reset(Instant::now() + idle_timeout);
                                    on_speech_rejected().await;
                                }
                                SpeechEvent::Utterance(pcm) => {
                                    turn.stop_capture().await?;
                                    return Ok(pcm);
                                }
                            }
                        }
                    }
                    Some(SpeechInput::End) => {
                        anyhow::bail!("mipns ended the stream before an utterance was captured");
                    }
                    Some(SpeechInput::Cancelled) => {
                        anyhow::bail!("mipns cancelled the speech stream");
                    }
                    None => anyhow::bail!("speech.usock service stopped during capture"),
                }
            }
        }
    }
}

pub async fn record_utterance_streaming<F, Fut, C, CFut, R, RFut>(
    config: CaptureConfig,
    idle_timeout: Duration,
    turn: &mut SpeechTurn,
    on_speech_start: F,
    on_audio_chunk: C,
    on_speech_rejected: R,
) -> anyhow::Result<Vec<u8>>
where
    F: Fn() -> Fut + Send + Sync,
    Fut: Future<Output = ()> + Send,
    C: Fn(Vec<u8>) -> CFut + Send + Sync,
    CFut: Future<Output = anyhow::Result<()>> + Send,
    R: Fn() -> RFut + Send + Sync,
    RFut: Future<Output = anyhow::Result<()>> + Send,
{
    let idle_timeout = idle_timeout.max(Duration::from_secs(1));
    let idle_timer = sleep(idle_timeout);
    tokio::pin!(idle_timer);
    let mut collector = SpeechCollector::new(&config);
    let mut speech_started = false;

    debug!("开始收音（本地能量检测断句，流式识别）");
    loop {
        tokio::select! {
            _ = &mut idle_timer, if !speech_started => {
                collector.log_noise_floor("timeout");
                let _ = turn.stop_capture().await;
                anyhow::bail!("timed out waiting for user speech");
            }
            input = turn.recv() => {
                match input {
                    Some(SpeechInput::Pcm(bytes)) => {
                        for event in collector.push(&bytes) {
                            match event {
                                SpeechEvent::SpeechStart(prefix) => {
                                    speech_started = true;
                                    on_speech_start().await;
                                    on_audio_chunk(prefix).await?;
                                }
                                SpeechEvent::SpeechChunk(chunk) => {
                                    on_audio_chunk(chunk).await?;
                                }
                                SpeechEvent::SpeechRejected => {
                                    speech_started = false;
                                    on_speech_rejected().await?;
                                    idle_timer.as_mut().reset(Instant::now() + idle_timeout);
                                }
                                SpeechEvent::Utterance(pcm) => {
                                    turn.stop_capture().await?;
                                    return Ok(pcm);
                                }
                            }
                        }
                    }
                    Some(SpeechInput::End) => {
                        anyhow::bail!("mipns ended the stream before an utterance was captured");
                    }
                    Some(SpeechInput::Cancelled) => {
                        anyhow::bail!("mipns cancelled the speech stream");
                    }
                    None => anyhow::bail!("speech.usock service stopped during capture"),
                }
            }
        }
    }
}

/// Streams mipns PCM until the remote ASR service closes the utterance with
/// its own VAD. Local timing is only used before speech and as a hard fail-safe.
pub async fn record_utterance_server_vad_streaming<F, Fut, E, EFut>(
    config: CaptureConfig,
    idle_timeout: Duration,
    turn: &mut SpeechTurn,
    session: &mut RealtimeAsrSession,
    on_speech_start: F,
    on_speech_stop: E,
) -> anyhow::Result<String>
where
    F: Fn() -> Fut + Send + Sync,
    Fut: Future<Output = ()> + Send,
    E: Fn() -> EFut + Send + Sync,
    EFut: Future<Output = ()> + Send,
{
    let idle_timeout = idle_timeout.max(Duration::from_secs(1));
    let idle_timer = sleep(idle_timeout);
    tokio::pin!(idle_timer);
    let max_timer = sleep(Duration::from_secs(24 * 60 * 60));
    tokio::pin!(max_timer);
    let transcript_timer = sleep(Duration::from_secs(24 * 60 * 60));
    tokio::pin!(transcript_timer);
    let appender = session.appender();
    let mut speech_started = false;
    let mut input_closed = false;
    let mut chunks = 0_u64;

    debug!("开始收音（服务端断句，流式识别）");
    loop {
        tokio::select! {
            _ = &mut idle_timer, if !speech_started => {
                let _ = turn.stop_capture().await;
                anyhow::bail!("timed out waiting for user speech");
            }
            _ = &mut max_timer, if speech_started && !input_closed => {
                warn!("服务端没有断句，已在 {} 秒上限处结束收音", config.max_utterance_s);
                turn.stop_capture().await?;
                input_closed = true;
                transcript_timer.as_mut().reset(Instant::now() + idle_timeout);
                session.commit().await?;
                on_speech_stop().await;
            }
            _ = &mut transcript_timer, if input_closed => {
                anyhow::bail!("timed out waiting for Realtime ASR transcript");
            }
            event = session.next_event() => {
                match event? {
                    RealtimeAsrEvent::SpeechStarted { audio_start_ms } => {
                        debug!("检测到开始说话（{audio_start_ms} 毫秒）");
                        if !speech_started {
                            speech_started = true;
                            max_timer.as_mut().reset(Instant::now() + max_utterance_duration(&config));
                            on_speech_start().await;
                        }
                    }
                    RealtimeAsrEvent::SpeechStopped { audio_end_ms } => {
                        debug!("检测到说话结束（{audio_end_ms} 毫秒）");
                        if !input_closed {
                            turn.stop_capture().await?;
                            input_closed = true;
                            transcript_timer.as_mut().reset(Instant::now() + idle_timeout);
                            on_speech_stop().await;
                        }
                    }
                    RealtimeAsrEvent::Transcript(text) => {
                        if !input_closed {
                            turn.stop_capture().await?;
                        }
                        return Ok(text);
                    }
                }
            }
            input = turn.recv(), if !input_closed => {
                match input {
                    Some(SpeechInput::Pcm(bytes)) => {
                        chunks += 1;
                        if chunks == 1 {
                            debug!(bytes = bytes.len(), "SPEECH_USOCK_FIRST_ASR_CHUNK");
                        }
                        appender.append_pcm(bytes).await?;
                    }
                    Some(SpeechInput::End) => {
                        if !speech_started {
                            anyhow::bail!("mipns ended the stream before server VAD detected speech");
                        }
                        input_closed = true;
                        transcript_timer.as_mut().reset(Instant::now() + idle_timeout);
                        session.commit().await?;
                        on_speech_stop().await;
                    }
                    Some(SpeechInput::Cancelled) => {
                        anyhow::bail!("mipns cancelled the speech stream");
                    }
                    None => anyhow::bail!("speech.usock service stopped during capture"),
                }
            }
        }
    }
}

fn max_utterance_duration(config: &CaptureConfig) -> Duration {
    Duration::from_secs_f64(config.max_utterance_s.max(1.0))
}
