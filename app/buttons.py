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

# dispatcher 在 state.pending_buttons[key] 里塞这个 sentinel,代表「下一条
# 文本回复发送时,用 build_game_action_buttons(state.current_game[key]) 现场
# 构造按钮」。延后构造的原因：游戏名由 C++ 桥接层异步通过 cb_match_announce
# 写入 current_game,dispatcher 接到用户命令的瞬间还拿不到。
PENDING_GAME_ACTION = '__pending_game_action__'


# 玩家在 LGTBot 房间里常用动作（创建/加入后追加在文本回复后）
def build_game_action_buttons(game_name: str | None = None) -> list[list[dict]]:
    """构造创建/加入房间后追加的按钮组。

    基础两个按钮（加入 / 退出）始终给出;若已知当前游戏名,再追加一行
    `/规则 <游戏名>` 按钮,玩家点一下就能直接查看规则。
    游戏名未知时（/随机游戏 进入时尚未知道引擎随机到了什么 / 进程重启
    后老房间）则不显示规则按钮,避免点出错误的规则。
    """
    rows: list[list[dict]] = [
        [
            {'text': '🟢 加入', 'data': '/加入', 'type': 2, 'style': 1},
            {'text': '🔴 退出', 'data': '/退出', 'type': 2, 'style': 3},
        ],
    ]
    if game_name:
        rows.append([
            {'text': f'📜 《{game_name}》规则',
             'data': f'/规则 {game_name}',
             'type': 2, 'style': 1},
        ])
    return rows

# 单独 @ 机器人时回复的欢迎菜单按钮
MENU_BUTTONS = [
    [
        {'text': '📖 查看帮助', 'data': '/帮助',    'type': 2, 'style': 4},
        {'text': '🎲 游戏列表', 'data': '/游戏列表', 'type': 2, 'style': 4},
    ],
    [
        {'text': '🎮 创建房间', 'data': '/新游戏',  'type': 2, 'style': 1},
        {'text': '📊 我的战绩', 'data': '/战绩',    'type': 2, 'style': 1},
    ],
    [
        {'text': '数字蜂巢', 'data': '/新游戏 数字蜂巢', 'type': 2, 'style': 0},
        {'text': '天赋云巢', 'data': '/新游戏 天赋云巢', 'type': 2, 'style': 0},
    ],
    [
        {'text': '五子棋', 'data': '/新游戏 五子棋',  'type': 2, 'style': 0},
        {'text': '困兽棋', 'data': '/新游戏 困兽棋',  'type': 2, 'style': 0},
    ],
    # 链接按钮（type=0）
    [
        {'text': '仓库',
         'link': 'https://github.com/tiedanGH/LGTBot_ElainaBot'},
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

# 触发"加入/退出"按钮的命令模式：/新游戏、/加入、/随机游戏
GAME_ACTION_RE = re.compile(r'^\s*/(新游戏|加入|随机游戏)(\s|$)')

# 单独 @ bot（content 为空）时回复的欢迎语
MENU_TEXT_HEADER = (
    '## 🎮 LGT-Bot 机器人\n'
    '\n'
    '---\n'
    '\n'
)
MENU_TEXT_BODY = (
    ''
)
# 兼容旧引用：拼接版
MENU_TEXT = MENU_TEXT_HEADER + MENU_TEXT_BODY
