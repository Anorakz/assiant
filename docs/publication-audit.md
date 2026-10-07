# 公开前仓库审计

检查日期：2026-10-07。结论：**当前工作树已移除模板中的真实本地 LLM 密钥，历史仍需处理；不能据此直接认定公开准备完成。**

## 检查范围与方法

- 检查开始时主仓库有 507 个 Git 跟踪条目；扫描工作树中的文件内容及 `git rev-list --objects --all` 可达的 436 个提交、2,519 个 blob。
- `--all` 覆盖本地分支、远端跟踪分支和标签，包含已从当前分支删除的文件。凭据匹配结果只记录路径、行号、类别与对象 ID，不回显值。
- 检查私钥 PEM/OpenSSH 头、常见 API/GitHub/AWS/Slack token、密码哈希、凭据 URL、Cookie/API key/密码赋值、SSH 公钥和敏感配置路径；对命中项区分代码变量、测试值、模板占位符与实际凭据。
- 检查真实 Cookie、Wi-Fi 文件、公钥及编辑器配置的 Git 跟踪/忽略状态，检查子模块来源与 CI 中的凭据引用。

这是本地规则扫描与人工复核，**不是“没有任何秘密”的证明**。没有获取 GitHub 上新的远端 refs，
也未检查服务器保留的 PR refs、Release 附件、Actions 日志、缓存、LFS 存储或已构建镜像。
子模块使用公开上游 URL；主仓库记录的是 gitlink，子模块上游完整历史不在本次历史扫描范围。

## 已确认的发现

| 类别 | 位置 / 证据 | 状态 |
| --- | --- | --- |
| 实际本地 LLM API 密钥 | `config/config.example.yaml` 的 `llm.local_api_key`，`tests/test_llm_env.py` 的样本 | 当前工作树已分别改为公共占位符和独立测试值；已部署服务及历史中的旧值仍需处理 |
| 历史派生配置 | `llm/config/llm.env`，blob `fbd9fc22017cfa6c0e7a7914fb3552d137fb3c25` | 包含同一旧本地 LLM key；当前未跟踪，历史仍可读取 |
| 历史配置备份 | `config/config.example.yaml.bak-1790930445`，blob `08240e29a9288e3f789dcdac59cf363efc6ebddb`；`gui/config/gui.yaml.bak`，blob 前缀 `32e868745fae`（历史文件已删除） | 备份中也有旧 key；忽略/删除当前文件不能清除历史 |
| 历史代码、模板与基准日志 | `gui/config/gui.yaml.example`、`llm/bench_multimodal.py`、`llm/test_llm_client.py`、`sig/bench/run_bench.py` 及 `llm/qwen3.5_bench/*.json`（历史文件已删除） | 同一个旧 key 被复制到历史脚本/命令行记录；只清一个配置路径不够 |
| 固定 root 开发密码 | `image/buildroot/configs/rockchip_rk3568_kickpi_k1mini_release_defconfig` 的 `BR2_TARGET_GENERIC_ROOT_PASSWD` | 仍在配方；`post-build.sh` 同时允许 root 密码 SSH 登录，适用于现场开发，发行策略需另行收口 |
| 个人部署默认值 | `config/config.example.yaml` 的 PC 用户名和主机地址 | 当前模板改为 `your-pc-user` 与文档保留地址 `192.0.2.1`；历史文档/脚本仍有个人用户名、本机路径与私网 IP |
| 旧工作记录 | `todo.md`、`todo2.md` | 保留本地、取消 Git 跟踪并忽略；旧版本仍属于历史 |

旧本地 LLM key 的 SHA-256 指纹前 12 位为 `4e31aa935c3d`，用于确认多个命中是否为同一值。
它是 llama-server 的访问凭据，**不能因为不是云 API key 就认为没有风险**。
历史 `llm.env` 还曾配置 `LLM_HOST=0.0.0.0`；按实际服务监听地址、网络访问范围评估并轮换。

此次匹配和复核未发现实际 OpenSSH/PEM 私钥、云端 OpenAI key、GitHub token、AWS access key、
Slack token、密码哈希或已提交的真实 B 站/网易云 Cookie。代码中的凭据字段名、变量、mock 值与中文 Wi-Fi 占位符不按真实凭据处理。
公开上游地址和 Git 提交作者信息也不等于凭据；作者邮箱与历史截图如有隐私要求，需要另做匿名化审查。

## 本地文件与忽略规则

| 本地文件 | Git 状态 |
| --- | --- |
| `config/bilibili_cookie.json` | 已忽略、未跟踪 |
| `image/local/wifi.nmconnection`、`image/local/wifi-alt.nmconnection` | 已忽略、未跟踪 |
| `image/local/authorized_keys` | 已忽略、未跟踪；这是部署公钥，不是私钥 |
| `.vscode/settings.json` | 已忽略、未跟踪 |

新增规则覆盖全仓库 `*.bak-*`、派生 `llm.env`、LLM bin/run、另一 GUI 构建目录、
常见私钥/密钥容器与 `known_hosts`。保留 `image/local/*.example` 和配置示例模板。
忽略规则只阻止日后的普通 `git add`，`git add -f` 仍可绕过；已经提交的历史不会自动消失。

`image/install-into-sdk.sh` / `post-build.sh` 会把现场 Wi-Fi 和公钥文件注入镜像。
**源码未提交凭据，不代表镜像不含凭据**；分享 rootfs/update.img、sysroot 或 Release 附件前需单独检查。

