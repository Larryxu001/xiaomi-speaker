# 小米音箱私人助理

> **English: [README.EN.md](README.EN.md)**

把 Xiaomi 智能音箱 Pro 变成一个由你自己掌控的 AI 助理：大模型由你选，会联网查资料、按你的习惯每天播报、记得你的偏好；放歌、放电台、讲故事、控制家电都用一句话搞定。所有设置都在一个网页控制台里完成，对话和配置都存在你自己的服务器上。

目前只在 **Xiaomi 智能音箱 Pro（OH2P）固件 `1.62.2`** 上测试通过，其他型号和固件需要自行适配并承担风险。

## 能做什么

**对话**
- 听懂你说的话交给你选的大模型（DeepSeek 等 OpenAI 兼容服务，或 Anthropic 兼容服务）回答；需要时会联网搜索，尽力把事情做完整。
- 常用的一句话指令不经过大模型，听到就做：调音量、开关家电、下一首/换台、问时间、暂停/继续/停止。
- 快捷指令：给常做的一串事起个短名字，比如说「晚安」就关空调、调小音量、放助眠电台。
- 播报中随时可以打断；说完一句后会继续听你接着说。

**每天主动帮你**
- 定时任务：固定提醒，或从 NewsNow 热榜、RSS、RSSHub、网页生成口播；可以勾选多个平台，按顺序各播前 10 条。
- 记忆：记住你的偏好，回答时自然地用上。
- 点子：每天晚上根据你的记忆、对话和任务，想几个音箱能帮你做的事，放在控制台由你决定要不要。

**听的**
- 音乐：网易云、QQ 音乐、酷狗（用你自己的账号扫码登录，登录状态自动续期，过期会提醒）、自建曲库（Subsonic/Navidrome）。
- 网络电台：全世界的公开电台，可收藏、按分类浏览。
- 有声内容：喜马拉雅、蜻蜓 FM、儿童故事、得到等交给小爱原生服务播放，用的是你小米账号绑定的会员权益；暂停、停止、唤醒照样由我们控制，两边不会抢着出声。
- AirPlay：可以当作 iPhone、Mac 的 AirPlay 音箱。

**家电**
- 米家设备通过小爱原生的云端执行（只发文字指令，不重新打开原生语音）。
- 也可以通过 MCP 接入 Home Assistant 等扩展工具。

**控制台**
- 网页上管理模型与密钥、定时任务、快捷指令、点子、记忆、音乐、电台、应用和扩展工具。
- 底部播放条显示正在放什么，可以暂停、切换、停止、调音量。
- 音箱每 20 秒主动来取一次新设置和该播报的内容，控制台从不主动连音箱，放在家里电脑或云服务器上都可以。

## 工作方式

```text
真实麦克风 → 固件原生 mipns（唤醒词、回声消除、降噪、VAD）
          → speech.usock → xiaoai-agent（音箱上，Rust）
          → 语音识别 → 快捷指令 / 快速指令 / 大模型 + 工具
          → 语音播报（原生音色或控制台合成的音色）
                ↕
          网页控制台（你的服务器，FastAPI）：设置、任务、音乐与电台、记忆、点子
```

- 麦克风仍由固件自己的语音前端处理，Agent 只接收处理好的 16 kHz PCM，所以唤醒、回声消除效果和原厂一样。
- 原生小爱听不到你说话，不会抢答；只有你明确要的内容（喜马拉雅、家电等）才以文字转给它执行。
- 接管在运行时完成，不改动 rootfs；停用后原生小爱立即恢复。

## 代码结构

```text
.
├── xiaoai-agent/          # 音箱上运行的 Agent（Rust）
├── console/               # 网页控制台（FastAPI + Docker）
├── deploy/                # 刷机补丁、音箱脚本（say.sh、home.sh、轮询）和部署脚本
├── apps/                  # 控制台「应用」页可一键安装的自托管应用
├── installer/             # 服务器上的应用安装器
├── upstream-open-xiaoai/  # 刷机补丁所用上游材料的来源与许可证
└── AGENTS.md              # 工程约定与不能破坏的边界
```

## 安装

### 1. 克隆仓库

```bash
git clone https://github.com/Larryxu001/xiaomi-speaker.git
cd xiaomi-speaker
```

### 2. 刷入补丁固件

用本仓库打包带 SSH 和 `/data/init.sh` 启动钩子的补丁固件并刷入，步骤见 [deploy/README.md](deploy/README.md)，OH2P 踩坑记录见 [deploy/OH2P_1.62.2_BUILD_NOTES.md](deploy/OH2P_1.62.2_BUILD_NOTES.md)。语音链路的接管不写进固件。

### 3. 部署控制台

```bash
cd console
docker compose up --build
```

打开 http://localhost:8088 ，第一次登录时设置管理密码。放到云服务器、绑定域名的做法见 [console/README.md](console/README.md)。

### 4. 构建 Agent

可以从 [Releases](https://github.com/Larryxu001/xiaomi-speaker/releases) 下载编译好的程序，也可以自己交叉编译：

```bash
rustup toolchain install 1.96.0
rustup target add armv7-unknown-linux-gnueabihf --toolchain 1.96.0
cargo install cargo-zigbuild
brew install zig   # macOS

(cd xiaoai-agent && cargo +1.96.0 zigbuild --release --target armv7-unknown-linux-gnueabihf.2.25)
```

一定要带 `.2.25` 后缀：音箱的 glibc 是 2.25，不带后缀编出来的程序在音箱上会缺符号。更多说明见 [AGENTS.md](AGENTS.md)。

### 5. 安装到音箱

```bash
ssh root@<音箱IP> 'mkdir -p /data/open-xiaoai'
scp -O xiaoai-agent/target/armv7-unknown-linux-gnueabihf/release/xiaoai-agent \
  xiaoai-agent/start-agent.sh deploy/say.sh deploy/home.sh \
  root@<音箱IP>:/data/open-xiaoai/
ssh root@<音箱IP> 'chmod +x /data/open-xiaoai/*'
```

再把音箱接到控制台（会安装轮询脚本，之后的配置都从控制台下发）：

```bash
XIAOAI_CONSOLE_URL=https://<你的控制台地址> \
XIAOAI_DEVICE_TOKEN=<控制台生成的设备令牌> \
deploy/push-console-poller.sh
```

先手动运行确认一切正常，再写入开机自启：

```sh
RUST_LOG=debug /data/open-xiaoai/start-agent.sh start

cat >/data/init.sh <<'EOF'
#!/bin/sh
exec /data/open-xiaoai/start-agent.sh start
EOF
chmod +x /data/init.sh
```

之后在控制台「模型与密钥」里填语音识别和大模型的服务商与 Key，大约 20 秒后音箱就会用上。

## 停用与卸载

```sh
/data/open-xiaoai/start-agent.sh stop
```

停止后会解除接管、恢复原生小爱，无需重新刷机。不想开机自动启用，就删掉 `/data/init.sh` 里启动 Agent 的那一行；彻底卸载再删除 `/data/open-xiaoai/` 里的相关文件。补丁固件带来的 SSH、禁用 OTA 等能力会保留。

## 免责声明

本项目为非官方技术研究项目，与小米及其关联公司不存在任何隶属、合作、授权、认可或背书关系。使用者应自行确认使用行为符合法律法规、平台规则和服务协议，并自行承担全部风险。详见 [DISCLAIMER.md](DISCLAIMER.md)。

本项目不提供、也不会加入绕过付费授权的内容来源：会员歌曲、付费专辑按你自己账号的权益播放。

## 许可证和来源

本项目以 [LGPL-3.0](LICENSE) 发布。
