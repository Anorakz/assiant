#!/bin/sh
# 02-chat.sh -- 对话与工具调用（edge/llama；判据看 agent 日志与回复 ✓）
. "$(dirname "$0")/_lib.sh"

say "LLM 就绪（edge 后端 ✓）"
grep -aq "LLMProvider 就绪" "$AGENTLOG" && ok "LLMProvider 就绪 ✓" || bad "没看到 LLMProvider 就绪 ✗"
grep -aq "OpenAIClientError" "$AGENTLOG" && bad "出现过 openai SDK 缺失/调用错误 ✗" || ok "没有 OpenAIClientError ✓"
key=$(grep -a '^LLM_API_KEY' /data/assistant/llm/config/llm.env 2>/dev/null | cut -d= -f2- | tr -d '\r\n')
if [ -n "$key" ]; then
    code=$(timeout 10 curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $key" http://127.0.0.1:9000/v1/models 2>/dev/null)
    judge "$([ "$code" = 200 ] && echo 0 || echo 1)" "llama /v1/models 用当前 key = $code（200 ✓）"
fi

say "纯对话（不走工具 ✓）"
n=$(mark_log)
ans=$(timeout 200 "$ASSISTANT" chat --timeout 180 "用一句话说你好" 2>&1 | tail -2)
printf '  · 回复: %s\n' "$ans"
case "$ans" in
    *"没有响应"*|*"规则兜底"*|*"简易模式"*) bad "又降级到规则兜底了 ✗" ;;
    *"助手:"*) ok "模型答了 ✓" ;;
    *) bad "回复不像模型输出：$ans" ;;
esac
grab "chat"

say "工具调用（搜索 ⇒ 入队 ⇒ 队列非空 ✓）"
n=$(mark_log)
out=$(timeout 280 "$ASSISTANT" chat --timeout 260 "搜索 罗刹海市" 2>&1 | tail -2)
printf '  · 回复: %s\n' "$out"
since_log "$n" "搜" >/dev/null 2>&1 && ok "agent 日志里有搜索记录 ✓" || bad "日志里没看到搜索 ✗"
next=$("$ASSISTANT" video next 2>&1 | tail -1)
printf '  · video next: %s\n' "$next"
case "$next" in
    *"队列是空的"*) bad "队列仍然为空 ⇒ 工具没真的入队 ✗" ;;
    *) ok "队列里有东西、能起播 ✓" ;;
esac

finish
