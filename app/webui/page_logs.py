#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
「消息日志」标签 —— 仅保留 Python 逻辑(数据生成 + 模板加载)。

HTML / JS 片段在 ``templates/logs.html`` 和 ``templates/logs.js``,由
``webui/main.py`` 的 ``_render_html()`` 把 ``TAB_HTML`` 与 ``TAB_JS`` 插进
主模板的对应占位。

功能与重构前一致:筛选(全部/收到/发出/群聊/私聊)+ 自动刷新切换 + 主题切换。
"立即刷新" 按钮取消了 —— 自动刷新足够 ——「自动 / 主题」按钮放在筛选行的
最右侧,与左侧筛选按钮通过 spacer + stats 自然分隔。
"""

from __future__ import annotations

import json
import os

from . import message_log

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates')


def _load(name: str) -> str:
    with open(os.path.join(_TEMPLATE_DIR, name), 'r', encoding='utf-8') as f:
        return f.read()


TAB_HTML = _load('logs/logs.html')
TAB_JS = _load('logs/logs.js')


def get_data() -> str:
    """返回 logs JSON,可直接嵌入 ``<script id="log-data">``。

    JSON 中可能含 ``</script>``,先转义避免破坏外层 ``<script>`` 标签。"""
    data_json = json.dumps(message_log.get_logs(), ensure_ascii=False, default=str)
    return data_json.replace('</script>', '<\\/script>')
