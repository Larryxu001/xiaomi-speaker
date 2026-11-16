#!/bin/sh
# say.sh TEXT - speak TEXT in the voice chosen in the console.
#
# Asks the console for a one-time ticket, then streams the MP3 with miplayer.
# The console answers "native" when XiaoAI's own voice is selected; if the
# console can't be reached at all, the native voice is used too, so the
# speaker never goes silent. Used by the agent (device.tts_command) and by
# console-poller.sh for scheduled announcements.
. /data/open-xiaoai/console.env 2>/dev/null

# Our own music (see agent.yaml play_url_command) pauses while we talk and
# resumes afterwards - unless something else had already paused it.
music_pid=$(cat /tmp/xiaoai-music.pid 2>/dev/null)
music_paused_by_us=""
pause_music() {
    [ -n "$music_pid" ] && [ -d "/proc/$music_pid" ] || return 0
    grep -q "^State:.*T" "/proc/$music_pid/status" 2>/dev/null && return 0
    kill -STOP "$music_pid" 2>/dev/null && music_paused_by_us=1 && touch /tmp/xiaoai-music.held
}
resume_music() {
    [ -n "$music_paused_by_us" ] && kill -CONT "$music_pid" 2>/dev/null
    rm -f /tmp/xiaoai-music.held
}

play_url() {
    pause_music
    ubus call pnshelper event_notify '{"src":3,"event":14}' >/dev/null 2>&1
    miplayer -f "$1" >/tmp/xiaoai-say.log 2>&1
    result=$?
    ubus call pnshelper event_notify '{"src":3,"event":15}' >/dev/null 2>&1
    resume_music
    # A killed miplayer means the agent interrupted us; don't repeat in the native voice.
    exit $result
}

# say.sh --stream URL: play a reply the agent is still sending sentence by sentence.
if [ "${1:-}" = "--stream" ]; then
    [ -n "${2:-}" ] || exit 1
    play_url "$2"
fi

native_only=0
if [ "${1:-}" = "--native" ]; then native_only=1; shift; fi
text=$1
[ -n "$text" ] || exit 0

native() {
    pause_music
    /usr/sbin/tts_play.sh --notify-pns "$text"
    result=$?
    resume_music
    exit $result
}
# say.sh --native TEXT: the agent already knows the native voice is chosen.
[ "$native_only" = 1 ] && native
[ -n "${XIAOAI_CONSOLE_URL:-}" ] && [ -n "${XIAOAI_DEVICE_TOKEN:-}" ] || native

ticket=$(curl -sf -m 3 -H "X-Device-Token: $XIAOAI_DEVICE_TOKEN" --data-urlencode "text=$text" \
    "$XIAOAI_CONSOLE_URL/api/device/tts" 2>/dev/null)
case "$ticket" in
    ""|native|*[!A-Za-z0-9_-]*) native ;;
esac

play_url "$XIAOAI_CONSOLE_URL/api/device/tts/$ticket.mp3"
