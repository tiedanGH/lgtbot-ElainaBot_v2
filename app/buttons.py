#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按钮模板 + 触发命令正则。

设计要点：本插件的命令按钮**不使用 `enter` 字段**。
  原因：当 bot.yaml 配置 `message.button_enter_to_send: true` 时，框架
        keyboard.py 会把 `type=2 + enter=True` 强制转成 `type=1`（纯
        callback 按钮）。type=1 在 QQ 协议层永远不会回填输入框，仅触发
        INTERACTION_CREATE → bot ACK → 客户端弹"操作成功"，与"点按钮 →
        文字进输入框"的本意冲突。
  去掉 enter 后保持 type=2 不被转换：点击 → 文字回填到输入框 → 用户手动
  点发送。如果想要"自动发送"，用户需在 bot.yaml 把 button_enter_to_send
  设为 false 并在按钮里加回 enter=True。
"""

from __future__ import annotations
import re

# 玩家在 LGTBot 房间里常用动作（创建/加入/退出后追加在文本回复后）
GAME_ACTION_BUTTONS = [[
    {'text': '🟢 加入', 'data': '/加入', 'type': 2, 'style': 1},
    {'text': '🔴 退出', 'data': '/退出', 'type': 2, 'style': 3},
]]

# 单独 @ 机器人时回复的欢迎菜单按钮（4 个命令快捷键 + 3 个外链）
MENU_BUTTONS = [
    [
        {'text': '📖 查看帮助', 'data': '/帮助',     'type': 2, 'style': 4},
        {'text': '🎲 游戏列表', 'data': '/游戏列表', 'type': 2, 'style': 4},
    ],
    [
        {'text': '🏆 排行大图', 'data': '/排行大图', 'type': 2, 'style': 1},
        {'text': '📊 我的战绩', 'data': '/战绩',     'type': 2, 'style': 1},
    ],
    # 链接按钮（type=0）—— 不受 button_enter_to_send 影响，行为永远一致
    [
        {'text': '仓库',
         'link': 'https://github.com/tiedanGH/lgtbot-ElainaBot_v2'},
        {'text': '网站',
         'link': 'https://tiedan.site'},
        {'text': '官群',
         'link': ('https://qun.qq.com/universal-share/share?ac=1'
                  '&authKey=GLoA6W7KujPW%2B%2B%2FeirVZVVEn61q%2FAmLFyd9mkJ8u%2Bv0E%2B2IooquHavHi9iaJSxKK'
                  '&busi_data=eyJncm91cENvZGUiOiIxMDU5ODM0MDI0IiwidG9rZW4iOiJsTUFlUHZsdVJpSUhTc2dLSTBoeDI2M0IxS09kTGg3NzFsd1dvaVVLajVqTTIvRm9zaGlMTHBrekRIOGdVZHlaIiwidWluIjoiMjI5NTgyNDkyNyJ9'
                  '&data=IMqVKIvDehyMv2ooaqlgzql0-Q9XENN4pK6qGR1mqYoZH5AFDBMmrflWNEFN-EOLeKuJTxLABAwgaaUnUp-iyw'
                  '&svctype=4&tempid=h5_group_info')},
    ],
]

# 触发"加入/退出"按钮的命令模式：/新游戏、/加入、/退出、/随机游戏
GAME_ACTION_RE = re.compile(r'^\s*/(新游戏|加入|随机游戏)(\s|$)')

# 单独 @ bot（content 为空）时回复的欢迎语
MENU_TEXT = (
    '## 🎮 LGT-Bot 机器人\n'
    '\n'
    '---\n'
    '\n'
    '▸ 群内 @我 + `/新游戏 <名称>` 创建房间\n'
    '▸ 发送 `/加入` 即可参加游戏\n'
    '▸ 也可私聊体验单机游戏\n\n'
    '👇 点下方按钮快速开始'
)
