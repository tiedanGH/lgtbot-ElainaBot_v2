"""LGTBot Web 面板拓展 —— 「LGTBot 机器人」侧边栏页面(单页多标签)。

子模块:
    main          页面注册入口 + 主 HTML 骨架(标题栏 / 重启按钮 / 标签导航
                  / 公共 CSS+JS)。`@on_load` 时通过 ``webui.register()``
                  把 PAGE_KEY 挂进框架的 `web_pages._registry`,卸载时
                  ``webui.unregister()`` 摘除。
    page_logs     「消息日志」标签的 HTML / JS / 数据生成
    page_users    「用户数据」标签的 HTML / JS / 数据生成(查 user_cache.db)
    message_log   日志缓冲(纯数据层):log_incoming / log_outgoing /
                  get_logs / clear_logs;被 callbacks 与 dispatcher 直接调用

后续如新增标签(统计、房间监控、排行榜等)请新建 ``page_*.py``,在
``main.py::_HTML_TEMPLATE`` 里加 tab nav + tab-pane 容器,并把新模块的
HTML/JS/data 拼进 ``_render_html``。
"""
