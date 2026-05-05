# LGTBot × ElainaBot 部署指南

> 把 [LGTBot](https://github.com/slontia/lgtbot) 桌游引擎接入 ElainaBot QQ 主框架，
> 一键编译 → 启动主框架即可使用，**无需任何额外配置**。

---

## 0. 工作原理

```
┌──────────────────────┐    @handler              ┌────────────────────────┐
│ ElainaBot 主框架     │ ──────────────────────►  │ plugins/lgtbot_qq/     │
│  (QQ Webhook / WS)   │                          │  main.py               │
│  MessageSender       │ ◄──── send_to_xxx ────── │   ↓ Boost.Python       │
└──────────────────────┘   run_coroutine_         │  lgtbot_qq.so          │
                            threadsafe            │   ↓ FFI                │
                                                  │  libbot_core (C++)     │
                                                  │  + 25+ games           │
                                                  └────────────────────────┘
```

* **Python 侧**（`main.py`）：注册为标准 ElainaBot 插件，监听所有群 @ / 私聊消息。
* **C++ 侧**（`lgtbot_qq.so`）：Boost.Python 模块，封装 LGTBot 引擎。
* **桥接**：C++ 工作线程通过 `asyncio.run_coroutine_threadsafe` 调度回 ElainaBot 的事件循环，调用 `MessageSender` 发消息。

---

## 1. 系统依赖（仅编译时）

只支持 **Linux**（lgtbot 引擎依赖 POSIX/Boost.Python，Windows 上编译复杂度极高）。

### Ubuntu / Debian
```bash
sudo apt update
sudo apt install -y \
    build-essential cmake git \
    libcurl4-openssl-dev \
    python3-dev \
    libboost-python-dev libboost-system-dev \
    libgflags-dev libgoogle-glog-dev libsqlite3-dev
```

### CentOS / RHEL
```bash
sudo yum install -y \
    gcc-c++ cmake git \
    libcurl-devel python3-devel \
    boost-python3-devel boost-devel \
    gflags-devel glog-devel sqlite-devel
```

> **C++20 要求**：GCC ≥ 10 / Clang ≥ 12。Ubuntu 20.04 默认 GCC 9，需 `sudo apt install g++-10` 并 `export CXX=g++-10`。

---

## 2. 准备 lgtbot 源码

`lgtbot/` 是 lgtbot 上游仓库的 git 子模块。如果该目录为空：

```bash
cd plugins/lgtbot_qq
git clone --recursive https://github.com/slontia/lgtbot.git lgtbot
```

或在已有 git 仓库中：
```bash
git submodule update --init --recursive plugins/lgtbot_qq/lgtbot
```

---

## 3. 一键编译

```bash
cd plugins/lgtbot_qq
bash build.sh                 # 标准编译（Release，无测试）
bash build.sh --test          # 带 LGTBot 单元测试 (-DWITH_TEST=ON)
bash build.sh --clean         # 清理后重编译
bash build.sh --clean --test  # 清理 + 测试模式
bash build.sh -j 8            # 8 进程并行
bash build.sh --debug         # Debug 构建（含调试符号）
bash build.sh --asan          # 启用 AddressSanitizer 排查内存问题
bash build.sh --no-glog       # 关闭 glog 日志
bash build.sh --no-games      # 不编译内置游戏（仅引擎）
bash build.sh --help          # 查看所有参数
```

| 参数 | CMake 选项 | 默认 | 说明 |
|------|------------|------|------|
| `--test` / `--no-test` | `-DWITH_TEST` | `OFF` | LGTBot 内部单元测试（开发调试用） |
| `--debug` / `--release` | `-DCMAKE_BUILD_TYPE` | `Release` | 构建类型 |
| `--asan` | `-DWITH_ASAN` | `OFF` | AddressSanitizer |
| `--gcov` | `-DWITH_GCOV` | `OFF` | 覆盖率统计 |
| `--no-glog` | `-DWITH_GLOG` | `ON`  | glog 日志 |
| `--no-sqlite` | `-DWITH_SQLITE` | `ON`  | SQLite 持久化（关闭后无排行榜/历史） |
| `--no-games` | `-DWITH_GAMES` | `ON`  | 25+ 内置游戏插件 |

> 生产部署使用 `bash build.sh` 即可；只有需要跑 LGTBot 自带测试用例时才加 `--test`。

成功后插件目录会生成：
```
plugins/lgtbot_qq/
├── lgtbot_qq.so                     ← Python 扩展模块（必需）
├── build/
│   ├── libbot_core.so               ← 引擎核心库（必需）
│   └── plugins/                     ← 各游戏 .so（必需，引擎运行时扫描）
│       ├── alchemist/libgame.so
│       ├── mahjong/libgame.so
│       └── ... (25+ 个游戏)
└── lgtbot/                          ← C++ 源码（编译完成后可删）
```

> ⚠️ **不要删除 `build/` 目录** —— LGTBot 引擎运行时通过该路径动态加载游戏 `.so`。

---

## 4. 启动主框架（零配置）

```bash
cd ../..                # 回到 ElainaBot_v2 根目录
python3 main.py
```

启动日志中应看到：
```
[plugin] LGTBot 桌游 已加载
[plugin] LGTBot   初始化 LGTBot 引擎: db=plugins/lgtbot_qq/data/lgtbot.db
[plugin] LGTBot   ✅ LGTBot 引擎已就绪 —— 在群 @ 机器人或私聊发送 #帮助
```

完成。在 QQ 群里 @ 机器人发送 `#帮助` 即可看到游戏列表。

---

## 5. 数据目录结构（自动创建）

```
plugins/lgtbot_qq/
├── data/
│   ├── lgtbot.db            ← SQLite 数据库（用户 / 对局 / 排行榜）
│   ├── images/              ← 引擎生成的临时图片
│   └── admin_uids.txt       ← (可选) LGTBot 管理员 openid，逗号分隔
└── ...
```

> **如果想授予 LGTBot 内部管理员权限**（如执行 `/重置赛季` 等命令），新建 `data/admin_uids.txt` 写入 openid 列表即可。否则 ElainaBot 自身的 `owner_ids` 机制不会传递给 LGTBot。

---

## 6. 多 Bot 场景

如果 ElainaBot 同时挂了多个 QQ Bot（`config/bot.yaml` 配置多条），LGTBot 默认使用**第一个 Bot** 收发消息。多 Bot 隔离不在当前版本支持范围内。

---

## 7. QQ Official Bot 限制说明

| 项目 | 说明 |
|------|------|
| **Mention 格式** | `<@openid>`（QQ Markdown 渲染）— 已在 C++ 侧硬编码 |
| **主动推送** | QQ 严格限制主动消息，本插件用最近 5 分钟内的消息 `msg_id` 作为引用上下文。超时后的引擎主动消息（如游戏倒计时）会失败并仅记录日志 |
| **用户头像** | QQ 不公开 openid → 头像 URL 映射，`get_user_avatar_url` 始终返回空。需要头像渲染的游戏（如 alchemist）将使用占位符 |
| **群昵称** | 暂未实现 `GetUserNameInGroup`，统一返回事件中的 `username` |

---

## 8. 卸载

```bash
# 框架运行中：通过 Web 面板「插件」选项卡禁用 LGTBot
# 或彻底移除：
rm -rf plugins/lgtbot_qq
```

> 安全关闭：插件 `@on_unload` 会调用 `release_bot_if_not_processing_games`，存在进行中游戏时会拒绝释放并打印警告，请等待对局结束或 `kill -9`。

---

## 9. 故障排查

| 现象 | 排查 |
|------|------|
| `lgtbot_qq C++ 扩展未编译或导入失败` | 重跑 `bash build.sh`；查看 `plugins/lgtbot_qq/lgtbot_qq.so` 是否存在 |
| `ImportError: undefined symbol: ...boost::python...` | Boost.Python 与编译时的 Python 版本不匹配 — `bash build.sh --clean` 重编译 |
| `Load mod failed: ... undefined symbol: _ZN6google10LogMessage...` | glog 符号不可见。本插件已在 `main.py` 用 `RTLD_GLOBAL` 解决；若仍出现，确认未 `--no-glog` 编译，或试 `LD_PRELOAD=$(ldconfig -p \| grep libglog \| awk '{print $4}' \| head -1) python3 main.py` |
| `图片渲染失败 (markdown2image 调用未生成文件)` 或 `markdown2image 二进制缺失` | 本插件在 `import` 时会切到 `build/` 目录让 LGTBot 找到 `markdown2image`。若仍报错：① 检查 `plugins/lgtbot_qq/build/markdown2image` 是否存在并可执行（`chmod +x`）；② 手动测试 `cd build && echo '# hi' \| ./markdown2image --output /tmp/x.png --width 400 --nowith_css --noprint_info`；③ 部分游戏依赖字体，需 `apt install fonts-noto-cjk`。不影响游戏核心运行，仅影响图片输出 |
| `LGTBot 引擎启动失败` | 查 `_GAME_PATH` 下是否有 `*.so` 游戏插件；首次编译需要等待所有 game 子项编译完成 |
| 消息发不出去 / 无响应 | 检查主框架日志中 sender 是否成功初始化；QQ Bot `appid/secret` 是否正确 |
| 主动消息 (倒计时等) 失败 | QQ 限制，5 分钟内无活跃消息无法主动推送 — 引导玩家保持活跃即可 |
