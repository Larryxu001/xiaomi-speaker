#!/bin/sh
# Polls the XiaoAI console every CHECK_INTERVAL seconds and:
#   1. pulls the current agent.yaml; if it changed, writes it and restarts
#      the agent via start-agent.sh
#   2. pulls at most one due announcement and reads it aloud with the
#      native TTS script, then acknowledges it
#   3. reports back a short status line + the agent's recent log tail
#
# Deliberately speaks plain text over HTTP, not JSON: this device's BusyBox
# has curl and ash but no jq, python, base64, or openssl. Every console
# endpoint this script calls returns either a raw file body or one line.
#
# Started from /data/init.sh; safe to run forever, never writes to the
# read-only root filesystem.
set -u

CONSOLE_URL=${XIAOAI_CONSOLE_URL:-}
DEVICE_TOKEN=${XIAOAI_DEVICE_TOKEN:-}
CHECK_INTERVAL=${XIAOAI_CONSOLE_POLL_INTERVAL:-20}
AGENT_HOME=${XIAOAI_AGENT_HOME:-/data/open-xiaoai}
AGENT_CONFIG=$AGENT_HOME/agent.yaml
AGENT_LOG=${XIAOAI_AGENT_LOG:-$AGENT_HOME/xiaoai-agent.log}
LAUNCHER=$AGENT_HOME/start-agent.sh
STATE_DIR=/tmp/xiaoai-console-poller
NEW_CONFIG=$STATE_DIR/agent.yaml.new
APPLIED=$AGENT_HOME/agent.yaml.applied

log() {
    printf '%s\n' "console-poller: $*"
}

[ -n "$CONSOLE_URL" ] || { log "XIAOAI_CONSOLE_URL not set; exiting"; exit 1; }
[ -n "$DEVICE_TOKEN" ] || { log "XIAOAI_DEVICE_TOKEN not set; exiting"; exit 1; }

mkdir -p "$STATE_DIR"

# Single instance: init.sh at boot and push-console-poller.sh may both start
# us; the PID file also lets the push script stop exactly this process.
PID_FILE=$STATE_DIR/poller.pid
old_pid=$(cat "$PID_FILE" 2>/dev/null || true)
if [ -n "$old_pid" ] && [ "$old_pid" != "$$" ] && kill -0 "$old_pid" 2>/dev/null; then
    log "already running as PID $old_pid; exiting"
    exit 0
fi
echo "$$" > "$PID_FILE"

sync_config() {
    rm -f "$NEW_CONFIG"
    # -f: on any HTTP error (403, 500, proxy page) curl fails instead of
    # handing us an error body that would overwrite a working agent.yaml.
    curl -sf -m 10 -H "X-Device-Token: $DEVICE_TOKEN" -o "$NEW_CONFIG" "$CONSOLE_URL/api/device/config.yaml" || return 1
    [ -s "$NEW_CONFIG" ] || return 1
    # Sanity check: a real config always has these top-level sections.
    grep -q '^runtime:' "$NEW_CONFIG" && grep -q '^llm:' "$NEW_CONFIG" || return 1
    # APPLIED is the config the agent was last restarted with successfully;
    # comparing against it (not agent.yaml) means a failed restart, e.g. one
    # that collided with a manual start, is retried on the next poll.
    if [ -f "$APPLIED" ] && cmp -s "$NEW_CONFIG" "$APPLIED"; then
        return 0  # unchanged and already running with it
    fi
    log "new config received; writing and restarting agent"
    cp "$NEW_CONFIG" "$AGENT_CONFIG"
    chmod 600 "$AGENT_CONFIG"  # contains API keys
    if "$LAUNCHER" restart >>"$STATE_DIR/restart.log" 2>&1 && "$LAUNCHER" status >/dev/null 2>&1; then
        cp "$NEW_CONFIG" "$APPLIED"
        chmod 600 "$APPLIED"
        log "agent restarted with the new config"
    else
        log "agent restart failed; will retry next poll ($(tail -n 1 "$STATE_DIR/restart.log" 2>/dev/null))"
    fi
}

