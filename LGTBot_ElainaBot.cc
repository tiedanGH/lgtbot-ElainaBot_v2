/*
 * LGTBot_ElainaBot.cc — LGTBot × ElainaBot (QQ Official Bot) 桥接层
 *
 * 将 LGTBot C++ 引擎通过 Boost.Python 暴露给 Python，
 * Python 侧由 ElainaBot 插件系统提供消息收发能力。
 *
 * 与 lgtbot_kook.cc 的主要差异：
 *   1. Boost.Python 模块名改为 LGTBot_ElainaBot
 *   2. 用户 Mention 格式：Kook (met)uid(met) → QQ Markdown <@uid>
 */

// 抑制 Boost 自身链路里残留的 deprecation `#pragma message`：
//   · BOOST_BIND_GLOBAL_PLACEHOLDERS —— 显式接受 _1 / _2 仍在全局命名空间
//   · BOOST_ALLOW_DEPRECATED_HEADERS —— 接受 boost.python 仍然间接引用
//                                       已被 Boost 标记为 deprecated 的内部头
// 两个都是 Boost 官方给消费者的 opt-in 开关，只关警告，不改行为。
#define BOOST_BIND_GLOBAL_PLACEHOLDERS
#define BOOST_ALLOW_DEPRECATED_HEADERS

#include <boost/python.hpp>
#include <boost/python/call.hpp>

#include "bot_core/bot_core.h"

#include <memory>
#include <thread>
#include <iostream>
#include <curl/curl.h>

// ──── 全局 Python 回调句柄 ────────────────────────────────────────────────
void* g_bot_core              = nullptr;
PyObject* g_get_user_name     = nullptr;
PyObject* g_get_user_avatar_url = nullptr;
PyObject* g_send_text_message = nullptr;
PyObject* g_send_image_message = nullptr;
PyObject* g_match_event       = nullptr;

// ──── GIL 辅助 RAII ──────────────────────────────────────────────────────
class AcquireGIL {
public:
    inline AcquireGIL()  { state = PyGILState_Ensure(); }
    inline ~AcquireGIL() { PyGILState_Release(state);   }
private:
    PyGILState_STATE state;
};

class ReleaseGIL {
public:
    inline ReleaseGIL()  { save_state = PyEval_SaveThread();  }
    inline ~ReleaseGIL() { PyEval_RestoreThread(save_state);  }
private:
    PyThreadState* save_state;
};

// ──── 回调实现 ────────────────────────────────────────────────────────────

/**
 * ClassifyMatchEvent — 从单次 Boardcast 合并后的 content 推断本条消息所对应
 * 的房间事件类型,Python 据此挂相应的按钮组(/规则、加入/退出、游戏列表/创建房间)。
 *
 * 与上游 lgtbot UI 字符串耦合(本插件不改 lgtbot 子模块,只依赖契约级稳定文本):
 *   "现在玩家可以..."       NewMatch 房间建立    -> "new_game"
 *   "加入了游戏" + brief      Match::Request          -> "join_leave"
 *   "退出了游戏" + brief      Match::Leave 等待中     -> "join_leave" 或 nullptr (最后一人,等下条 all_left)
 *   "所有玩家都退出了游戏"   全员退出,房间解散       -> "all_left"
 *   "所有玩家都强制退出..."  全员强制退出,房间解散   -> "all_left"
 *   "游戏已解散"             Terminate(主动/新建前置) -> "terminate" (清状态,不挂按钮)
 *   "游戏开始，您可以使用"  Match::GameStart 成功后  -> "game_started" (触发「刷新按钮使用说明」教学)
 *   "未预料的游戏设置"        房主输错游戏配置        -> "unknown_config" (配置帮助 + 元指令帮助)
 *   "未预料的游戏指令"        游戏中玩家输错游戏指令  -> "unknown_game"   (游戏帮助 + 元指令帮助)
 *   "未预料的元指令"          / 开头的未知元指令       -> "unknown_meta"   (仅元指令帮助)
 *   "若您想执行元指令" 兜底  未参与/未在本群参与游戏 -> "unknown_meta"   (仅元指令帮助)
 *   "未知的游戏名"            /新游戏 /规则 /设置 等误输游戏名 -> "unknown_game_name" (附「🎲 游戏列表」按钮)
 *   "LGTBot v" (前缀)         /关于 回执              -> "about"         (附两个仓库链接按钮)
 *   仅含 "游戏名称：X" 的其他 brief(/设置 成功等)    -> "announce" (只更新当前游戏名)
 *   其余                                                -> nullptr (不调回调)
 *
 * out_game_name 同步取出 brief 顶上的「游戏名称：X」用作 /规则 按钮的参数。
 */