## 公开前尚需完成

1. 在真实配置与派生 `llm.env` 中轮换旧本地 LLM key，并重启/核查实际服务；测试占位符不能作正式凭据。
2. 选择历史处理方式：保留原仓库历史时，替换旧 key 的所有历史副本并移除历史私有配置/备份和旧 TODO；或用当前已检查的源码建立新的干净公开仓库。
3. 历史处理必须覆盖拟公开的分支和标签，复扫后才推送；旧 clone、远端 PR 引用和发布附件需单独处理。当前未重写历史、强推或修改仓库可见性。
4. 发布可运行镜像前收口 root 登录策略，去除共享固定密码，采用现场独立凭据或密钥登录；确认 SDK/镜像中未带个人 Wi-Fi、SSH 访问授权或运行 Cookie。
5. 查看 GitHub Release / Actions 等外部产物及自托管 runner 设置；它们未纳入本次本地检查。源码公开与镜像发行是两次不同的检查。

完成以上项并复查后再切换仓库可见性；当前的检查报告只对本次本地仓库状态负责。

## 独立复核（2026-10-07 晚，值级判据）

方法：不搜"名字"（`SESSDATA` / `PRIVATE KEY` 这类标识符在代码里到处都是 ✓），而是拿**真实凭据的值**
去 `git log --all -S<值>` 逐个 pickaxe ✓ —— 命中才算泄漏 ✓。

| 判据 | 结果 |
| --- | --- |
| Sunshine 客户端**私钥**值（`E:\rk3568\local\creds\client.key` 正文） | **全部历史无命中 ✓**（旧 `todo.md` 里记的"只差一个 `git add -A`"没有真的发生 ✓） |
| Sunshine 客户端**证书**值（`client.pem` 正文） | **全部历史无命中 ✓** |
| OpenSSH / PEM 私钥文件头（`BEGIN … PRIVATE KEY`） | **任何提交里都没有 ✓**（pickaxe 命中 0） |
| 本地 llama key（``<新值——原文不入库；sha256 前 12 位 8b77029b…>``） | ⚠ **历史里有**：`llm/config/llm.env`（提交 `882cbd1`）与 `gui/config/gui.yaml.bak`（提交 `26545b5`）✓ —— 与上面 §已确认的发现同一把 ✓（指纹 `4e31aa935c3d` ✓） |
| 历史里曾**新增过**的敏感文件名 | `llm/config/llm.env`、`sig/config/sig.env`、`config/config.example.yaml.bak-1790930445`、`gui/config/gui.yaml.bak`、`image/local/authorized_keys.example`（示例 ✓ 无真实公钥） |
| `sig/config/sig.env` 内容 | 只有**路径与说明**（`SIG_TOKENIZER_PATH=/home/<用户名>/…` ⇒ 个人用户名 ✓），**没有密钥** ✓ |

⚠ **没有验成、因此不能算通过**的一项 ✗：**B 站 Cookie（SESSDATA）/ Wi‑Fi PSK / 板端 SSH 私钥**的
值级判据 **没跑成** —— 两次尝试从板端取凭据文件都失败（scp 未拿到 ✓），所以这三项目前只有
"名字扫描"（工作区 ✓ 历史标识符 ✓），**没有值级证据** ✗。公开前应当补上（取到值再 pickaxe ✓）。

其它会随历史一起公开的信息（如实列出 ✓）：
- 提交者身份：`Anorakz <2129741519@qq.com>`（390 次）、`DSH Agent <dsh-agent@rk3568.local>`（28 次）、
  `root <root@LAPTOP-EUVCHGBL.localdomain>`（23 次，含主机名 ✓）
- 私网地址 `192.168.137.x`：76 个文件里出现 ✓（部署文档/脚本 ✓）
- 个人用户名 `anorak` / `Anorak`：12 个文件里出现 ✓（含绝对路径 `/home/anorak/…`、`E:\rk3568\…`）

## 公开前必须完成（按顺序）

1. **轮换**：本地 llama key 换一个新的强随机值 ✓（`llm/config/llm.env`、镜像默认配置、已部署的板端与
   PC 两侧一起换 ✓），并确认 `llm/config/llm.env` 里的 `LLM_HOST` 不再是 `0.0.0.0` ✓。
2. **历史处理**：`git filter-repo`（或 BFG）把上面那些 blob 从**全部历史**里去掉 ✓，
   然后强推、并删除/重建受影响的 tag 与 **Release 附件**（附件不受 `.gitignore` 影响 ✗）。
   ⚠ 历史重写会让已有的 fork/clone 失效 ✓，公开前做才划算 ✓。
3. **确认默认登录策略**：配方里的 `BR2_TARGET_GENERIC_ROOT_PASSWD` 与 `post-build.sh` 放行的
   root 密码 SSH ✓ —— 发行版要么去掉固定密码、要么只留公钥登录 ✓（现场开发镜像与发行镜像分开 ✓）。
4. **补验第 3 节那条**（B 站 Cookie / PSK / 板端私钥的值级判据 ✓）。
5. **Readme 截图**：主界面整屏照片待补 ✓ —— 已试 `/dev/fb0`（GUI 走 DRM/EGLFS ✓，抓到的是**全黑**帧 ✗），
   可改用手机拍屏 ✓ 或 DRM writeback ✓（`card0-Writeback-1` 支持 1920×1080 ✓）。
