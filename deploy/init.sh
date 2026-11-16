#!/bin/sh
# 开机入口（由补丁固件 /etc/rc.local 调用）。
# 1) 持久化 root 家目录与 SSH 公钥（固件根分区只读，用绑定挂载覆盖）
mkdir -p /data/root/.ssh
grep -q ' /root ' /proc/mounts || mount --bind /data/root /root
if [ -s /data/root/.ssh/authorized_keys ]; then
    grep -q ' /etc/dropbear/authorized_keys ' /proc/mounts || \
        mount --bind /data/root/.ssh/authorized_keys /etc/dropbear/authorized_keys
fi
# 2) 启动控制台轮询脚本（拉取配置/定时播报；在后台常驻，不影响第 3 步）
if [ -f /data/open-xiaoai/console.env ] && [ -x /data/open-xiaoai/console-poller.sh ]; then
    (
        . /data/open-xiaoai/console.env
        export XIAOAI_CONSOLE_URL XIAOAI_DEVICE_TOKEN
        exec /data/open-xiaoai/console-poller.sh
    ) >>/data/open-xiaoai/console-poller.log 2>&1 &
fi
# 3) 启动 XiaoAI Agent（接管语音链路；失败会自动回滚到原生小爱）
exec /data/open-xiaoai/start-agent.sh start