static const char* ClassifyMatchEvent(const std::string& content, std::string& out_game_name)
{
    static const std::string kAllLeft1 = "所有玩家都退出了游戏";
    static const std::string kAllLeft2 = "所有玩家都强制退出了游戏";
    static const std::string kTerminate = "游戏已解散";
    static const std::string kNewMatch = "现在玩家可以";
    static const std::string kJoined = "加入了游戏";
    static const std::string kLeft = "退出了游戏";
    static const std::string kGameNameMarker = "游戏名称：";
    static const std::string kZeroUsers = "当前用户数：0";
    // 未知指令引导(bot_core.cc HandleRequest / HandleMetaRequest / match.cc
    // Request 四处错误回执):
    //   "未预料的游戏设置"  房主在等待房间里输错配置
    //   "未预料的游戏指令"  玩家在游戏中输错游戏指令
    //   "未预料的元指令"    输了一条 / 开头的未知元指令(HandleMetaRequest)
    //   "若您想执行元指令"  上面前三种之外、bot_core 层的「未参与游戏 / 未在
    //                       本群参与游戏」两类错误带的兜底尾句
    static const std::string kUnknownConfig   = "未预料的游戏设置";
    static const std::string kUnknownGame     = "未预料的游戏指令";
    static const std::string kUnknownMetaCmd  = "未预料的元指令";
    static const std::string kUnknownMeta     = "若您想执行元指令";
    // 引擎里至少 9 处错误回执形如「[错误] 创建/查看/设置失败：未知的游戏名,
    // 请通过「/游戏列表」查看游戏名称」(message_handlers.cc 的 new_game /
    // show_rule / show_options / 等)。共用此 marker 同样附「🎲 游戏列表」按钮。
    static const std::string kUnknownGameName = "未知的游戏名";
    // /关于 命令的回执(message_handlers.cc::about) —— 其首句拼 "LGTBot " + LGTBot_Version()。版本号来自 `git describe --tags --always`
    // tagged 构建形如 v1.0.0-N-gXXXX,合并后含 "LGTBot v" 子串。lgtbot 整个代码库其他用户输出路径都不会出现此前缀,做识别串足够稳定。
    // (注:CMake 找不到 git tag 时会回退 <unpublished version>,无 v 前缀; 这是开发未提交场景,生产部署不会遇到。)
    static const std::string kAbout = "LGTBot v";
    // Match::GameStart 成功后引擎 BoardcastAtAll 这条欢迎语(match.cc 唯一出处)。
    // 用「游戏开始，您可以使用」这段较长的前缀做识别,既避开游戏内文本里偶然
    // 出现「游戏开始」二字的可能,也避开 announce / new_game 类 brief 被误判。
    static const std::string kGameStarted = "游戏开始，您可以使用";

    out_game_name.clear();

    // 1. 全员退出导致房间解散 —— 优先级高于「退出了游戏」单条匹配
    if (content.find(kAllLeft1) != std::string::npos ||
        content.find(kAllLeft2) != std::string::npos) {
        return "all_left";
    }

    // 2. 主动/新建前置的 Terminate
    if (content.find(kTerminate) != std::string::npos) {
        return "terminate";
    }

    // 3. 未知指令分类 —— 顺序敏感:特化的 unknown_config / unknown_game 都
    // 在尾部带了 unknown_meta 的兜底句,所以必须先匹配前两者再兜底
    if (content.find(kUnknownConfig) != std::string::npos) {
        return "unknown_config";
    }
    if (content.find(kUnknownGame) != std::string::npos) {
        return "unknown_game";
    }
    if (content.find(kUnknownMetaCmd) != std::string::npos ||
        content.find(kUnknownMeta)    != std::string::npos) {
        return "unknown_meta";
    }
    if (content.find(kUnknownGameName) != std::string::npos) {
        return "unknown_game_name";
    }

    // 4. /关于 回执 —— 附两个仓库链接按钮
    if (content.find(kAbout) != std::string::npos) {
        return "about";
    }

    // 5. 游戏真的开始 —— 早于 brief 检查,因为 GameStart 这条广播没有 brief
    if (content.find(kGameStarted) != std::string::npos) {
        return "game_started";
    }

    // 6. 以下事件都需要 brief 存在,顺带把游戏名拿出来
    const size_t name_pos = content.find(kGameNameMarker);
    if (name_pos == std::string::npos) {
        return nullptr;
    }
    const size_t name_start = name_pos + kGameNameMarker.size();
    const size_t name_end = content.find('\n', name_start);
    out_game_name = (name_end == std::string::npos)
        ? content.substr(name_start)
        : content.substr(name_start, name_end - name_start);
    if (out_game_name.empty()) {
        return nullptr;
    }

    if (content.starts_with(kNewMatch)) {
        return "new_game";
    }
    if (content.find(kJoined) != std::string::npos) {
        return "join_leave";
    }
    if (content.find(kLeft) != std::string::npos) {
        // "退出了游戏" 含义可能是「中途强制退出」(无 brief,已被 name_pos 拦截)
        // 或「等待中退出」(有 brief)。后者若是最后一人,下一条消息会带 all_left
        // 按钮,本条不再附,避免重复 / 玩家误点解散后的「加入」。
        const size_t zpos = content.find(kZeroUsers);
        if (zpos != std::string::npos) {
            const size_t after = zpos + kZeroUsers.size();
            if (after == content.size() || content[after] == '\n') {
                return nullptr;
            }
        }
        return "join_leave";
    }

    // brief 但非以上事件(如 /设置 成功),只用来刷新 Python 侧记下的游戏名
    return "announce";
}

