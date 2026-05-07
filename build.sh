#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# LGTBot × ElainaBot 一键编译脚本 (Linux)
#
# 用法：
#   cd plugins/lgtbot_qq
#   bash build.sh                 # 标准编译（无测试）
#   bash build.sh --test          # 带 LGTBot 测试模式编译 (-DWITH_TEST=ON)
#   bash build.sh --clean         # 清理后重编译
#   bash build.sh --clean --test  # 清理 + 测试模式
#   bash build.sh -j 8            # 指定并行 jobs
#   bash build.sh --debug         # Debug 构建 (默认 Release)
#   bash build.sh --asan          # 启用 AddressSanitizer (-DWITH_ASAN=ON)
#   bash build.sh --no-glog       # 关闭 glog 日志 (默认 ON)
#   bash build.sh --no-games      # 不编译内置游戏插件 (默认 ON)
#
# 产物：plugins/lgtbot_qq/lgtbot_qq.so （ElainaBot 主框架启动时自动加载）
# -----------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── 默认 LGTBot 编译选项 ────────────────────────────────────────────────
JOBS="$(nproc 2>/dev/null || echo 4)"
CLEAN=0
BUILD_TYPE="Release"
WITH_GCOV="OFF"
WITH_ASAN="OFF"
WITH_GLOG="ON"
WITH_SQLITE="ON"
WITH_TEST="OFF"      # ← 默认关闭，用 --test 开启
WITH_GAMES="ON"

# ── 参数解析 ─────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --clean)      CLEAN=1; shift ;;
        --test)       WITH_TEST="ON"; shift ;;
        --no-test)    WITH_TEST="OFF"; shift ;;
        --debug)      BUILD_TYPE="Debug"; shift ;;
        --release)    BUILD_TYPE="Release"; shift ;;
        --asan)       WITH_ASAN="ON"; shift ;;
        --gcov)       WITH_GCOV="ON"; shift ;;
        --no-glog)    WITH_GLOG="OFF"; shift ;;
        --no-sqlite)  WITH_SQLITE="OFF"; shift ;;
        --no-games)   WITH_GAMES="OFF"; shift ;;
        -j)           JOBS="$2"; shift 2 ;;
        -j*)          JOBS="${1#-j}"; shift ;;
        -h|--help)
            sed -n '2,20p' "$0"; exit 0 ;;
        *)            echo "未知参数: $1 (使用 --help 查看)"; exit 1 ;;
    esac
done

# ── 子模块检查 ───────────────────────────────────────────────────────────
if [[ ! -f "lgtbot/CMakeLists.txt" ]]; then
    echo "[!] lgtbot/ 子模块为空，尝试初始化..."
    if [[ -d "../../.git" ]] || [[ -d ".git" ]]; then
        git submodule update --init --recursive lgtbot || true
    fi
    if [[ ! -f "lgtbot/CMakeLists.txt" ]]; then
        echo "[!] 请手动准备 lgtbot/ 源码 (https://github.com/slontia/lgtbot)"
        exit 1
    fi
fi

# ── 依赖自检 ─────────────────────────────────────────────────────────────
echo "── 依赖检查 ─────────────"
need=()
command -v cmake >/dev/null 2>&1 || need+=("cmake")
command -v g++   >/dev/null 2>&1 || command -v clang++ >/dev/null 2>&1 || need+=("g++ 或 clang++")
[[ -f /usr/include/curl/curl.h ]] || pkg-config --exists libcurl 2>/dev/null || need+=("libcurl-dev")

# Python3 dev headers
PY=$(command -v python3 || true)
if [[ -z "$PY" ]]; then
    need+=("python3")
else
    PY_INC=$($PY -c "import sysconfig; print(sysconfig.get_path('include'))")
    [[ -f "$PY_INC/Python.h" ]] || need+=("python3-dev (找不到 Python.h)")
fi

# Boost.Python（任意命名）
if ! ldconfig -p 2>/dev/null | grep -qE 'libboost_python(3[0-9]*)?\.so'; then
    need+=("libboost-python3-dev")
fi

if [[ ${#need[@]} -gt 0 ]]; then
    echo "[!] 缺少依赖："
    for d in "${need[@]}"; do echo "    - $d"; done
    echo
    echo "Ubuntu/Debian 一键安装："
    echo "  sudo apt update && sudo apt install -y \\"
    echo "    build-essential cmake libcurl4-openssl-dev \\"
    echo "    python3-dev libboost-python-dev libboost-system-dev \\"
    echo "    libgflags-dev libgoogle-glog-dev libsqlite3-dev"
    exit 1
fi
echo "✅ 依赖齐全"

# ── 清理 ─────────────────────────────────────────────────────────────────
if [[ $CLEAN -eq 1 ]]; then
    echo "── 清理 build/ ────────"
    rm -rf build
fi

# ── 配置 + 编译 ──────────────────────────────────────────────────────────
echo "── 编译选项 ───────────"
cat <<EOF
  Build Type   : $BUILD_TYPE
  WITH_TEST    : $WITH_TEST   $([ "$WITH_TEST" = "ON" ] && echo '← 测试模式已启用')
  WITH_GAMES   : $WITH_GAMES
  WITH_GLOG    : $WITH_GLOG
  WITH_SQLITE  : $WITH_SQLITE
  WITH_ASAN    : $WITH_ASAN
  WITH_GCOV    : $WITH_GCOV
  Parallel     : $JOBS
EOF

echo "── CMake 配置 ─────────"
cmake -S . -B build \
    -DCMAKE_BUILD_TYPE="$BUILD_TYPE" \
    -DWITH_GCOV="$WITH_GCOV" \
    -DWITH_ASAN="$WITH_ASAN" \
    -DWITH_GLOG="$WITH_GLOG" \
    -DWITH_SQLITE="$WITH_SQLITE" \
    -DWITH_TEST="$WITH_TEST" \
    -DWITH_GAMES="$WITH_GAMES"

echo "── 编译 (-j $JOBS) ────"
cmake --build build -j "$JOBS"

# ── 验证产物 ─────────────────────────────────────────────────────────────
SO_PATH="$SCRIPT_DIR/lgtbot_qq.so"
if [[ ! -f "$SO_PATH" ]]; then
    # 部分 CMake 版本不遵守 LIBRARY_OUTPUT_DIRECTORY，到 build/ 找
    FOUND=$(find build -name 'lgtbot_qq.so' -print -quit || true)
    if [[ -n "$FOUND" ]]; then
        cp "$FOUND" "$SO_PATH"
    fi
fi

if [[ ! -f "$SO_PATH" ]]; then
    echo "[!] 编译完成但未找到 lgtbot_qq.so"
    exit 1
fi

echo
echo "════════════════════════════════════════════════════════════════"
echo " ✅ 编译成功"
echo "    $SO_PATH"
echo "════════════════════════════════════════════════════════════════"
