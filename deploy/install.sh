#!/bin/sh
# install.sh - put the agent on a Xiaomi speaker in one command.
#
# Run it on a computer that can already SSH into the speaker (flash the
# patched firmware first, see deploy/README.md):
#
#   curl -fsSL https://raw.githubusercontent.com/Larryxu001/xiaomi-speaker/main/deploy/install.sh | sh -s -- \
#     --speaker root@192.168.1.100 --console https://console.example.com --token <device token>
#
# The console's overview page prints this command with your own address and
# token already filled in. Everything the speaker needs is installed under
# /data/open-xiaoai, the agent starts, and the result is checked.
set -eu

REPO=${XIAOAI_REPO:-Larryxu001/xiaomi-speaker}
BRANCH=${XIAOAI_BRANCH:-main}
SPEAKER=""
CONSOLE=""
TOKEN=""
BINARY=""

die() { echo "错误：$*" >&2; exit 1; }
say() { echo "==> $*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --speaker) SPEAKER=${2:-}; shift 2 ;;
        --console) CONSOLE=${2:-}; shift 2 ;;
        --token)   TOKEN=${2:-}; shift 2 ;;
        --binary)  BINARY=${2:-}; shift 2 ;;   # use a local build instead of the release
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        *) die "不认识的参数：$1" ;;
    esac
done

[ -n "$SPEAKER" ] || die "缺少 --speaker，例如 --speaker root@192.168.1.100"
[ -n "$CONSOLE" ] || die "缺少 --console，例如 --console https://console.example.com"
[ -n "$TOKEN" ]   || die "缺少 --token，到控制台「概览」页复制"
case "$CONSOLE" in http://*|https://*) ;; *) die "--console 要带 http:// 或 https://" ;; esac
CONSOLE=$(printf '%s' "$CONSOLE" | sed 's#/*$##')

command -v ssh >/dev/null 2>&1 || die "这台电脑上没有 ssh"
command -v scp >/dev/null 2>&1 || die "这台电脑上没有 scp"
command -v curl >/dev/null 2>&1 || die "这台电脑上没有 curl"

SSH="ssh -o ConnectTimeout=10"
say "连接音箱 $SPEAKER"
$SSH "$SPEAKER" 'echo ok' >/dev/null 2>&1 || die "连不上音箱。先确认刷过补丁固件、SSH 能登录（ssh $SPEAKER）"

ARCH=$($SSH "$SPEAKER" 'uname -m' 2>/dev/null || echo '?')
case "$ARCH" in armv7*|armv6*|arm) ;; *) die "音箱的处理器是 $ARCH，这个版本只支持 ARMv7 的小爱音箱 Pro" ;; esac

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

if [ -n "$BINARY" ]; then
    [ -f "$BINARY" ] || die "找不到 $BINARY"
    cp "$BINARY" "$WORK/xiaoai-agent"
    say "用本地编译好的程序：$BINARY"
else
    say "下载最新发布的音箱程序"
    URL="https://github.com/$REPO/releases/latest/download/xiaoai-agent"
    curl -fsSL -o "$WORK/xiaoai-agent" "$URL" \
        || die "下载失败：$URL（服务器访问不了 GitHub 时，先在自己电脑上编译，再加 --binary 路径）"
fi
[ -s "$WORK/xiaoai-agent" ] || die "下载到的程序是空的"

say "下载配套脚本"
for f in say.sh home.sh console-poller.sh init.sh; do
    curl -fsSL -o "$WORK/$f" "https://raw.githubusercontent.com/$REPO/$BRANCH/deploy/$f" \
        || die "下载 deploy/$f 失败"
done
curl -fsSL -o "$WORK/start-agent.sh" \
    "https://raw.githubusercontent.com/$REPO/$BRANCH/xiaoai-agent/start-agent.sh" \
    || die "下载 start-agent.sh 失败"

for f in say.sh home.sh console-poller.sh init.sh start-agent.sh; do
    sh -n "$WORK/$f" || die "$f 内容不完整，重试一次"
done

say "停掉旧版本（如果有）"
$SSH "$SPEAKER" 'test -x /data/open-xiaoai/start-agent.sh && /data/open-xiaoai/start-agent.sh stop >/dev/null 2>&1; \
    for p in $(ps w 2>/dev/null | grep "[c]onsole-poller.sh" | awk "{print \$1}"); do kill "$p" 2>/dev/null || true; done; true'

say "安装到 /data/open-xiaoai"
$SSH "$SPEAKER" 'mkdir -p /data/open-xiaoai'
# -O: the speaker's dropbear has no SFTP server.
scp -O -q "$WORK/xiaoai-agent" "$WORK/say.sh" "$WORK/home.sh" "$WORK/console-poller.sh" \
    "$WORK/start-agent.sh" "$SPEAKER":/data/open-xiaoai/
scp -O -q "$WORK/init.sh" "$SPEAKER":/data/init.sh

say "写入控制台地址"
printf 'XIAOAI_CONSOLE_URL=%s\nXIAOAI_DEVICE_TOKEN=%s\n' "$CONSOLE" "$TOKEN" > "$WORK/console.env"
scp -O -q "$WORK/console.env" "$SPEAKER":/data/open-xiaoai/console.env
$SSH "$SPEAKER" 'chmod +x /data/open-xiaoai/xiaoai-agent /data/open-xiaoai/*.sh /data/init.sh; \
    chmod 600 /data/open-xiaoai/console.env'

say "检查音箱能不能连上控制台"
$SSH "$SPEAKER" ". /data/open-xiaoai/console.env; \
    curl -fsS -m 15 -o /dev/null -H \"X-Device-Token: \$XIAOAI_DEVICE_TOKEN\" \
    \"\$XIAOAI_CONSOLE_URL/api/device/command\"" \
    || die "音箱连不上控制台。检查 --console 地址在音箱上能打开，以及 --token 是否复制完整"

say "启动"
$SSH "$SPEAKER" '( . /data/open-xiaoai/console.env; export XIAOAI_CONSOLE_URL XIAOAI_DEVICE_TOKEN; \
    exec /data/open-xiaoai/console-poller.sh ) </dev/null >>/tmp/xiaoai-console-poller.log 2>&1 &
    /data/open-xiaoai/start-agent.sh start >/dev/null 2>&1 || true'

say "等它起来"
i=0
while [ $i -lt 20 ]; do
    sleep 3
    if $SSH "$SPEAKER" 'pidof xiaoai-agent >/dev/null 2>&1'; then
        echo
        echo "装好了。"
        echo "  现在到控制台的「模型与密钥」页填上语音识别和大模型的服务商和 Key，"
        echo "  大约 20 秒后音箱就会用上，然后喊一声「小爱同学」试试。"
        echo "  看日志：ssh $SPEAKER 'tail -f /tmp/xiaoai-agent.log'"
        exit 0
    fi
    i=$((i + 1))
done

echo >&2
echo "程序没能启动起来。音箱上的日志：" >&2
$SSH "$SPEAKER" 'tail -n 20 /tmp/xiaoai-agent.log 2>/dev/null; tail -n 10 /tmp/xiaoai-agent-launcher.log 2>/dev/null' >&2 || true
exit 1
