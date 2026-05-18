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
                              include_rule: bool = False,
                              include_join_leave: bool = True) -> list[list[dict]]:
    """构造房间相关按钮组。

    `include_join_leave=True`(群聊默认)时,第一行是「加入 / 退出」;私信
    场景调用方传 False 跳过这一行,因为 DM 里玩家通常自己就是房主或经
    match_id 加入,/加入 这种群内简写并不适用。
    `include_rule=True`(仅新建房间消息)且游戏名已知时,追加一行
    `/规则 <游戏名>` 按钮。
    两个开关都关掉且无游戏名时返回空列表,调用方负责跳过 pending_buttons
    的写入。
    """
    rows: list[list[dict]] = []
    if include_join_leave:
        rows.append([
            {'text': '🟢 加入', 'data': '/加入', 'type': 2, 'style': 1},
            {'text': '🔴 退出', 'data': '/退出', 'type': 2, 'style': 3},
        ])
    if include_rule and game_name:
        rows.append([
            {'text': f'📖 《{game_name}》规则', 'data': f'/规则 {game_name}', 'type': 1, 'style': 4},
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
        {'text': '⚙️ 配置帮助', 'data': '帮助',  'type': 1, 'style': 4},
        {'text': '❓ 元指令帮助', 'data': '/帮助', 'type': 2, 'style': 1},
    ]]


def build_unknown_game_buttons() -> list[list[dict]]:
    """场景 3:游戏进行中,但用了未知的游戏指令 —— 游戏帮助 + 元指令帮助。"""
    return [[
        {'text': '🎮 游戏帮助', 'data': '帮助',  'type': 1, 'style': 4},
        {'text': '❓ 元指令帮助', 'data': '/帮助', 'type': 2, 'style': 1},
    ]]


def build_game_list_buttons() -> list[list[dict]]:
    """单按钮一行:「🎲 游戏列表」——与欢迎菜单同款。
    用于 `/新游戏 X` / `/规则 X` / `/设置 X` 等误输游戏名时,引导用户查正确名字。
    """
    return [[
        {'text': '🎲 游戏列表', 'data': '/游戏列表', 'type': 2, 'style': 4},
    ]]


def build_full_volume_apply_button() -> list[list[dict]]:
    """单按钮一行:「全量申请」(type=2,回填到输入框,用户自行补群号再发送)。

    挂在非全量群的「消息回复限制」教学提示底部,与文案里给出的命令格式
    ``全量申请 <本群群号>`` 对齐 —— 用户点完按钮后输入框出现「全量申请」,
    再手动补群号即可。``type=2`` 不带 ``enter``,符合本插件按钮约定。
    实际处理「全量申请」命令的是另一个插件,本插件只提供 UI 入口。
    """
    return [[
        {'text': '全量消息授权', 'data': '全量申请', 'type': 2, 'style': 4},
    ]]


def build_about_buttons() -> list[list[dict]]:
    """/关于 回执底部附:左 适配层仓库,右 LGT-Bot 上游仓库。两个都是链接按钮
    (type=0,QQ 协议下点击直接跳转,无 style)。
    """
    return [[
        {'text': '适配层 仓库',  'link': 'https://github.com/tiedanGH/LGTBot_ElainaBot'},
        {'text': 'LGT-Bot 仓库', 'link': 'https://github.com/Slontia/lgtbot'},
    ]]

# ──────── 欢迎菜单按钮组 ────────────────────────────────────────────────────
# 「游戏快捷开局」部分按 ``MENU_GAMES`` 渲染,这个列表由 ``data/config.yaml``
# 的 ``menu_game_buttons`` 字段在 @on_load 时下发 (见 config.py)。其他部分
# (帮助 / 游戏列表 / 创建房间 / 战绩 / 仓库链接) 是固定的。
#
# 之所以拆成函数而非常量,是为了让 config 改后 dispatcher 下次 reply 立刻拿到
# 新布局,不用重启进程;调用方一律走 ``build_menu_buttons()``。

DEFAULT_MENU_GAMES: list[str] = [
    '数字蜂巢', '天赋云巢', '炼金术士',
    '差值投标', '决胜五子', '彩虹奇兵',
]
# 由 config.py::_apply_runtime_tunables 覆盖;默认 6 个游戏 → 2 行 × 3 列。
MENU_GAMES: list[str] = list(DEFAULT_MENU_GAMES)
# 每行最多几个游戏按钮;QQ 客户端单行最多 5 个,3 排版上最舒服。
MENU_GAMES_PER_ROW: int = 3

_OFFICIAL_GROUP_LINK = (
    'https://qun.qq.com/universal-share/share?ac=1'
    '&authKey=GLoA6W7KujPW%2B%2B%2FeirVZVVEn61q%2FAmLFyd9mkJ8u%2Bv0E%2B2IooquHavHi9iaJSxKK'
    '&busi_data=eyJncm91cENvZGUiOiIxMDU5ODM0MDI0IiwidG9rZW4iOiJsTUFlUHZsdVJpSUhTc2dLSTBoeDI2M0IxS09kTGg3NzFsd1dvaVVLajVqTTIvRm9zaGlMTHBrekRIOGdVZHlaIiwidWluIjoiMjI5NTgyNDkyNyJ9'
    '&data=IMqVKIvDehyMv2ooaqlgzql0-Q9XENN4pK6qGR1mqYoZH5AFDBMmrflWNEFN-EOLeKuJTxLABAwgaaUnUp-iyw'
    '&svctype=4&tempid=h5_group_info'
)


def build_menu_buttons() -> list[list[dict]]:
    """组装欢迎菜单完整按钮组(每次调用都按当前 ``MENU_GAMES`` 重新渲染)。

    游戏快捷部分被切分为每行 ``MENU_GAMES_PER_ROW`` 个;``MENU_GAMES`` 为空
    列表时跳过整个游戏分区,菜单仍包含帮助/创建房间等固定按钮和仓库链接。
    """
    game_rows: list[list[dict]] = []
    for i in range(0, len(MENU_GAMES), MENU_GAMES_PER_ROW):
        chunk = MENU_GAMES[i:i + MENU_GAMES_PER_ROW]
        game_rows.append([
            {'text': name, 'data': f'/新游戏 {name}',
             'type': 2, 'style': 0}
            for name in chunk
        ])
    return [
        [
            {'text': '📖 查看帮助', 'data': '/帮助',    'type': 1, 'style': 4},
            {'text': '🎲 游戏列表', 'data': '/游戏列表', 'type': 1, 'style': 4},
        ],
        [
            {'text': '🎮 创建房间', 'data': '/新游戏',  'type': 2, 'style': 1},
            {'text': '📊 我的战绩', 'data': '/战绩',    'type': 2, 'style': 1},
        ],
        # 游戏快捷开局按钮(可配置 —— data/config.yaml 的 menu_game_buttons)
        *game_rows,
        # 链接按钮(type=0)
        [
            {'text': '仓库', 'link': 'https://github.com/tiedanGH/LGTBot_ElainaBot'},
            {'text': '网站', 'link': 'https://tiedan.site'},
            {'text': '官群', 'link': _OFFICIAL_GROUP_LINK},
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
