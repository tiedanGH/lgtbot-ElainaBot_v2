# LGTBot × ElainaBot 部署指南

> 把 [LGTBot](https://github.com/slontia/lgtbot) 桌游引擎接入 ElainaBot QQ 主框架，
> 一键编译 → 启动主框架即可使用，**无需任何额外配置**。

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

| 参数                      | CMake 选项             | 默认        | 说明                     |
|-------------------------|----------------------|-----------|------------------------|
| `--test` / `--no-test`  | `-DWITH_TEST`        | `OFF`     | LGTBot 内部单元测试（开发调试用）   |
| `--debug` / `--release` | `-DCMAKE_BUILD_TYPE` | `Release` | 构建类型                   |
| `--asan`                | `-DWITH_ASAN`        | `OFF`     | AddressSanitizer       |
| `--gcov`                | `-DWITH_GCOV`        | `OFF`     | 覆盖率统计                  |
| `--no-glog`             | `-DWITH_GLOG`        | `ON`      | glog 日志                |
| `--no-sqlite`           | `-DWITH_SQLITE`      | `ON`      | SQLite 持久化（关闭后无排行榜/历史） |
| `--no-games`            | `-DWITH_GAMES`       | `ON`      | 50+ 内置游戏插件             |

> 生产部署使用 `bash build.sh` 即可；只有需要跑 LGTBot 自带测试用例时才加 `--test`。
>
> ⚠️ 编译完成后 **请勿删除 `build/`** —— LGTBot 引擎运行时会从该目录动态加载游戏 `.so`。

---

## 4. 启动主框架（零配置）

```bash
cd ../..                # 回到 ElainaBot_v2 根目录
python3 main.py
```

启动应看到类似日志：
```
[插件:LGTBot] LGTBot 管理员配置：1 人
[插件:LGTBot] 初始化 LGTBot 引擎: db=plugins/lgtbot_qq/data/lgtbot.db
[插件:LGTBot] ✅ LGTBot 引擎已就绪
[插件:lgtbot_qq] 大型插件加载完成 (1 个处理器, 0.12s)
```

完成。在 QQ 群里 @ 机器人发送 `#帮助` 即可看到游戏列表。

---

## 5. 配置 `data/config.yaml`

首次启动时插件会自动生成下面这份模板：

```yaml
# LGTBot 内部管理员 openid 列表（不同于 ElainaBot 的 owner_ids）
#   这些用户可执行 LGTBot 管理命令（如 /管理 重置赛季 等）
#   留空则该机器人无 LGTBot 管理员；可在 Web 面板「日志」查 user_id
admin_uids: []
# 被动消息配额（5 条）耗尽时，等待用户点击「刷新」按钮的最长秒数
#   超时后会用旧引用强制尝试发送（多半会失败）
#   推荐 5–30 秒：过短玩家来不及点，过长命令响应延迟明显
refresh_wait_timeout: 10.0
```

**配置项说明：**

| 字段                     | 类型          | 默认     | 说明                            |
|------------------------|-------------|--------|-------------------------------|
| `admin_uids`           | `list[str]` | `[]`   | LGTBot 内部管理员的 QQ openid 列表    |
| `refresh_wait_timeout` | `float`     | `10.0` | 配额耗尽时阻塞等待用户点击刷新按钮的秒数；超时改为强制发送 |

**两种填写方式（任选其一）：**

**A. Web 面板在线编辑（推荐）**

1. ElainaBot 主面板 → 左侧「插件」→ 找到 `lgtbot_qq` → 点击「配置」
2. 直接编辑 `config.yaml`，保存即生效
3. 部分修改可能需要在「插件管理」里 reload 一下本插件

**B. 命令行直接编辑**

1. 让目标管理员在群里给 bot 发任意消息
2. Web 面板「日志」找到该用户的 `user_id`（即 openid）
3. 编辑 `plugins/lgtbot_qq/data/config.yaml`：
   ```yaml
   admin_uids:
     - 'AAAA-BBBB-CCCC-DDDD'
     - 'EEEE-FFFF-GGGG-HHHH'
   ```
4. 重启 ElainaBot 或在 Web 面板禁用→重新启用本插件

---

## 6. 卸载

```bash
# 框架运行中：通过 Web 面板「插件」选项卡禁用 LGTBot
# 或彻底移除：
rm -rf plugins/lgtbot_qq
```

> 安全关闭：插件 `@on_unload` 会调用 `release_bot_if_not_processing_games`，存在进行中游戏时会拒绝释放并打印警告，请等待对局结束或 `kill -9`。

---

## 7. 故障排查

| 现象                                                                 | 排查                                                                                                                                                                                                                                                                                                              |
|--------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `lgtbot_qq C++ 扩展未编译或导入失败`                                         | 重跑 `bash build.sh`；查看 `plugins/lgtbot_qq/lgtbot_qq.so` 是否存在                                                                                                                                                                                                                                                     |
| `libbot_core.so: cannot open shared object file`                   | `lgtbot_qq.so` 链接 `libbot_core.so` 但 ld.so 默认不搜 `build/`。本插件已用 `ctypes.CDLL` 在 import 阶段预加载 `build/lib*.so`；若仍报错，确认 `build/libbot_core.so` 存在，或手动 `LD_LIBRARY_PATH=plugins/lgtbot_qq/build python3 main.py`                                                                                                     |
| `ImportError: undefined symbol: ...boost::python...`               | Boost.Python 与编译时的 Python 版本不匹配 — `bash build.sh --clean` 重编译                                                                                                                                                                                                                                                   |
| `Load mod failed: ... undefined symbol: _ZN6google10LogMessage...` | glog 符号不可见。本插件已在 `main.py` 用 `RTLD_GLOBAL` 解决；若仍出现，确认未 `--no-glog` 编译，或试 `LD_PRELOAD=$(ldconfig -p \| grep libglog \| awk '{print $4}' \| head -1) python3 main.py`                                                                                                                                             |
| `图片渲染失败 (markdown2image 调用未生成文件)` 或 `markdown2image 二进制缺失`         | 本插件在 `import` 时会切到 `build/` 目录让 LGTBot 找到 `markdown2image`。若仍报错：① 检查 `plugins/lgtbot_qq/build/markdown2image` 是否存在并可执行（`chmod +x`）；② 手动测试 `cd build && echo '# hi' \| ./markdown2image --output /tmp/x.png --width 400 --nowith_css --noprint_info`；③ 部分游戏依赖字体，需 `apt install fonts-noto-cjk`。不影响游戏核心运行，仅影响图片输出 |
| `LGTBot 引擎启动失败`                                                    | 查 `build/plugins/` 下是否有各 `libgame.so`；首次编译需要等待所有 game 子项编译完成                                                                                                                                                                                                                                                    |
| 消息发不出去 / 无响应                                                       | 检查主框架日志中 sender 是否成功初始化；QQ Bot `appid/secret` 是否正确                                                                                                                                                                                                                                                              |
| 段错误 / `Segmentation fault (core dumped)`                           | 通常是 ASAN 编译产物未通过 LD_PRELOAD 启动 —— `bash build.sh --clean` 重编（默认 ASAN OFF）                                                                                                                                                                                                                                       |