/**
 * HandleMessages — 将引擎消息列表合并为最少的对外发送
 *
 * 合并策略（避免 "@xxx 文本" 和 "图片" 拆成两条）：
 *   1. 单次 Flush 内的所有 TEXT/MENTION 段落累积为一段 content
 *   2. 所有 IMAGE 段落收集到列表
 *   3. 无图片：发一条文本
 *      有图片：第 1 张图片带 content 一起发，其余图片单独发（QQ 媒体消息每条
 *               仅能附带一个媒体，多图情况无法压缩为单条，但 @文本 不会再
 *               重复出现，符合"@+图片同一条"的最常见用例）
 *
 * QQ Markdown mention 格式：<@openid>
 */
void HandleMessages(void* handler, const char* const id, const int is_uid,
                    const LGTBot_Message* messages, const size_t size)
{
    std::string content;
    std::vector<std::string> images;
    images.reserve(4);

    for (size_t i = 0; i < size; ++i) {
        const auto& msg = messages[i];
        switch (msg.type_) {
        case LGTBOT_MSG_TEXT:
            content.append(msg.str_);
            break;
        case LGTBOT_MSG_USER_MENTION:
            content.append("<@");
            content.append(msg.str_);
            content.append(">");
            break;
        case LGTBOT_MSG_IMAGE:
            images.emplace_back(msg.str_);
            break;
        default:
            assert(false);
        }
    }

    try {
        AcquireGIL a;

        // 分类本条消息属于哪种房间事件,推断要附什么按钮 / 是否要清当前游戏名。
        // ClassifyMatchEvent 返回 nullptr 时不调 Python(无需操作)。
        if (g_match_event != nullptr) {
            std::string game_name;
            const char* kind = ClassifyMatchEvent(content, game_name);
            if (kind != nullptr) {
                try {
                    boost::python::call<void>(g_match_event, id, is_uid, kind, game_name);
                } catch (...) {
                    std::cerr << "[LGTBot_ElainaBot] match_event dispatch failed" << std::endl;
                }
            }
        }

        if (images.empty()) {
            if (!content.empty()) {
                boost::python::call<void>(g_send_text_message, id, is_uid, content);
            }
        } else {
            // 第 1 张图片附带 content（合并成一条消息），其余图片仅图片
            for (size_t i = 0; i < images.size(); ++i) {
                const std::string& cap = (i == 0) ? content : std::string();
                boost::python::call<void>(g_send_image_message, id, is_uid, images[i], cap);
            }
        }
    } catch (...) {
        std::cerr << "[LGTBot_ElainaBot] HandleMessages dispatch failed" << std::endl;
    }
}

/**
 * GetUserName — 获取用户显示名
 * 格式：<昵称(前4…后4)>，省略号中间隐藏 uid 主体（QQ openid 太长不适合 UI 直接展示）。
 * uid 长度 ≤ 8 时不截断，原样输出。
 * Python 侧 cb_get_user_name 缓存未命中时返回 uid 作为名字，此时退化为 <截短uid(截短uid)>。
 * Python 抛异常时 fallback 到 <uid>（仅 uid，不截短不包昵称壳，便于排错）。
 */
void GetUserName(void* handler, char* const buffer, const size_t size, const char* const uid)
{
    try {
        AcquireGIL a;
        const std::string name = boost::python::call<std::string>(g_get_user_name, uid);
        // 截短 uid：长度 > 8 时显示「前4字节 + UTF-8 省略号 + 后4字节」
        std::string short_uid;
        const size_t uid_len = std::strlen(uid);
        if (uid_len > 8) {
            short_uid.reserve(4 + 3 /* "…" UTF-8 */ + 4);
            short_uid.append(uid, 4);
            short_uid.append("\xe2\x80\xa6");  // U+2026 HORIZONTAL ELLIPSIS
            short_uid.append(uid + uid_len - 4, 4);
        } else {
            short_uid.assign(uid, uid_len);
        }
        snprintf(buffer, size, "<%s(%s)>", name.c_str(), short_uid.c_str());
    } catch (...) {
        std::cerr << "[LGTBot_ElainaBot] GetUserName failed: " << uid << std::endl;
        snprintf(buffer, size, "<%s>", uid);
    }
}