handle_announcement() {
    line=$(curl -sf -m 10 -H "X-Device-Token: $DEVICE_TOKEN" "$CONSOLE_URL/api/device/announcement") || return 0
    [ -n "$line" ] || return 0
    ann_id=${line%%	*}
    text=${line#*	}
    [ "$ann_id" = "$line" ] && return 0  # no tab found, malformed; skip
    log "announcement #$ann_id: $text"
    if [ -x "$AGENT_HOME/say.sh" ]; then
        "$AGENT_HOME/say.sh" "$text" >/dev/null 2>&1
    else
        /usr/sbin/tts_play.sh --notify-pns "$text" >/dev/null 2>&1
    fi
    curl -s -m 10 -H "X-Device-Token: $DEVICE_TOKEN" -X POST "$CONSOLE_URL/api/device/announcement/$ann_id/ack" >/dev/null 2>&1
}

# Wake replies ("我在", "在呢"...) in the chosen voice: the firmware plays
# /usr/share/sound-vendor/<vendor>/wakeup_*.wav on the wake word. When the
# voice changes, fetch matching clips into /tmp and bind-mount them over the
# originals (nothing on the read-only rootfs changes; a reboot or the native
# voice restores the factory clips).
WAKE_CLIPS="wakeup_wozai.wav wakeup_zai_01.wav wakeup_zai_02.wav wakeup_ei_01.wav wakeup_ei_02.wav"
sync_wake_clips() {
    sig=$(curl -sf -m 10 -H "X-Device-Token: $DEVICE_TOKEN" "$CONSOLE_URL/api/device/voice") || return 0
    [ -n "$sig" ] || return 0
    [ "$sig" = "$(cat "$STATE_DIR/wake.sig" 2>/dev/null)" ] && return 0
    wake_dir=$STATE_DIR/wake
    mkdir -p "$wake_dir"
    if [ "$sig" != "native" ]; then
        for n in $WAKE_CLIPS; do
            curl -sf -m 30 -H "X-Device-Token: $DEVICE_TOKEN" -o "$wake_dir/$n.new" "$CONSOLE_URL/api/device/wake/$n" \
                || { log "wake clip $n not available; keeping current clips"; return 0; }
        done
    fi
    for f in /usr/share/sound-vendor/*/wakeup_*.wav; do
        grep -q " $f " /proc/mounts && umount "$f" 2>/dev/null
    done
    if [ "$sig" != "native" ]; then
        for n in $WAKE_CLIPS; do mv "$wake_dir/$n.new" "$wake_dir/$n"; done
        for d in /usr/share/sound-vendor/*; do
            for n in $WAKE_CLIPS; do
                [ -f "$d/$n" ] && mount --bind "$wake_dir/$n" "$d/$n"
            done
        done
    fi
    echo "$sig" > "$STATE_DIR/wake.sig"
    log "wake clips now use voice: $sig"
}

report_status() {
    agent_status=$("$LAUNCHER" status 2>&1)
    tail_log=""
    # Last 80 info/warning/error lines (the agent logs in Chinese); debug
    # detail, if enabled, would crowd them out.
    [ -r "$AGENT_LOG" ] && tail_log=$(tail -n 800 "$AGENT_LOG" 2>/dev/null | grep -E " (信息|警告|错误) " | tail -n 80)
    {
        printf 'agent: %s\n' "$agent_status"
        printf '--- recent log ---\n'
        printf '%s\n' "$tail_log"
    } | curl -s -m 10 -H "X-Device-Token: $DEVICE_TOKEN" -X POST --data-binary @- \
        "$CONSOLE_URL/api/device/status" >/dev/null 2>&1
}

log "starting; console=$CONSOLE_URL interval=${CHECK_INTERVAL}s"
while true; do
    sync_config
    sync_wake_clips
    handle_announcement
    report_status
    sleep "$CHECK_INTERVAL"
done
