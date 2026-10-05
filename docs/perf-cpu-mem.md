# CPU / 内存全周期测试（板端实测）

> 生成时间：2026-10-06 02:03:16
>
> 口径与 [bench.md](bench.md) 一致：数字随温度 / NPU 频率 / 后台负载波动，
> 所以每场景都跟**当时的板子状态**一起记（温度 / NPU / loadavg）。
> ⚠ **读不到的打 `-`，不编数字。**

采样：`scripts/monitor.sh --watch 2 --count N --csv`（18 列，含 agent/gui/llama 的 RSS、
NPU 频率与负载、soc/gpu 温度、loadavg）；逐线程热点另用 `scripts/cpu-sample.sh`（见各 `.threads.txt`）。

## 总表（每场景 1 行）

| 场景 | 样本 | CPU峰值% | CPU均值% | agent RSS 首→末 (MB) | Δagent | gui RSS 首→末 (MB) | Δgui | 温度峰°C | NPU负载峰% | NPU频率MHz | 末load1 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A1-sleep | 16 | 31.1 | 27.1 | 95→95 | +0 | 128→128 | +0 | 38.3 | 0 | 900 | 1.11 |

## 自动标出的可疑项

- （本批数据里没有触发阈值：Δagent RSS ≥ 5 MB 或 CPU 峰值 ≥ 50% ✓）

## ⚠ 本次必须如实说明的前提

- **串流在跑**（`VideoDec` / `VideoRecv` 等解码线程存在 ✓）⇒ 「待机」是**带串流的待机** ✓，
  与 `docs/image.md:58` 记的「VideoDec 80% 单核」直接相关 ✓。
- 板端 ssh（板→PC）本次已修好 ✓ ⇒ 「music 问状态失败」刷屏已停止 ✓（此前每 3 秒一条 ✓）。
- 串流客户端证书本次补齐到 `/data/assistant/creds/` ✓（此前缺失 ⇒ `_start_native` 失败 ✓）。
- ⚠ 判定串流是否连接**不能用** `ss -tn | grep 48010` ✗（视频流走 UDP ✓）⇒
  本次改用 **Agent 线程表里的 `VideoDec`/`VideoRecv`** ✓。

## 证据位置

- `E:\rk3568\tmp\perf\<场景>.csv`（主采样）／`<场景>.threads.txt`（逐线程）／`<场景>.meta.txt`（模式/串流/status）
- 板上原目录：`/data/assistant/perf/`
