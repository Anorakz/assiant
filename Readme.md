# Agent

开发机上的源码工作区。负责 C++/pybind11 底层、Python Agent Core、PySide6 GUI 的开发、交叉编译与部署。

目标板：RK3568 / Ubuntu 20.04 / aarch64 / glibc 2.31

---

## 目录结构
agent/
├── cmake/toolchain.cmake # aarch64 交叉编译工具链
├── native/ # C++ / pybind11
│ ├── ring_buffer.h
│ ├── image_rb.cpp/.h
│ ├── host_input_rb.cpp/.h
│ ├── moonlight_adapter.cpp/.h
│ ├── decoder.cpp/.h
│ ├── input_sender.cpp/.h
│ ├── binding.cpp
│ └── third_party/ # moonlight-common-c, pybind11
├── agent/ # Python Agent Core
│ ├── main.py
│ ├── io.py
│ ├── state.py
│ ├── scheduler.py
│ ├── router.py
│ ├── llm.py
│ ├── vision.py
│ ├── ipc.py
│ ├── config.py
│ └── tools/
├── gui/ # PySide6 GUI
│ ├── main.py
│ ├── panels.py
│ └── zmq_client.py
├── config/ # 配置模板
├── scripts/ # 构建/部署/测试脚本
└── tests/ # PC 侧单测

---

## 快速开始

# 初始化
git submodule update --init --recursive

# 交叉编译
scripts/build.ps1

# PC 侧单测
scripts/test-host.ps1

# 部署到板端
scripts/deploy.ps1

# 运行 GUI
python gui/main.py
关键约定
板端不手改代码，agent/、.so、VERSION 由 deploy.ps1 覆盖。

send_key / send_mouse 必须释放 GIL，不阻塞 asyncio。

ZeroMQ PUB 绑定 0.0.0.0:5555，SUB 连板端 :5556。

固件升级后重拉 sysroot 并重新交叉编译，否则 glibc 不匹配。

工具链：GCC 9.2-2019.12，路径 E:/rk3568/arm/
sysroot：E:/rk3568/sysroot（从板端 tar 同步，rsync 展开）
板端：root@192.168.137.30，目录 /home/kickpi/myproject/assitant/agent
Python：板端 3.8
GLIBC：板端 2.31，编译产物最高要求 2.17