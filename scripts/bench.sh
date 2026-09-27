#!/bin/bash
# ============================================================================
#  scripts/bench.sh — 板端性能基线（T14-6）
#
#  跑什么（三段；每段都真的在这块板子上跑，不估算）
#  ---------------------------------------------------------------------------
#    1. **NPU 推理延迟**：SigLIP 双塔的 encode_image，N 次取 p50/p95（排除首次预热）。
#       ⚠ 用**确定性合成帧**（256×256 渐变）—— 只量"过一遍 NPU 要多久"，
#         不掺画面语义（那是标定/验收的事，见 tests/board/t13_study_calib.py）。
#    2. **MPP 解码延迟**：跑交叉编译出来的 `mpp_decode_smoke`（夹具是
#       tests/data/color_1280x720_8bit.h265，由 scripts/gen-decoder-testdata.py 生成），
#       按它解出的帧数算 ms/帧。⚠ 那个二进制**只在交叉编译时构建**、也不随 deploy 上板 ——
#       不在就如实报 null（不假装量过）。
#    3. **端到端**（`--with-stream`，默认跳过）：真 Runtime + 真串流 -> 抓一帧 -> 判定，
#       量"抓帧 + 编码 + 判据"的墙钟时间。要 PC 开着 Sunshine。
#
#  基线怎么比
#  ---------------------------------------------------------------------------
#    `--json <path>`      把结果写出去（含**板子状态**：温度 / NPU 频率 / load）
#    `--write-baseline`   把这次结果当成基线入库（tests/data/bench/baseline.json）
#    `--baseline <path>`  与基线比：任一指标准超过 `--tolerance`（默认 1.35 倍）就
#                         **非零退出** —— 这就是"回归能被机器发现"的那条线
#    ⚠ 数字随温度/负载波动，只做回归参考；所以基线里记了当时的状态。
#
#  跑法（板端, 仓库根）::
#      bash scripts/bench.sh --json /tmp/bench.json
#      bash scripts/bench.sh --baseline tests/data/bench/baseline.json
# ============================================================================
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

JSON_OUT=""
BASELINE=""
WRITE_BASELINE=""
TOLERANCE="1.35"
ENCODE_N="${ENCODE_N:-20}"
WITH_STREAM=0

while [ $# -gt 0 ]; do
    case "$1" in
        --json) JSON_OUT="${2:-}"; shift 2 ;;
        --baseline) BASELINE="${2:-}"; shift 2 ;;
        --write-baseline) WRITE_BASELINE="${2:-tests/data/bench/baseline.json}"; shift 2 ;;
        --tolerance) TOLERANCE="${2:-1.35}"; shift 2 ;;
        --with-stream) WITH_STREAM=1; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

echo "== 板子状态（数字要跟它一起看）"
TEMP="$(for z in /sys/class/thermal/thermal_zone*/temp; do [ -f "$z" ] && cat "$z"; done 2>/dev/null | sort -n | tail -1)"
NPU_FREQ="$(cat /sys/class/devfreq/fdab0000.npu/cur_freq /sys/class/devfreq/fde40000.npu/cur_freq 2>/dev/null | head -1 || echo '')"
LOAD="$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null || echo '')"
CPU_TEMP="$(( ${TEMP:-0} / 1000 ))"
printf '   温度 %s°C · NPU %s Hz · load %s\n' "${CPU_TEMP:-?}" "${NPU_FREQ:-?}" "${LOAD:-?}"

