#!/bin/sh
# 00-env.sh -- 环境与运行条件自检（服务 / 依赖 / 槽 / 密钥 / 数据盘）
. "$(dirname "$0")/_lib.sh"

say "服务"
for u in assistant.target agent.service agent-gui.service assistant-init.service sshd.service; do
    st=$(svc "$u")
    judge "$([ "$st" = active ] && echo 0 || echo 1)" "$u = $st"
done

say "槽与空间"
slot=$(tr ' ' '\n' < /proc/cmdline | grep slotsufix | cut -d= -f2)
info "当前槽: $slot"
misc=$(dd if=/dev/mmcblk0p2 bs=1 skip=2048 count=20 2>/dev/null | od -An -tx1 | tr -s ' ' | tr -d '\n')
info "misc: $misc"
left=$(df -k /data | awk 'NR==2 {print $4}')
judge "$([ "${left:-0}" -gt 1048576 ] && echo 0 || echo 1)" "/data 剩余 $((left / 1024)) MB（应 >1 GB ✓）"
[ -f /data/model/*.gguf ] && ok "模型在位 ✓" || bad "没有 /data/model/*.gguf ✗"

say "python 依赖（对话/工具/视觉/NPU）"
for m in numpy cv2 yaml psutil ruamel.yaml openai tokenizers zlib; do
    python3 -c "import $m" 2>/dev/null && ok "import $m ✓" || bad "import $m 失败 ✗"
done
python3 -c "from rknnlite.api import RKNNLite" 2>/dev/null && ok "rknnlite（NPU）✓" || bad "rknnlite 不可用 ✗"

say "板端连 PC 的密钥（音乐/串流状态查询要用 ✓）"
[ -f /root/.ssh/id_ed25519 ] && ok "客户端私钥在位 ✓" || bad "缺 /root/.ssh/id_ed25519（音乐洪流的根因 ✗）"
if [ -f /root/.ssh/id_ed25519 ]; then
    timeout 15 ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
        -p 22 "$(grep -a pc_user /data/assistant/config/config.yaml | head -1 | awk '{print $2}')@$(grep -a pc_host /data/assistant/config/config.yaml | head -1 | awk '{print $2}')" \
        "echo pc-ok" 2>/dev/null | grep -aq pc-ok && ok "板端→PC 免密 ✓" || bad "板端→PC 认证失败 ✗"
fi

say "IPC socket"
[ -S /tmp/agent.sock ] && ok "/tmp/agent.sock 在位 ✓" || bad "/tmp/agent.sock 不在 ✗（ota-confirm 就会失败 ✓）"

finish