/**
 * GetUserNameInGroup — 获取群内用户显示名
 * QQ 群昵称需额外 API，此处直接委托 GetUserName（Python 侧可按需扩展缓存以
 * 区分群昵称 / 全局昵称）
 */
void GetUserNameInGroup(void* handler, char* const buffer, const size_t size,
                        const char* group_id, const char* const user_id)
{
    return GetUserName(handler, buffer, size, user_id);
}

/**
 * DownloadUserAvatar — 通过 libcurl 将头像下载到本地文件
 * Python 侧 get_user_avatar_url 返回空字符串时跳过下载
 */
int DownloadUserAvatar(void* handler, const char* const uid, const char* const dest_filename)
{
    std::string url;
    try {
        AcquireGIL a;
        url = boost::python::call<std::string>(g_get_user_avatar_url, uid);
    } catch (...) {
        std::cerr << "[LGTBot_ElainaBot] DownloadUserAvatar get_url failed, uid=" << uid << std::endl;
        return false;
    }
    if (url.empty()) {
        // Python 侧暂无头像 URL（常见于首次运行）—— 静默跳过
        return false;
    }

    CURL* const curl = curl_easy_init();
    if (!curl) {
        std::cerr << "[LGTBot_ElainaBot] curl_easy_init() failed" << std::endl;
        return false;
    }
    FILE* const fp = fopen(dest_filename, "wb");
    if (!fp) {
        curl_easy_cleanup(curl);
        return false;
    }
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, fwrite);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, fp);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 10L);
    const CURLcode res = curl_easy_perform(curl);
    if (res != CURLE_OK) {
        std::cerr << "[LGTBot_ElainaBot] avatar download failed: " << curl_easy_strerror(res) << std::endl;
    }
    curl_easy_cleanup(curl);
    fclose(fp);
    return res == CURLE_OK;
}

// ──── 对外接口 ────────────────────────────────────────────────────────────

/**
 * Start — 初始化 LGTBot 引擎，注入所有回调
 *
 * 参数与 lgtbot_kook 完全相同，便于 Python 侧无缝替换。
 */
bool Start(
        const char* const game_path,
        const char* const db_path,
        const char* const conf_path,
        const char* const image_path,
        const char* const admins,
        PyObject* get_user_name,
        PyObject* get_user_avatar_url,
        PyObject* send_text_message,
        PyObject* send_image_message,
        PyObject* match_event)
{
    ReleaseGIL r;
    const LGTBot_Option option {
        .game_path_  = game_path,
        .db_path_    = db_path,
        .conf_path_  = std::strlen(conf_path) == 0 ? nullptr : conf_path,
        .image_path_ = image_path,
        .admins_     = admins,
        .callbacks_  = LGTBot_Callback{
            .get_user_name         = GetUserName,
            .get_user_name_in_group = GetUserNameInGroup,
            .download_user_avatar  = DownloadUserAvatar,
            .handle_messages       = HandleMessages,
        },
    };
    g_get_user_name      = get_user_name;
    g_get_user_avatar_url = get_user_avatar_url;
    g_send_text_message  = send_text_message;
    g_send_image_message = send_image_message;
    g_match_event        = match_event;

    const char* errmsg = nullptr;
    g_bot_core = LGTBot_Create(&option, &errmsg);
    if (!g_bot_core) {
        std::cerr << "[LGTBot_ElainaBot] Init failed: " << (errmsg ? errmsg : "unknown") << std::endl;
        return false;
    }
    return true;
}

void OnPrivateMessage(const char* msg, const std::string& uid)
{
    ReleaseGIL r;
    LGTBot_HandlePrivateRequest(g_bot_core, uid.c_str(), msg);
}

void OnPublicMessage(const char* msg, const std::string& uid, const std::string& gid)
{
    ReleaseGIL r;
    LGTBot_HandlePublicRequest(g_bot_core, gid.c_str(), uid.c_str(), msg);
}

bool ReleaseBotIfNoProcessingGames()
{
    ReleaseGIL r;
    return LGTBot_ReleaseIfNoProcessingGames(g_bot_core);
}

// ──── Boost.Python 模块注册 ───────────────────────────────────────────────
BOOST_PYTHON_MODULE(LGTBot_ElainaBot)
{
    namespace python = boost::python;
    python::def("start",                          Start);
    python::def("on_private_message",             OnPrivateMessage);
    python::def("on_public_message",              OnPublicMessage);
    python::def("release_bot_if_not_processing_games", ReleaseBotIfNoProcessingGames);
}
