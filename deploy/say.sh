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

# When TTS ends (pnshelper event 15) the firmware resumes whatever its own
# player still had loaded - a story, an album from an earlier request. If the
# agent did not start that content, it must not come back on by itself, so
# it is paused again for a few seconds after we stop talking. The agent
# touches the marker file while it is deliberately playing native content.
# Pause it before we speak, too: an alarm, a native morning routine or an
# album pushed from Xiaomi's cloud keeps playing underneath otherwise, and
# the listener hears two voices at once. The marker file means the agent
# started that content on purpose, so it is left alone.
hush_native() {
    [ -f /tmp/xiaoai-native-playing ] && return 0
    case "$(ubus -t 2 call mediaplayer player_get_play_status 2>/dev/null)" in
        *'"status\": 1'*)
            ubus -t 2 call mediaplayer player_play_operation \
                '{"action":"pause","media":"app_ios"}' >/dev/null 2>&1 ;;
    esac
}

settle_native() {
    [ -f /tmp/xiaoai-native-playing ] && return 0
    i=0
    while [ $i -lt 6 ]; do
        sleep 1
        case "$(ubus -t 2 call mediaplayer player_get_play_status 2>/dev/null)" in
            *'"status\": 1'*)
                ubus -t 2 call mediaplayer player_play_operation \
                    '{"action":"pause","media":"app_ios"}' >/dev/null 2>&1 ;;
        esac
        i=$((i + 1))
    done
}

play_url() {
    pause_music
    hush_native
    ubus call pnshelper event_notify '{"src":3,"event":14}' >/dev/null 2>&1
    miplayer -f "$1" >/tmp/xiaoai-say.log 2>&1
    result=$?
    ubus call pnshelper event_notify '{"src":3,"event":15}' >/dev/null 2>&1
    resume_music
    settle_native &
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

# The firmware's text_to_speech service refuses anything much over 400
# Chinese characters: measured on OH2P, 912 bytes was spoken in full and
# 1200 bytes came back as "Failed to call text_to_speech" after half a
# minute. It also exits 0 when that happens, and say.sh used to pass that
# on, so console-poller.sh marked three whole morning briefings as read
# while the room stayed silent. Long text is therefore spoken in
# sentence-sized pieces, and a piece that does not come out is reported as
# a failure so the announcement is not acknowledged as read.
NATIVE_CHUNK_BYTES=600

speak_chunk() {
    [ -n "$1" ] || return 0
    /usr/sbin/tts_play.sh --notify-pns "$1" >/tmp/xiaoai-say-chunk.log 2>&1
    rc=$?
    cat /tmp/xiaoai-say-chunk.log >>/tmp/xiaoai-say.log 2>/dev/null
    # tts_play.sh exits 0 even when the service failed, so look at what it said.
    grep -q "Failed to call text_to_speech" /tmp/xiaoai-say-chunk.log && return 1
    return $rc
}

native() {
    pause_music
    hush_native
    result=0
    if [ ${#text} -le $NATIVE_CHUNK_BYTES ]; then
        speak_chunk "$text" || result=1
    else
        # BusyBox sed matches bytes, so a bracket expression would cut
        # multibyte punctuation in half; match each mark as a whole string.
        printf '%s' "$text" | sed -e 's/。/。\n/g' -e 's/！/！\n/g' \
            -e 's/？/？\n/g' -e 's/；/；\n/g' -e 's/，/，\n/g' \
            >/tmp/xiaoai-say-parts 2>/dev/null
        buf=""
        while IFS= read -r part; do
            [ -n "$part" ] || continue
            if [ -n "$buf" ] && [ $((${#buf} + ${#part})) -gt $NATIVE_CHUNK_BYTES ]; then
                speak_chunk "$buf" || result=1
                buf="$part"
            else
                buf="$buf$part"
            fi
        done < /tmp/xiaoai-say-parts
        speak_chunk "$buf" || result=1
        rm -f /tmp/xiaoai-say-parts
    fi
    settle_native &
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
