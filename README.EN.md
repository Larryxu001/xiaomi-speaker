# Xiaomi Speaker Personal Assistant

> **中文：[README.md](README.md)**

This project turns the Xiaomi Smart Speaker Pro into an AI assistant you control yourself. You choose the language model. It searches the web, gives daily briefings the way you like them, and remembers your preferences. One sentence plays music, radio or stories, or controls your appliances. Everything is set up in a web console, and conversations and settings stay on your own server.

It has only been tested on the **Xiaomi Smart Speaker Pro (OH2P), firmware `1.62.2`**. Other models and firmware need your own porting, at your own risk.

## What it does

**Conversation**
- Your speech goes to the model you picked: DeepSeek or another OpenAI-compatible service, or an Anthropic-compatible one. It searches the web when needed and tries to finish the whole task.
- Everyday one-liners skip the model and run at once:
  - volume
  - turning appliances on or off
  - next track or station
  - the time
  - pause, resume, stop
- Voice shortcuts give a short name to a routine. For example, "good night" turns off the AC, lowers the volume and plays a sleep station.
- You can interrupt a reply at any time, and the speaker keeps listening for a follow-up.

**Daily help**
- **Scheduled tasks:**
  - Fixed reminders.
  - Spoken briefings from NewsNow hot lists, RSS, RSSHub or web pages.
  - You can tick several platforms; each one's top 10 is read in order.
- **Memory:** the assistant learns your preferences and uses them in its answers.
- **Ideas:** every evening it suggests a few things the speaker could do for you. They wait in the console, and you adopt or drop each one.

**Listening**
- **Music:**
  - NetEase Cloud Music, QQ Music and KuGou, using your own accounts. You log in with a QR code, logins are refreshed automatically, and you are told when one expires.
  - Your own Subsonic or Navidrome library.
- **Internet radio:** public stations worldwide, with favourites and categories.
- **Audio content:**
  - Ximalaya, Qingting FM, kids' stories, Dedao and similar services are played by XiaoAI's own service, using the memberships bound to your Xiaomi account.
  - Pause, stop and wake still go through us, so the two never talk over each other.
- **AirPlay:** the speaker can act as an AirPlay receiver.

**Appliances**
- Mi Home devices are controlled through XiaoAI's own cloud, by text command only. This does not reopen the native voice path.
- More tools, such as Home Assistant, can be added over MCP.

**Console**
- Web pages for:
  - models and keys
  - tasks
  - shortcuts
  - ideas
  - memory
  - music
  - radio
  - apps
  - extension tools
- A now-playing bar shows what is playing and offers pause, skip, stop and volume.
- The speaker polls the console every 20 seconds. The console never connects to the speaker, so it can run on a home computer or a cloud server.

## How it works

```text
microphone -> firmware's native mipns (wake word, echo cancellation, noise reduction, VAD)
           -> speech.usock -> xiaoai-agent (on the speaker, Rust)
           -> ASR -> shortcuts / fast commands / LLM + tools
           -> speech (native voice, or a voice synthesized by the console)
                 <->
           web console (your server, FastAPI): settings, tasks, music & radio, memory, ideas
```

- The firmware's own speech frontend still processes the microphone. The agent only receives cleaned 16 kHz PCM, so wake-word detection and echo cancellation work as they do on the stock speaker.
- Native XiaoAI never hears you, so it never answers first. Only what you explicitly ask for, such as Ximalaya content or an appliance command, is passed to it as text.
- The takeover happens at runtime without changing the rootfs. Stopping the agent restores native XiaoAI immediately.

## Layout

```text
.
├── xiaoai-agent/          # agent running on the speaker (Rust)
├── console/               # web console (FastAPI + Docker)
├── deploy/                # firmware patch, speaker scripts (say.sh, home.sh, poller), deploy scripts
├── apps/                  # self-hosted apps installable from the console's Apps page
├── installer/             # app installer for the server
├── upstream-open-xiaoai/  # sources and licenses of upstream firmware-patch material
└── AGENTS.md              # engineering rules and invariants
```

## Install in four steps

1. **Flash the patched firmware.** The only awkward step: a cable, a flashing
   tool and some risk. See [deploy/README.md](deploy/README.md).

2. **Run a console** on any machine that stays on and that the speaker can reach:

   ```bash
   git clone https://github.com/Larryxu001/xiaomi-speaker.git
   cd xiaomi-speaker/console && docker compose up -d
   ```

   Open `http://that-machine:8088` and set an admin password.

3. **Install on the speaker with one command.** The console's overview page
   prints it with your address and token already filled in:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/Larryxu001/xiaomi-speaker/main/deploy/install.sh | sh -s -- \
     --speaker root@192.168.1.100 \
     --console https://your-console \
     --token <device token from the console>
   ```

   It downloads the prebuilt binary, installs the scripts, writes the config,
   starts everything and checks that it is running. Add `--binary <path>` to
   use your own build when the machine cannot reach GitHub.

4. **Fill in the keys** on the console's Models & Keys page (DeepSeek is cheap
   and fast). The speaker picks them up within about 20 seconds.

Say "小爱同学" and start using it.

## Disable / uninstall

```sh
/data/open-xiaoai/start-agent.sh stop
```

This restores native XiaoAI; no reflash is needed. To keep it off after a reboot, remove the agent line from `/data/init.sh`.

## Disclaimer

This is an unofficial research project. It is not affiliated with or endorsed by Xiaomi. See [DISCLAIMER.md](DISCLAIMER.md). The project does not include, and will not add, any source that bypasses paid licensing.

## License and origin

Released under [LGPL-3.0](LICENSE). This project is a modified and substantially extended version of the open-source project xiaoai-agent. For the sources and licenses of the firmware-patch material, see [upstream-open-xiaoai/](upstream-open-xiaoai/).
