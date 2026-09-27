# 性能基线（板端实测）

> 目的只有一个：**回归能被机器发现**。数字本身不是成绩单 —— 它们随温度、NPU 频率、
> 后台负载波动，所以每次结果都跟**当时的板子状态**一起记（`temperature` / `npu_freq` / `loadavg`）。

跑法（板端，仓库根）：

```bash
bash scripts/bench.sh                                  # 只出数字（段 1+2）
bash scripts/bench.sh --with-stream                    # 加上端到端（PC 要开着 Sunshine）
bash scripts/bench.sh --baseline tests/data/bench/baseline.json   # 与基线比，超阈值非零退出
bash scripts/bench.sh --json /tmp/bench.json --write-baseline tests/data/bench/baseline.json
```

段与判据：

| 段 | 量什么 | 判据键 | 基线（2026-09-27，36 °C / NPU 600 MHz / load 0.57） |
| --- | --- | --- | --- |
| 1 **NPU 推理** | SigLIP `encode_image`（256×256×3 uint8，N=20，排除首次预热） | `npu_encode.p50_ms` / `.p95_ms` | **1827.3 / 1908.8 ms** |
| 2 **MPP 解码** | `mpp_decode_smoke tests/data/color_1280x720_8bit.h265`（12 帧） | `mpp_decode.ms_per_frame` | **75.4 ms/帧**（900 ms / 12 帧） |
| 3 **端到端** | 真串流 → 抓一帧 → `StudyWatcher.verdict()`（抓帧+编码+判据） | `end_to_end.ms` | 未测（那次没开 Sunshine，`skipped`） |

`--baseline` 的默认容差 **1.35×**（`--tolerance` 可改）。任一项超了就非零退出 ——
CI/本地都能拿它当门禁。

## 怎么读这些数字

- **段 1（1827 ms）** 与 T13-4 标定时的 **1.26–1.72 s/张** 同一量级，这次略慢 —— 说明这块
  板子的 NPU 前向延迟**本身就在 1.8 s 上下浮动**，做"每 30 分钟看一眼"的监督完全够用，
  但**别把它当成实时推理**（这也是"抓帧 → 判定"要 ~1.8 s 的原因）。
- **段 2（75 ms/帧）** 是**含进程启动 + MPP 初始化**的平均值（`mpp_decode_smoke` 整个跑完
  才 0.9 s，其中 12 帧解码）。它适合当"解码这条路还能不能跑、有没有慢一个数量级"的回归线；
  **不是**解码器的稳态吞吐（要那个得在长流上量，现在没做）。
- **段 3** 要 PC 开着 Sunshine（真帧 + 真 NPU）。`--with-stream` 时它等**非全黑**的帧
  （全黑帧是串流没起来/锁屏，判据见 `docs/study.md` §10）。
- 段 1 用**确定性合成帧**（256×256 渐变）：只量"过一遍 NPU 要多久"，不掺画面语义 ——
  语义那部分（准确率）在 `tests/board/t13_study_calib.py` 与 `docs/study.md`。

## 已知的边界（别把这些数字当别的用）

1. **温度/负载会动数字**：`npu_freq` 会自己降频（这块板子上看到过 780/600/200 MHz），
   所以基线里记了当时的值；跨状态比要谨慎。
2. **NPU 的 devfreq 节点名不固定**：脚本先探 `/sys/class/devfreq/fdab0000.npu/cur_freq`，
   再探 `/sys/class/devfreq/fde40000.npu/cur_freq`（这块板子是后者）；都没有就记 `null`
   （**不假装量过**）。
3. **段 2 的二进制只在交叉编译时构建**（`tests/board/mpp_decode_smoke`，刻意不随
   `deploy.ps1` 上板）：不在就报 `null` + 原因，不编数。
4. **不是功耗/散热报告**：这里只有延迟。要看 CPU/RAM/NPU 占用随时间的变化，用
   `scripts/monitor.sh`（T14-10）。
