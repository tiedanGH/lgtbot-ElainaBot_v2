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
| [lgtbot-khl (Kook 适配，本项目参考实现)](https://github.com/Slontia/lgtbot-khl) | [@Slontia](https://github.com/Slontia) | LGPLv2 |
| [ElainaBot 框架](https://github.com/ElainaCore/ElainaBot_v2)            | [@冷曦](https://github.com/lengxi-root)  | —      |
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

| 能力          | 实现                                                          |
|-------------|-------------------------------------------------------------|
| **零配置自动加载** | 作为 ElainaBot 插件，路径全部自包含在 `plugins/lgtbot_qq/`               |
| **消息合并**    | C++ 端聚合 "@玩家 文本 + 图片" 到单条媒体消息（避免 QQ 端拆成两条）                  |
| **玩家头像**    | 利用 `q.qlogo.cn/qqapp/{appid}/{openid}` 直链，LGTBot 渲染头像无需额外接口 |
| **回调按钮**    | `/新游戏` `/加入` `/退出` 等命令自动附加交互按钮                              |
| **欢迎菜单**    | 单独 @机器人无消息时回复模板菜单，含「帮助 / 游戏列表 / 排行大图 / 战绩」按钮                |
| **优雅退出**    | 进行中对局拒绝释放引擎，避免数据丢失                                          |

## QQ 协议相关限制（已知）

QQ 官方机器人协议层面的限制，**所有 QQ Bot 都会遇到**，与 LGTBot 无关：

| 限制                                                | 影响                          | 当前应对                                        |
|---------------------------------------------------|-----------------------------|---------------------------------------------|
| 主动消息需 `msg_id` / `event_id` 引用                    | 倒计时类被动推送可能失败                | 5 分钟事件上下文缓存                                 |
| Markdown 图片 URL 必须腾讯白名单 CDN                       | 游戏盘面图无法内嵌 markdown          | 用 `msg_type=7 + content` 合并                 |
| 媒体消息（`msg_type=7`）的 content 不解析 `<@openid>`        | 图文同条消息里的 @ 既不高亮也不 ping     | 自动转为可读的 `@昵称`（牺牲 ping 换图文同条 + 文字可读）   |
| 媒体消息无法附加按钮（QQ 协议）                                | 图片消息不能带按钮                   | 仅文本回复附按钮                                    |
| Linux only（Boost.Python + C++20）                  | Windows 编译复杂度极高             | 仅在 Linux/WSL 上构建                            |

## 文件结构

```
plugins/lgtbot_qq/
├── main.py              ElainaBot 插件入口（消息派发 / 回调实现 / 按钮注入）
├── lgtbot_qq.cc         C++ ↔ Python 桥接层（Boost.Python 模块）
├── CMakeLists.txt       构建配置（自动探测 Python / Boost.Python 版本）
├── build.sh             一键编译脚本（依赖自检 + 多种编译选项）
├── DEPLOY.md            完整部署指南
├── README.md            本文档
├── lgtbot/              LGTBot 上游源码（submodule）
└── data/                运行时数据（自动创建）
    ├── lgtbot.db
    ├── images/
    └── admin_uids.txt   (可选) LGTBot 管理员白名单
```

## 许可证

本适配层与 LGTBot 引擎保持一致，使用 **LGPLv2** 协议。

游戏逻辑、引擎核心、图片渲染等核心实现的著作权归 [@Slontia](https://github.com/Slontia) 所有，请遵守上游项目的 [LICENSE](https://github.com/Slontia/lgtbot/blob/master/LICENSE)。

## 链接

- 🎮 LGTBot 上游仓库：https://github.com/Slontia/lgtbot
- 🟢 KOOK 版（本项目参考实现）：https://github.com/Slontia/lgtbot-khl
- 🤖 ElainaBot_v2 主框架：[https://github.com/ElainaCore/ElainaBot_v2](https://github.com/ElainaCore/ElainaBot_v2)
- 📖 部署指南：[DEPLOY.md](./DEPLOY.md)
