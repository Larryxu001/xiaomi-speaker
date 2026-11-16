use std::time::Duration;

use tokio::time::sleep;
use tracing::{debug, warn};

use crate::config::DeviceConfig;
use crate::shell::run_shell;

#[derive(Clone)]
pub struct Device {
    config: DeviceConfig,
}

impl Device {
    pub fn new(config: DeviceConfig) -> Self {
        Self { config }
    }

    pub async fn speak(&self, text: &str) -> anyhow::Result<()> {
        if text.trim().is_empty() {
            return Ok(());
        }
        self.run_template(&self.config.tts_command, &[("text", text)])
            .await
    }

    /// The native voice directly (no console round trip for a ticket).
    pub async fn speak_native(&self, text: &str) -> anyhow::Result<()> {
        if text.trim().is_empty() {
            return Ok(());
        }
        self.run_template("/data/open-xiaoai/say.sh --native {text}", &[("text", text)]).await
    }

    /// Plays a reply the console is still synthesizing (blocks until it ends).
    pub async fn play_stream(&self, url: &str) -> anyhow::Result<()> {
        self.run_template(&self.config.play_stream_command, &[("url", url)])
            .await
    }

    pub async fn play_url(&self, url: &str) -> anyhow::Result<()> {
        self.run_template(&self.config.play_url_command, &[("url", url)])
            .await
    }

    pub async fn stop_audio(&self) -> anyhow::Result<()> {
        self.run_script(&self.config.stop_audio_command).await
    }

    pub async fn pause_audio(&self) -> anyhow::Result<()> {
        self.run_script(&self.config.pause_audio_command).await
    }

    pub async fn resume_audio(&self) -> anyhow::Result<()> {
        self.run_script(&self.config.resume_audio_command).await
    }

    pub async fn duck_audio(&self) -> anyhow::Result<()> {
        self.run_script(&self.config.duck_audio_command).await
    }

    pub async fn unduck_audio(&self) -> anyhow::Result<()> {
        self.run_script(&self.config.unduck_audio_command).await
    }

    pub async fn play_heard_sound(&self) {
        if self.config.heard_sound_command.trim().is_empty() {
            return;
        }
        if let Err(err) = self.run_script(&self.config.heard_sound_command).await {
            warn!("播放提示音失败：{err:?}");
        }
    }

    /// Freeze / unfreeze the reply being spoken (the miplayer started by
    /// say.sh or tts_play.sh), e.g. while checking whether the user is
    /// really talking over it. Music is untouched.
    pub async fn hold_speech(&self, hold: bool) {
        let signal = if hold { "STOP" } else { "CONT" };
        let script = format!(
            "for p in $(pidof miplayer 2>/dev/null); do pp=$(awk '{{print $4}}' /proc/$p/stat 2>/dev/null); \
             case \"$(cat /proc/$pp/comm 2>/dev/null)\" in say.sh|tts_play.sh) kill -{signal} $p 2>/dev/null;; esac; done; true"
        );
        if let Err(err) = self.run_script(&script).await {
            warn!("暂停/恢复播报失败：{err:?}");
        }
    }

    pub async fn abort_current_output(&self) {
        if let Err(err) = self.run_script(&self.config.abort_command).await {
            warn!("停止当前播放失败：{err:?}");
        }
    }

    pub async fn show_led(&self, id: u8) {
        if !self.config.led_enabled || id == 0 {
            return;
        }
        if let Err(err) = self
            .run_script(&format!("/bin/show_led {id} >/dev/null 2>&1"))
            .await
        {
            debug!("点亮指示灯 {id} 失败：{err:?}");
        }
    }

    pub async fn shut_led(&self, id: u8) {
        if !self.config.led_enabled || id == 0 {
            return;
        }
        if let Err(err) = self
            .run_script(&format!("/bin/shut_led {id} >/dev/null 2>&1"))
            .await
        {
            debug!("关闭指示灯 {id} 失败：{err:?}");
        }
    }

    pub async fn blink_ready(&self, id: u8, duration: Duration) {
        self.show_led(id).await;
        sleep(duration).await;
        self.shut_led(id).await;
    }

    async fn run_template(&self, template: &str, values: &[(&str, &str)]) -> anyhow::Result<()> {
        let mut script = template.to_string();
        for (key, value) in values {
            let quoted = shell_words::quote(value);
            script = script.replace(&format!("{{{key}}}"), quoted.as_ref());
        }
        self.run_script(&script).await
    }

    async fn run_script(&self, script: &str) -> anyhow::Result<()> {
        let result = run_shell(script)
            .await
            .map_err(|err| anyhow::anyhow!("{err}"))?;
        if result.exit_code != 0 {
            anyhow::bail!(
                "command failed exit={} stderr={}",
                result.exit_code,
                result.stderr.trim()
            );
        }
        debug!(
            "device command ok: {}",
            script.lines().next().unwrap_or(script)
        );
        Ok(())
    }
}

impl std::fmt::Debug for Device {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Device").finish_non_exhaustive()
    }
}
