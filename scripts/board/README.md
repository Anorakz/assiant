# 板端功能自检与截图脚本

每个功能一个脚本，各自**自带判据**（PASS/FAIL）✓；统一入口一次跑完 ✓；
再配一套**统一截图**（板端抓裸帧 → PC 转 PNG ✓）。

## 跑测试

```sh
# 板端（脚本已随镜像在 /usr/lib/assistant/scripts/board/，或从仓库 scp 过去 ✓）
sh scripts/board/run-all.sh            # 全部功能
sh scripts/board/run-all.sh 01 03      # 只跑指定编号
```

| 脚本 | 覆盖的功能 | 主要判据 |
| --- | --- | --- |
| `00-env.sh` | 环境/服务/依赖/槽/密钥/数据盘 | 5 个 unit active ✓、8 个 python 模块可导入 ✓、板端→PC 免密 ✓、`/tmp/agent.sock` 在位 ✓ |
| `01-modes.sh` | 模式与资源释放 | 四种模式切换成功 ✓、切换推 status ✓、SLEEP 停 llama-server ✓ |
| `02-chat.sh` | 对话与工具调用 | `LLMProvider 就绪` ✓、**没有降级到规则兜底** ✓、搜索真的入队 ✓（`video next` 不报"队列是空的"） |
| `03-video.sh` | B 站视频 + **零拷贝** | `可以播了` ✓、`走零拷贝主路径` ✓、绘制 **fps ≥ 20** ✓、画帧间隔 ≤ 60 ms ✓、`vqueue=0` ✓、无 page flip ✓ |
| `04-music.sh` | 音乐（PC 侧出声） | 板端→PC 免密 ✓、neteasecli 可用 ✓、**最近 1 分钟"问状态失败"= 0** ✓（洪流回归） |
| `05-moonlight.sh` | 游戏串流 | 凭据在位 ✓、`PairStatus=1` ✓、解码线程（`VideoRecv/VideoDec/mpp_dec_*`）✓、mTLS `/applist`=200 ✓ |
| `06-wallpaper.sh` | 壁纸 | 目录里有图 ✓、next/prev 能切 ✓、GUI 收到 `wallpaper` 推送 ✓ |
| `07-gui-link.sh` | GUI 三态与 status 补推 | 镜像里带 `on_status`（T15-2-10e ✓）、GUI 显示"已连接" ✓、切模式触发的 status 被 GUI 收到 ✓ |
| `08-ota-slots.sh` | OTA / A-B 槽 | 至少一个槽 `prio≠0` ✓、当前槽 **`succ=1`**（tries 不再递减 ✓）、`ota-confirm` 结果 success ✓ |

退出码：**0 = 全部通过 ✓ / 1 = 有失败 ✗**（`run-all.sh` 会汇总成一行清单 ✓）。

## 统一截图

```sh
# ① 板端：跑一遍功能，在每个功能出画面那一刻抓 3 帧裸帧（800×1280×4 ✓）
CAPTURE=1 sh scripts/board/capture-all.sh          # 等价于 run-all.sh + 抓帧 + manifest.txt
#   目录：/data/assistant/shots/<功能>-<名字>.raw，清单 /data/assistant/shots/manifest.txt ✓

# ② PC：拉回并按帧切 PNG（挑颜色最丰富的一帧 ✓，自动剔黑屏/重复 ✓）
powershell -ExecutionPolicy Bypass -File scripts\board\pull-shots.ps1
#   产物：docs/images/shot-<功能>-<名字>.png ✓
```

为什么这样分两半 ✗：**板端没有 PIL**（也没有 `pngenc`/`jpegenc` ✗），所以转换必须在 PC ✓；
板子只负责"把那一刻的画面原样抓下来" ✓。抓帧元素是 Rockchip 的 **`kmssrc`**（`gst-rockchip` ✓）——
`/dev/fb0` 那条路在 DRM/EGLFS 下抓到的是**全黑** ✗，别用 ✓。

## 约定（改脚本时请遵守）

- 每个功能脚本**必须自己判 PASS/FAIL** ✓，不许"跑完就算过" ✗；
- 抓帧只在 `CAPTURE=1` 时发生 ✓（测试与截图共用同一套状态摆设 ✓，不写两份 ✗）；
- 判据要**看日志/看线程/看 HTTP 码**这类客观证据 ✓，不要只信 CLI 打印的"已切到…" ✓。
