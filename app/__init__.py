"""LGTBot 插件子模块包。

模块划分：
    state         共享运行时状态容器
    boot          C++ 扩展导入 + 路径常量（顺序敏感，须最先加载）
    buttons       按钮模板 + 命令触发正则
    helpers       通用工具（sender/coro/mention/target_key）
    quota         被动消息引用配额管理（绕过 5 条限制的核心）
    callbacks     C++ 引擎回调（cb_* 同步入口 + 异步发送实现）
    dispatcher    @handler 注册（消息派发 + INTERACTION 处理）
    config        data/config.yaml 读写
    userdb        用户昵称 / 头像 SQLite 持久化（pending dict + 5min flush）
    uploader      图床上传调度 + 图片尺寸解析
    log_attribution  类级 monkey-patch MessageSender._log_push，让本插件
                     push 出去的消息在 Web 面板正确归类到「LGTBot 消息派发」
    webui/        Web 面板拓展页（侧边栏「LGTBot 机器人」）
        └─ message_log   消息日志页（当前唯一页面，未来可加 stats / admin 等）
"""
