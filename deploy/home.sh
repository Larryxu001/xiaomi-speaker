#!/bin/sh
# home.sh "打开客厅灯" - run a smart-home command through XiaoAI's own cloud
# brain (the Mi Home devices bound to this speaker's Xiaomi account) and print
# what it answered. The native brain only executes; it doesn't speak (tts:0),
# the agent reads the result out in the chosen voice.
LOG=/tmp/mico_aivs_lab/instruction.log
cmd=$(printf '%s' "$1" | tr -d '"\\' | tr '\n' ' ')
[ -n "$cmd" ] || { echo "没有收到指令"; exit 1; }
out=$(ubus -t 10 call mibrain ai_service "{\"nlp\":1,\"nlp_text\":\"$cmd\",\"nlp_execute\":1,\"tts\":0}" 2>&1)
id=$(printf '%s' "$out" | grep -o 'dialog_id[^0-9a-f]*[0-9a-f]\{32\}' | grep -o '[0-9a-f]\{32\}$')
[ -n "$id" ] || { echo "小爱的云端没有响应：$out"; exit 1; }
i=0
while [ $i -lt 24 ]; do
    grep "$id" "$LOG" 2>/dev/null | grep -q '"name":"Finish","namespace":"Dialog"' && break
    sleep 0.5; i=$((i+1))
done
said=$(grep "$id" "$LOG" 2>/dev/null | grep -o '"text":"[^"]*"' | sed 's/^"text":"//; s/"$//' | grep -v "^$cmd\$" | head -5)
if [ -n "$said" ]; then
    printf '%s\n' "$said"
elif grep "$id" "$LOG" 2>/dev/null | grep -q '"name":"Finish","namespace":"Dialog"'; then
    # Its newer LLM path executes the command but often sends no text.
    echo "小爱已经执行了这条指令，只是没有回复文字（通常就是做好了）"
else
    echo "（小爱的云端没有给出文字回复）"
fi
