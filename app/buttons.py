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


# 玩家在 LGTBot 房间里常用动作（C++ 桥接层 ClassifyMatchEvent 决定挂在哪条上）
def build_game_action_buttons(game_name: str | None = None,
                              include_rule: bool = False) -> list[list[dict]]:
    """构造房间相关按钮组。

    第一行恒为「加入 / 退出」。
    `include_rule=True`（仅新建房间消息）且游戏名已知时,第二行追加
    `/规则 <游戏名>` 按钮,玩家直接点开规则。
    其他消息（/加入 / /退出 等）传 ``include_rule=False`` 只显示第一行,
    避免在已知房间里反复提示规则。
    """
    rows: list[list[dict]] = [
        [
            {'text': '🟢 加入', 'data': '/加入', 'type': 2, 'style': 1},
            {'text': '🔴 退出', 'data': '/退出', 'type': 2, 'style': 3},
        ],
    ]
    if include_rule and game_name:
        rows.append([
            {'text': f'📖 《{game_name}》规则', 'data': f'/规则 {game_name}', 'type': 2, 'style': 4},
        ])
    return rows


def build_dissolve_buttons() -> list[list[dict]]:
    """房间因全员退出而解散时建议的两个引导按钮:看看别的游戏 / 直接再开一局。

    仅在「所有玩家都退出了游戏」/「所有玩家都强制退出了游戏」这两条解散
    广播上附加（见 LGTBot_ElainaBot.cc::ClassifyMatchEvent 的 ``all_left``
    分支）。/新游戏 时引擎前置发出的「游戏已解散，谢谢大家参与」(Terminate)
    不附,因为紧跟着会有真正的新建房间消息覆盖。
    """
    return [[
        {'text': '🎲 游戏列表', 'data': '/游戏列表', 'type': 2, 'style': 4},
        {'text': '🎮 创建房间', 'data': '/新游戏',  'type': 2, 'style': 1},
    ]]


# ──────── 未知指令引导(LGTBot_ElainaBot.cc::ClassifyMatchEvent 的 unknown_* 分支)──
# 「元指令帮助」按钮发 `/帮助`(带斜杠,bot_core 元指令路径处理);
# 「配置/游戏帮助」按钮发 `帮助`(不带斜杠,在 match 上下文里被分别解释为
# 等待房间的配置帮助 / 进行中游戏的游戏帮助)。

def build_unknown_meta_buttons() -> list[list[dict]]:
    """场景 1:用户没参与游戏 / 已加入但不在本群 —— 只给元指令帮助。"""
    return [[
        {'text': '❓ 元指令帮助', 'data': '/帮助', 'type': 2, 'style': 1},
    ]]


def build_unknown_config_buttons() -> list[list[dict]]:
    """场景 2:已在等待中的房间但用了未知的游戏配置 —— 配置帮助 + 元指令帮助。"""
    return [[
        {'text': '⚙️ 配置帮助', 'data': '帮助',  'type': 2, 'style': 4},
        {'text': '❓ 元指令帮助', 'data': '/帮助', 'type': 2, 'style': 1},
    ]]


def build_unknown_game_buttons() -> list[list[dict]]:
    """场景 3:游戏进行中,但用了未知的游戏指令 —— 游戏帮助 + 元指令帮助。"""
    return [[
        {'text': '🎮 游戏帮助', 'data': '帮助',  'type': 2, 'style': 4},
        {'text': '❓ 元指令帮助', 'data': '/帮助', 'type': 2, 'style': 1},
    ]]

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
