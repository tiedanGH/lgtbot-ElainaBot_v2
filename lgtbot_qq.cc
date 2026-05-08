/*
 * lgtbot_qq.cc — LGTBot × ElainaBot (QQ Official Bot) 桥接层
 *
 * 将 LGTBot C++ 引擎通过 Boost.Python 暴露给 Python，
 * Python 侧由 ElainaBot 插件系统提供消息收发能力。
 *
 * 与 lgtbot_kook.cc 的主要差异：
 *   1. Boost.Python 模块名改为 lgtbot_qq
 *   2. 用户 Mention 格式：Kook (met)uid(met) → QQ Markdown <@uid>
 */

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
        std::cerr << "[lgtbot_qq] HandleMessages dispatch failed" << std::endl;
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
        std::cerr << "[lgtbot_qq] GetUserName failed: " << uid << std::endl;
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
        std::cerr << "[lgtbot_qq] DownloadUserAvatar get_url failed, uid=" << uid << std::endl;
        return false;
    }
    if (url.empty()) {
        // Python 侧暂无头像 URL（常见于首次运行）—— 静默跳过
        return false;
    }

    CURL* const curl = curl_easy_init();
    if (!curl) {
        std::cerr << "[lgtbot_qq] curl_easy_init() failed" << std::endl;
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
        std::cerr << "[lgtbot_qq] avatar download failed: " << curl_easy_strerror(res) << std::endl;
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
        PyObject* send_image_message)
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

    const char* errmsg = nullptr;
    g_bot_core = LGTBot_Create(&option, &errmsg);
    if (!g_bot_core) {
        std::cerr << "[lgtbot_qq] Init failed: " << (errmsg ? errmsg : "unknown") << std::endl;
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
BOOST_PYTHON_MODULE(lgtbot_qq)
{
    namespace python = boost::python;
    python::def("start",                          Start);
    python::def("on_private_message",             OnPrivateMessage);
    python::def("on_public_message",              OnPublicMessage);
    python::def("release_bot_if_not_processing_games", ReleaseBotIfNoProcessingGames);
}
