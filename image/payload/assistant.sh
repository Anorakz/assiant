# ============================================================================
#  /etc/profile.d/assistant.sh — 交互式 shell 里的助手环境（镜像形态，T15-2-10b）
#
#  为什么要有它：两个 systemd 单元的 Environment= 只作用于服务进程；
#  串口/ssh 登进来的交互式 shell **不会**继承它们，于是：
#      python3 -m agent.cli status     → No module named agent
#      读配置                          → 找不到 /data/assistant/config/config.yaml
#  这里把同一套变量补上（与 systemd/image/*.service 里的取值**逐条对齐**，
#  tests/test_image_payload.py 会核对，防止两边漂）。
#
#  ⚠ 只设"默认值"（`:=`）：调用方显式给的值优先，便于现场临时换配置调试。
#  ⚠ 这个文件被 POSIX shell 与 bash 都会 source，所以**只用 POSIX 语法**。
# ============================================================================

# 代码在 rootfs（/usr/lib/assistant），状态在 userdata（/data/assistant）
case ":${PYTHONPATH}:" in
    *:/usr/lib/assistant:*) ;;
    *) PYTHONPATH="/usr/lib/assistant${PYTHONPATH:+:$PYTHONPATH}" ;;
esac
export PYTHONPATH

: "${AGENT_CONFIG_DIR:=/data/assistant/config}"
export AGENT_CONFIG_DIR
: "${AGENT_LOG:=/data/assistant/logs/agent.log}"
export AGENT_LOG
: "${AGENT_CRASH_DIR:=/data/assistant/logs/crash}"
export AGENT_CRASH_DIR
: "${LLM_ENV_FILE:=/data/assistant/llm/config/llm.env}"
export LLM_ENV_FILE
: "${LLM_STATE_DIR:=/data/assistant/llm}"
export LLM_STATE_DIR
