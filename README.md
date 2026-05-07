<div align="center">

![Logo](https://github.com/Slontia/lgtbot/blob/master/images/logo_transparent_colorful.svg)

# LGTBot × ElainaBot

**QQ 官方机器人版 LGTBot 适配插件**

![lang](https://img.shields.io/badge/language-Python%20%2B%20C%2B%2B20-green.svg)
![platform](https://img.shields.io/badge/platform-QQ%20Official%20Bot-blue.svg)
![license](https://img.shields.io/badge/license-LGPLv2-orange.svg)

</div>

---

## 致谢与项目来源

本插件是一个**适配层 / 集成包**，核心游戏引擎完全来自上游项目：

> **[LGTBot](https://github.com/Slontia/lgtbot)** — © [@Slontia](https://github.com/Slontia)
>
> *「LGT」源自日本漫画家甲斐谷忍《Liar Game》中的虚构组织「**L**iar **G**ame **T**ournament 事务所」*
>
> 一个基于 C++20 的多人文字推理游戏裁判机器人库，包含 50+ 种不同风格的游戏。游戏逻辑、引擎核心、图片渲染均由原作者 Slontia 设计实现。

本插件并未对 LGTBot 引擎做任何功能性修改，仅做：
1. 把 [LGTBot](https://github.com/Slontia/lgtbot) 适配 [ElainaBot_v2](https://github.com/ElainaCore/ElainaBot_v2) QQ 官方机器人框架
2. 处理 QQ 协议特有的限制（媒体消息合并、@mention 格式、按钮交互等）

**所有荣誉归原作者所有 —— 强烈建议先去 [LGTBot 主仓库](https://github.com/Slontia/lgtbot) 给原项目点 Star。**

| 上游项目                                                                  | 作者                                     | 协议     |
|-----------------------------------------------------------------------|----------------------------------------|--------|
| [LGTBot 引擎](https://github.com/Slontia/lgtbot)                        | [@Slontia](https://github.com/Slontia) | LGPLv2 |
| [lgtbot-khl](https://github.com/Slontia/lgtbot-khl) (KOOK 适配，本项目参考实现) | [@Slontia](https://github.com/Slontia) | LGPLv2 |
| [ElainaBot_v2 框架](https://github.com/ElainaCore/ElainaBot_v2)         | [@冷曦](https://github.com/lengxi-root)  | MIT    |
| 本适配层                                                                  | 铁蛋                                     | LGPLv2 |

---

## 简介

把 LGTBot 的 50+ 种游戏通过 ElainaBot 主框架接入到 **QQ 官方机器人**。

**作为 ElainaBot 插件零配置启动**：编译完 → 启动主框架 → 自动加载 → 在群里 @ 机器人即可玩。

## 工作原理

```
┌─────────────────────┐    @handler                ┌────────────────────────┐
│ ElainaBot 主框架    │ ──────────────────────►    │ plugins/lgtbot_qq/     │
│  (QQ Webhook / WS)  │                            │  main.py               │
│  MessageSender      │ ◄──── send_to_xxx ──────── │   ↓ Boost.Python       │
└─────────────────────┘     run_coroutine_         │  lgtbot_qq.so          │
                            threadsafe             │   ↓ FFI                │
                                                   │  libbot_core (C++)     │
                                                   │  + 50+ games           │
                                                   └────────────────────────┘
```

## 快速开始

详见 [DEPLOY.md](./DEPLOY.md)，三步：

```bash
# 1. 准备 lgtbot 子模块
cd plugins/lgtbot_qq
git clone --recursive https://github.com/Slontia/lgtbot.git lgtbot

# 2. 一键编译
bash build.sh

# 3. 启动主框架
cd ../.. && python3 main.py
```

## 关键特性

| 能力              | 实现                                                          |
|-----------------|-------------------------------------------------------------|
| **零配置自动加载**     | 作为 ElainaBot 插件，路径全部自包含在 `plugins/lgtbot_qq/`               |
| **消息合并**        | C++ 端聚合 "@玩家 文本 + 图片" 到单条媒体消息（避免 QQ 端拆成两条）                  |
| **玩家头像**        | 利用 `q.qlogo.cn/qqapp/{appid}/{openid}` 直链，LGTBot 渲染头像无需额外接口 |
| **回调按钮**        | `/新游戏` `/加入` 等命令自动附加交互按钮                                    |
| **欢迎菜单**        | 单独 @机器人时回复模板菜单，含「帮助 / 游戏列表 / 排行大图 / 战绩」等按钮                  |
| **Web 面板拓展页**   | 侧边栏「LGTBot 机器人」：消息日志 + 页面主题 + 收发/群私多维过滤 + 自动刷新              |
| **在线配置**        | `data/config.yaml` 在 Web 面板「插件 → 配置」可直接编辑保存                 |
| **优雅退出**        | 进行中对局拒绝释放引擎，避免数据丢失                                          |

## QQ 协议相关限制（已知）

QQ 官方机器人协议层面的限制，**所有 QQ Bot 都会遇到**，与 LGTBot 无关：

| 限制                                          | 影响                     | 当前应对                                |
|---------------------------------------------|------------------------|-------------------------------------|
| 主动消息需 `msg_id` / `event_id` 引用              | 倒计时类被动推送可能失败           | 5 分钟事件上下文缓存                         |
| Markdown 图片 URL 必须腾讯白名单 CDN                 | 游戏盘面图无法内嵌 markdown     | 用 `msg_type=7 + content` 合并         |
| 媒体消息（`msg_type=7`）的 content 不解析 `<@openid>` | 图文同条消息里的 @ 既不高亮也不 ping | 自动转为可读的 `@昵称`（牺牲 ping 换图文同条 + 文字可读） |
| 媒体消息无法附加按钮（QQ 协议）                           | 图片消息不能带按钮              | 仅文本回复附按钮                            |
| Linux only（Boost.Python + C++20）            | Windows 编译复杂度极高        | 仅在 Linux/WSL 上构建                    |

## 文件结构

```
plugins/lgtbot_qq/
├── main.py                  ElainaBot 插件入口（精简，仅元数据 + 生命周期）
├── lgtbot_qq.cc             C++ ↔ Python 桥接层（Boost.Python 模块）
├── CMakeLists.txt           构建配置（自动探测 Python / Boost.Python 版本）
├── build.sh                 一键编译脚本（依赖自检 + 多种编译选项）
├── DEPLOY.md                完整部署指南
├── README.md                本文档
├── LICENSE                  LGPLv2 许可证
│
├── app/                     插件功能模块（按职责拆分）
│   ├── __init__.py
│   ├── state.py             共享运行时状态容器
│   ├── boot.py              C++ 扩展导入 + 路径常量（必须最先加载）
│   ├── buttons.py           按钮模板 + 命令触发正则
│   ├── helpers.py           通用工具（sender / coro / mention / target_key）
│   ├── quota.py             被动消息引用配额管理（绕过 5 条限制）
│   ├── callbacks.py         C++ 引擎回调（cb_* 入口 + 异步发送实现）
│   ├── dispatcher.py        @handler 注册（消息派发 + INTERACTION 处理）
│   ├── config.py            data/config.yaml 读写
│   └── webui/               Web 面板拓展页（侧边栏「LGTBot 机器人」）
│       ├── __init__.py
│       └── message_log.py   消息日志页（日志缓冲 + HTML 模板 + 懒渲染注册）
│
├── .github/workflows/cmake.yml   GitHub Actions CI（Ubuntu 编译 + ctest）
│
├── lgtbot/                  ⬇ git submodule（LGTBot 上游源码）
│
├── build/                   ⚙️ CMake 编译产物（gitignored，运行时不可删）
│   ├── libbot_core.so
│   ├── markdown2image
│   └── plugins/<game>/libgame.so   各游戏插件
│
└── data/                    🗂 运行时数据（gitignored，自动创建）
    ├── config.yaml          插件配置（首次启动自动生成）
    ├── lgtbot.db            SQLite（用户 / 对局 / 排行榜）
    └── images/              引擎临时渲染图片
```

## 许可证

本适配层与 LGTBot 引擎保持一致，使用 **LGPLv2** 协议。

游戏逻辑、引擎核心、图片渲染等核心实现的著作权归 [@Slontia](https://github.com/Slontia) 所有，请遵守上游项目的 [LICENSE](https://github.com/Slontia/lgtbot/blob/master/LICENSE)。

## 链接

- 🎮 LGTBot 上游仓库：https://github.com/Slontia/lgtbot
- 🟢 KOOK 版（本项目参考实现）：https://github.com/Slontia/lgtbot-khl
- 🤖 ElainaBot_v2 主框架：[https://github.com/ElainaCore/ElainaBot_v2](https://github.com/ElainaCore/ElainaBot_v2)
- 📖 部署指南：[DEPLOY.md](./DEPLOY.md)