# ---------------------------------------------------------------- 1) NPU 推理 ---
echo "== 1) NPU 推理（SigLIP encode_image × $ENCODE_N）"
ENCODE_JSON="$(ENCODE_N="$ENCODE_N" python3 - <<'PY'
import json, os, time
import numpy as np
out = {"ok": False, "n": 0, "p50_ms": None, "p95_ms": None, "error": ""}
try:
    from agent.config import load_config
    from agent.vision.siglip.model import SiglipModel

    cfg = load_config("config")
    model = SiglipModel.from_config((cfg or {}).get("vision"))
    # 确定性合成帧（256×256×3 uint8）：只量 NPU 前向，不含画面语义
    xs = np.arange(256, dtype=np.uint16)
    frame = np.stack([np.tile(xs, (256, 1)), np.tile(xs[:, None], (1, 256)),
                      (np.tile(xs, (256, 1)) // 2)], axis=-1).astype(np.uint8)
    model.encode_image(frame)                        # 预热（首次含加载/缓存）
    n = int(os.environ.get("ENCODE_N", "20"))
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        model.encode_image(frame)
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    pick = lambda q: samples[min(len(samples) - 1, int(round(q * (len(samples) - 1))))]
    out.update(ok=True, n=n, p50_ms=round(pick(0.50), 1), p95_ms=round(pick(0.95), 1),
               first_ms=round(samples[0], 1), max_ms=round(samples[-1], 1))
except Exception as exc:                             # noqa: BLE001 - 失败要如实报
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
print(json.dumps(out, ensure_ascii=False))
PY
)"
echo "   $ENCODE_JSON"

# --------------------------------------------------------------- 2) MPP 解码 ---
echo "== 2) MPP 解码（mpp_decode_smoke × 夹具码流）"
DECODE_JSON="$(python3 - <<'PY'
import glob, json, os, subprocess, time
out = {"ok": False, "frames": 0, "ms_per_frame": None, "total_ms": None, "error": ""}
cands = glob.glob("build-rk3568/**/mpp_decode_smoke", recursive=True) + \
        glob.glob("tests/board/mpp_decode_smoke")
fixture = "tests/data/color_1280x720_8bit.h265"
if not cands:
    out["error"] = "没找到 mpp_decode_smoke（它只在交叉编译时构建：cmake -DCMAKE_CROSSCOMPILING）"
elif not os.path.exists(fixture):
    out["error"] = "夹具不在: %s（跑 scripts/gen-decoder-testdata.py 生成）" % fixture
else:
    binary = cands[0]
    t0 = time.perf_counter()
    proc = subprocess.run([binary, fixture], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    total = (time.perf_counter() - t0) * 1000.0
    text = (proc.stdout or b"").decode("utf-8", "replace")
    frames = len([ln for ln in text.splitlines() if ln.strip() and not ln.startswith("[")])
    out.update(ok=(proc.returncode == 0 and frames > 0), frames=frames,
               total_ms=round(total, 1),
               ms_per_frame=round(total / frames, 2) if frames else None,
               binary=binary,
               error="" if proc.returncode == 0 else "退出码 %d: %s" % (proc.returncode, text[-200:]))
print(json.dumps(out, ensure_ascii=False))
PY
)"
echo "   $DECODE_JSON"

# ------------------------------------------------------------- 3) 端到端(可选) ---
E2E_JSON='{"ok": false, "skipped": true, "ms": null, "error": "默认跳过（加 --with-stream 且 PC 开着 Sunshine）"}'
if [ "$WITH_STREAM" = "1" ]; then
    echo "== 3) 端到端（真串流 -> 抓帧 -> 判定）"
    E2E_JSON="$(python3 - <<'PY'
import asyncio, json, time
out = {"ok": False, "skipped": False, "ms": None, "error": ""}
async def main():
    from agent.config import load_config, config_path
    from agent.core.study_anchors import StudyAnchors
    from agent.core.study_stats import StudyStats
    from agent.core.study_watch import StudyWatcher
    from agent.main import Runtime
    runtime = Runtime(config=load_config("config"), config_path_used=config_path("config"),
                      start_native=True, start_terminal=False)
    try:
        await runtime._start_bus_and_io()
        await runtime._start_native()
        await runtime._start_study()
        for _ in range(100):
            reader = getattr(runtime, "image_reader", None)
            frame = await reader.read_latest() if reader is not None else None
            if frame is not None and int(frame.max()) > 8:      # 非全黑 = 真帧
                t0 = time.perf_counter()
                runtime._study_watch.verdict(frame)
                out.update(ok=True, ms=round((time.perf_counter() - t0) * 1000.0, 1))
                return
            await asyncio.sleep(0.3)
        out["error"] = "等不到非全黑的真帧（PC 没开 Sunshine？）"
    finally:
        try:
            await runtime.stop()
        except Exception:                                # noqa: BLE001
            pass
asyncio.run(main())
print(json.dumps(out, ensure_ascii=False))
PY
)"
    echo "   $E2E_JSON"
fi

# ------------------------------------------------------------------ 汇总/比较 ---
python3 - "$JSON_OUT" "$BASELINE" "$WRITE_BASELINE" "$TOLERANCE" "$CPU_TEMP" "$NPU_FREQ" "$LOAD" \
    "$ENCODE_JSON" "$DECODE_JSON" "$E2E_JSON" <<'PY'
import json, os, sys, time

(json_out, baseline, write_baseline, tolerance, temp, freq, load,
 encode_s, decode_s, e2e_s) = sys.argv[1:11]

def pick(text):
    """从一段输出里挑出**那行 JSON**。

    ⚠ RKNN 运行时会往 stdout 打自己的横幅（librknnrt 版本/驱动/模型信息），
      所以"整段当成 JSON 解析"必然炸（第一次跑就是这么炸的）。这里从后往前找
      第一行能解析成 object 的。
    """
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {}

encode, decode, e2e = (pick(x) for x in (encode_s, decode_s, e2e_s))

result = {
    "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
    "board": {"temp_c": int(temp or 0), "npu_freq_hz": freq or None, "loadavg": load or None},
    "npu_encode": encode,
    "mpp_decode": decode,
    "end_to_end": e2e,
}

def metric(payload, key):
    value = payload.get(key)
    return value if isinstance(value, (int, float)) else None

report = []
if baseline and os.path.exists(baseline):
    base = json.load(open(baseline, encoding="utf-8"))
    tol = float(tolerance)
    for group, key in (("npu_encode", "p50_ms"), ("npu_encode", "p95_ms"),
                       ("mpp_decode", "ms_per_frame"), ("end_to_end", "ms")):
        now, was = metric(result[group], key), metric(base.get(group, {}), key)
        if now is None or was is None:
            continue
        ok = now <= was * tol
        report.append((group, key, was, now, ok))
    print("== 与基线比（容差 %.2f×）" % tol)
    for group, key, was, now, ok in report:
        print("   %s %s %s: %s -> %s  %s" % ("✔" if ok else "✘", group, key, was, now,
                                            "OK" if ok else "**超阈值**"))
else:
    print("== 没给基线（--baseline）—— 只出数字")

if json_out:
    os.makedirs(os.path.dirname(os.path.abspath(json_out)), exist_ok=True)
    with open(json_out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("== 结果写到 %s" % json_out)

if write_baseline:
    os.makedirs(os.path.dirname(os.path.abspath(write_baseline)), exist_ok=True)
    with open(write_baseline, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("== 基线已写入 %s（⚠ 记得把它一起提交）" % write_baseline)

failed = [item for item in report if not item[4]]
print("== 结论: %s" % ("全部在阈值内" if not failed else "**%d 项超阈值**" % len(failed)))
sys.exit(1 if failed else 0)
PY
