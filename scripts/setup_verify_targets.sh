#!/usr/bin/env bash
# 一次性装齐「多目标验证」所需的交叉编译器与 qemu-user —— 在验证机（Ubuntu）上执行。
#
# 为什么需要它：v1 只在验证机本机（x86-64）编译执行，结论只覆盖这一套 ABI。
# 星载软件的典型缺陷恰恰落在字长、字节序、对齐、整型宽度上，宿主机跑绿证明不了
# 目标机没问题。装上 qemu-user 与三套交叉编译器后，同一份源码就能在
# arm32（32 位小端）/ arm64（64 位小端）/ ppc32（32 位大端）上分别编译、执行、
# 采覆盖率——目标表见 verification/targets.py，由 .env 的 VERIFY_TARGETS 选用。
#
# 本脚本的三条硬要求：
#   1. 幂等：工具已装齐就完全不碰 apt，可以重复执行；
#   2. 不静默：装不上、跑不起来都非零退出并点名是哪个目标缺哪个工具；
#   3. 装完就验：对每个目标做一次「交叉编译 + qemu 执行 + 目标 gcov 采覆盖率」冒烟，
#      并核对目标事实（字长/字节序）与目标表一致——包装上了不等于链子能跑通，
#      gcov 与交叉 gcc 的 .gcda 格式不匹配这类问题只有在真跑一遍时才会暴露。
#
# 用法：
#   bash scripts/setup_verify_targets.sh                     # 装全部四个目标（交互输 sudo 密码）
#   WB_SUDO_PASSWORD=xxx bash scripts/setup_verify_targets.sh # 非交互（密码经 stdin 喂 sudo，不入库）
#   bash scripts/setup_verify_targets.sh --targets host,arm32 # 只装指定目标
#   bash scripts/setup_verify_targets.sh --check              # 只探测 + 冒烟，不安装
#
# 从 Windows 侧远程执行（密钥登录已配好，见 scripts/setup_verify_vm.ps1）：
#   ssh -i ~/.ssh/id_ed25519_workbuddy_verify lixf@<验证机IP> 'bash -s' -- < scripts/setup_verify_targets.sh
#   注：上面的重定向由本地 shell 完成，远端只收到脚本正文；需要密码时改用 --check 或先在 VM 控制台执行。
set -uo pipefail

PROG="setup_verify_targets"
ALL_TARGETS="host arm32 arm64 ppc32"
TARGETS=""
DO_INSTALL=1
ALLOW_EOL_MIRROR=1
QEMU_PKG="qemu-user-static"
# 冒烟用的编译选项与 verification/buildkit.py 保持一致（交叉目标另加 -static）
SMOKE_CFLAGS="-std=c99 -Wall -Wextra -g -O0 -fprofile-arcs -ftest-coverage"

usage() {
    cat <<EOF
$PROG —— 在验证机上装齐多目标验证工具链（幂等）

用法：bash $PROG.sh [选项]
  --targets <id,id,...>   只处理指定目标，可选：$ALL_TARGETS（默认全部）
  --check                 只探测工具链并跑冒烟，不安装任何软件包
  --no-eol-mirror         apt 失败时不改写 /etc/apt/sources.list（默认允许改写并备份）
  -h, --help              显示本帮助

环境变量：
  WB_SUDO_PASSWORD        非交互 sudo 密码，经 stdin 喂给 sudo -S（不出现在命令行里）。
                          不设置则交互式询问；已是 root 或已配置免密 sudo 时都不需要。

退出码：0=全部目标就绪且冒烟通过  1=有目标缺工具或冒烟失败  2=参数错误
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --targets)        TARGETS="${2:-}"; shift 2 || true ;;
        --targets=*)      TARGETS="${1#*=}"; shift ;;
        --check)          DO_INSTALL=0; shift ;;
        --no-eol-mirror)  ALLOW_EOL_MIRROR=0; shift ;;
        -h|--help)        usage; exit 0 ;;
        *) echo "$PROG: 未知参数 $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "$TARGETS" ]; then
    TARGETS="$ALL_TARGETS"
else
    TARGETS=$(printf '%s' "$TARGETS" | tr ',' ' ')
fi
for t in $TARGETS; do
    case " $ALL_TARGETS " in
        *" $t "*) ;;
        *) echo "$PROG: 未知目标 $t，可选：$ALL_TARGETS" >&2; exit 2 ;;
    esac
done

# ---------------- 目标表 ----------------
# 以下四张表必须与 verification/targets.py 的目标表一致；
# tests/test_targets.py 会逐目标交叉校验工具名、字长与字节序，漂移会让测试红。

tools_of() {          # 这个目标要用到的可执行文件（探测与冒烟都按它逐个检查）
    case "$1" in
        host)  echo "gcc gcov" ;;
        arm32) echo "arm-linux-gnueabihf-gcc arm-linux-gnueabihf-gcov qemu-arm-static" ;;
        arm64) echo "aarch64-linux-gnu-gcc aarch64-linux-gnu-gcov qemu-aarch64-static" ;;
        ppc32) echo "powerpc-linux-gnu-gcc powerpc-linux-gnu-gcov qemu-ppc-static" ;;
        *)     echo "" ;;
    esac
}

cc_of()   { case "$1" in
                host) echo "gcc";; arm32) echo "arm-linux-gnueabihf-gcc";;
                arm64) echo "aarch64-linux-gnu-gcc";; ppc32) echo "powerpc-linux-gnu-gcc";;
                *) echo "";;
            esac; }
gcov_of() { case "$1" in
                host) echo "gcov";; arm32) echo "arm-linux-gnueabihf-gcov";;
                arm64) echo "aarch64-linux-gnu-gcov";; ppc32) echo "powerpc-linux-gnu-gcov";;
                *) echo "";;
            esac; }
qemu_of() { case "$1" in
                host) echo "";; arm32) echo "qemu-arm-static";;
                arm64) echo "qemu-aarch64-static";; ppc32) echo "qemu-ppc-static";;
                *) echo "";;
            esac; }
pkgs_of() {           # Ubuntu（bionic 起）的包名；host 只需本机 gcc/gcov
    case "$1" in
        host)  echo "gcc" ;;
        arm32) echo "gcc-arm-linux-gnueabihf libc6-dev-armhf-cross" ;;
        arm64) echo "gcc-aarch64-linux-gnu libc6-dev-arm64-cross" ;;
        ppc32) echo "gcc-powerpc-linux-gnu libc6-dev-powerpc-cross" ;;
        *)     echo "" ;;
    esac
}

# WB_SMOKE_FACTS_BEGIN
facts_of() {          # 目标事实：字长（位）与字节序，冒烟时与实跑结果逐字核对
    case "$1" in
        host)  echo "64 little" ;;
        arm32) echo "32 little" ;;
        arm64) echo "64 little" ;;
        ppc32) echo "32 big" ;;
        *)     echo "" ;;
    esac
}
# WB_SMOKE_FACTS_END

# ---------------- sudo ----------------
SUDO_MODE="root"
if [ "$(id -u)" -ne 0 ]; then
    if [ -n "${WB_SUDO_PASSWORD:-}" ]; then
        SUDO_MODE="stdin"
    elif sudo -n true 2>/dev/null; then
        SUDO_MODE="nopass"
    else
        SUDO_MODE="ask"
    fi
fi

run_sudo() {
    # 密码只经 stdin 交给 sudo -S：写进命令行会被 ps 与 shell 历史记下来
    case "$SUDO_MODE" in
        root)   "$@" ;;
        stdin)  printf '%s\n' "$WB_SUDO_PASSWORD" | sudo -S -p "" "$@" ;;
        nopass) sudo -n "$@" ;;
        *)      sudo "$@" ;;
    esac
}

# 颜色只在交互终端上打开：本脚本常被 ssh 管道执行，转义码落进日志里只是噪声
if [ -t 1 ]; then
    C_HEAD=$'\033[36m'; C_OK=$'\033[32m'; C_BAD=$'\033[31m'; C_WARN=$'\033[33m'; C_OFF=$'\033[0m'
else
    C_HEAD=""; C_OK=""; C_BAD=""; C_WARN=""; C_OFF=""
fi

say()  { printf '%s\n' "$*"; }
head1(){ printf '\n%s== %s%s\n' "$C_HEAD" "$*" "$C_OFF"; }
okf()  { printf '  %sOK%s   %s\n' "$C_OK" "$C_OFF" "$*"; }
badf() { printf '  %sFAIL%s %s\n' "$C_BAD" "$C_OFF" "$*"; }
warnf(){ printf '  %sWARN%s %s\n' "$C_WARN" "$C_OFF" "$*"; }

# ---------------- 探测 ----------------
missing_tools() {     # 输出该目标缺失的可执行文件，全齐则输出空
    local miss="" c
    for c in $(tools_of "$1"); do
        command -v "$c" >/dev/null 2>&1 || miss="$miss $c"
    done
    printf '%s' "$miss" | sed 's/^ *//'
}

head1 "1/4 探测目标工具链"
say "验证机：$(uname -srm)　$(grep -h PRETTY_NAME /etc/os-release 2>/dev/null | cut -d'"' -f2)"
say "sudo 方式：$SUDO_MODE"
NEED_PKGS=""
NEED_QEMU=0
CROSS_MISSING=0
for t in $TARGETS; do
    miss=$(missing_tools "$t")
    if [ -z "$miss" ]; then
        okf "$t：$(cc_of "$t") $($(cc_of "$t") --version 2>/dev/null | head -1)"
    else
        badf "$t：缺 $miss"
        CROSS_MISSING=1
        if [ "$t" != "host" ]; then NEED_QEMU=1; fi
        for p in $(pkgs_of "$t"); do
            case " $NEED_PKGS " in *" $p "*) ;; *) NEED_PKGS="$NEED_PKGS $p";; esac
        done
    fi
done

if [ "$CROSS_MISSING" -eq 0 ]; then
    say "工具链已齐备，无需安装（幂等退出）。"
    NEED_PKGS=""
fi

# ---------------- apt ----------------
eol_rewrite() {
    # Ubuntu 18.04 已 EOL，archive/security.ubuntu.com 不再提供该版本索引，
    # apt-get update 必然 404。这里把源切到 old-releases.ubuntu.com，并先备份原文件。
    # 只在确认是 EOL 版本、且尚未改过的情况下动手——改源是有副作用的操作，不能反复做。
    local f=/etc/apt/sources.list codename bak
    [ -f "$f" ] || return 1
    grep -q "old-releases.ubuntu.com" "$f" 2>/dev/null && return 1
    codename=$(. /etc/os-release 2>/dev/null; printf '%s' "${UBUNTU_CODENAME:-}")
    case "$codename" in
        xenial|bionic) ;;
        *) warnf "当前版本 $codename 不在 EOL 名单内，不改写源"; return 1 ;;
    esac
    command -v sed >/dev/null 2>&1 || return 1
    bak="$f.wb-bak.$(date +%Y%m%d%H%M%S)"
    run_sudo cp -a "$f" "$bak" || return 1
    say "→ Ubuntu $codename 已 EOL，源切到 old-releases.ubuntu.com（原文件备份为 $bak）"
    run_sudo sed -i -E \
        "s#(https?://)(archive|security|ports)\.ubuntu\.com#\1old-releases.ubuntu.com#g" \
        "$f" || return 1
    return 0
}

apt_update() {
    say "→ apt-get update"
    if run_sudo apt-get update -qq >/dev/null 2>&1; then return 0; fi
    warnf "apt-get update 失败"
    if [ "$ALLOW_EOL_MIRROR" -eq 1 ] && eol_rewrite; then
        say "→ 重新 apt-get update"
        run_sudo apt-get update -qq >/dev/null 2>&1 && return 0
    fi
    say "！apt 索引更新失败。若验证机是 EOL 版本，可手动把 /etc/apt/sources.list 指向" >&2
    say "  old-releases.ubuntu.com 后重跑本脚本，或改用离线 deb 包安装：" >&2
    say "  $NEED_PKGS $([ "$NEED_QEMU" -eq 1 ] && echo "$QEMU_PKG")" >&2
    return 1
}

head1 "2/4 安装缺失的软件包"
INSTALL_OK=1
if [ -z "$NEED_PKGS" ]; then
    say "无需安装。"
elif [ "$DO_INSTALL" -eq 0 ]; then
    warnf "--check 模式不安装。缺失的包：$NEED_PKGS $([ "$NEED_QEMU" -eq 1 ] && echo "$QEMU_PKG")"
    INSTALL_OK=0
else
    PKG_LIST="$NEED_PKGS"
    [ "$NEED_QEMU" -eq 1 ] && PKG_LIST="$PKG_LIST $QEMU_PKG"
    say "→ 待安装：$PKG_LIST"
    if apt_update; then
        # --no-install-recommends：不拉 binfmt-support 等推荐包。本系统显式用
        # qemu-<arch>-static 执行二进制，不依赖 binfmt 注册，少装一层系统级副作用。
        if run_sudo apt-get install -y --no-install-recommends $PKG_LIST; then
            okf "安装完成"
        else
            badf "apt-get install 失败"
            INSTALL_OK=0
        fi
    else
        INSTALL_OK=0
    fi
fi

# ---------------- 冒烟：交叉编译 + qemu 执行 + 目标 gcov ----------------
head1 "3/4 逐目标冒烟（编译 → 执行 → 采覆盖率 → 核对目标事实）"
SMOKE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/wb_targets_smoke.XXXXXX") || {
    badf "无法创建临时目录"; exit 1; }
trap 'rm -rf "$SMOKE_DIR"' EXIT INT TERM
mkdir -p "$SMOKE_DIR/build"
cat > "$SMOKE_DIR/wb_smoke.c" <<'EOF'
/* 冒烟程序：打印本目标的字长与字节序，并留一个真分支供 gcov 统计。 */
#include <stdio.h>

static int endian_is_little(void)
{
    unsigned int probe = 1u;
    return *((unsigned char *)&probe) == 1u;
}

int main(void)
{
    int little = endian_is_little();
    printf("bits=%d endian=%s\n", (int)(sizeof(void *) * 8),
           little ? "little" : "big");
    if (little) {
        printf("branch=little\n");
    } else {
        printf("branch=big\n");
    }
    return 0;
}
EOF

SMOKE_FAIL=0
for t in $TARGETS; do
    cc=$(cc_of "$t"); gcov=$(gcov_of "$t"); qemu=$(qemu_of "$t")
    miss=$(missing_tools "$t")
    if [ -n "$miss" ]; then
        badf "$t：工具链缺失（$miss），跳过冒烟"
        SMOKE_FAIL=1
        continue
    fi
    # 交叉目标一律 -static：动态执行要 qemu 找得到目标架构的动态链接器与库搜索路径，
    # 静态链接把这类环境依赖一次消掉，证据也就与验证机的库版本无关（同 buildkit）。
    extra=""; [ -n "$qemu" ] && extra="-static"
    rm -f "$SMOKE_DIR"/build/* 2>/dev/null
    log="$SMOKE_DIR/build/$t.log"
    {
        echo "# cc: $cc $SMOKE_CFLAGS $extra"
        ( cd "$SMOKE_DIR" && $cc $SMOKE_CFLAGS $extra -c wb_smoke.c -o build/wb_smoke.o ) 2>&1 \
            && ( cd "$SMOKE_DIR" && $cc $SMOKE_CFLAGS $extra build/wb_smoke.o -o "build/smoke_$t" ) 2>&1 \
            || { echo "WB_SMOKE compile_fail"; }
    } > "$log" 2>&1
    if grep -q "WB_SMOKE compile_fail" "$log"; then
        badf "$t：编译或链接失败，详见 $log"
        sed 's/^/        /' "$log" | head -8
        SMOKE_FAIL=1
        continue
    fi
    # 交叉目标用 qemu-<arch>-static 直接执行；host 就是本机执行。
    # 注意 $qemu 必须是独立的命令词：写成 "$qemu " 会让命令名带上尾随空格而找不到。
    if [ -n "$qemu" ]; then
        out=$( cd "$SMOKE_DIR" && "$qemu" "./build/smoke_$t" 2>&1 )
    else
        out=$( cd "$SMOKE_DIR" && "./build/smoke_$t" 2>&1 )
    fi
    got_bits=$(printf '%s\n' "$out" | sed -n 's/^bits=\([0-9]*\) endian=\(.*\)$/\1/p')
    got_endian=$(printf '%s\n' "$out" | sed -n 's/^bits=\([0-9]*\) endian=\(.*\)$/\2/p')
    want=$(facts_of "$t"); want_bits=${want%% *}; want_endian=${want##* }
    if [ "$got_bits" != "$want_bits" ] || [ "$got_endian" != "$want_endian" ]; then
        badf "$t：实跑事实 bits=$got_bits endian=$got_endian，与目标表 $want_bits/$want_endian 不符"
        SMOKE_FAIL=1
        continue
    fi
    # gcov 必须能解析交叉编译产物的 .gcda：格式不匹配时这里是唯一会暴露的地方
    cov=$( cd "$SMOKE_DIR" && $gcov -b -c -f build/wb_smoke.gcda 2>&1 )
    if ! printf '%s' "$cov" | grep -q "Function 'main'" \
       || ! printf '%s' "$cov" | grep -q "Taken at least once"; then
        badf "$t：$gcov 未能解析 .gcda（覆盖率采集不可用）"
        printf '%s\n' "$cov" | head -6 | sed 's/^/        /'
        SMOKE_FAIL=1
        continue
    fi
    br=$(printf '%s\n' "$cov" | sed -n "s/^Taken at least once:\([0-9.]*\)% of \([0-9]*\).*/\1% of \2/p" | head -1)
    okf "$t：$cc → ${qemu:-本机执行}　bits=$got_bits endian=$got_endian　gcov 分支 $br"
done

# ---------------- 结论 ----------------
head1 "4/4 结论"
if [ "$INSTALL_OK" -eq 1 ] && [ "$SMOKE_FAIL" -eq 0 ]; then
    okf "全部目标就绪：$TARGETS"
    say ""
    say "把下面这行写进 .env 即可开启多目标验证（不写则默认只测 host）："
    say "  VERIFY_TARGETS=$(printf '%s' "$TARGETS" | tr ' ' ',')"
    exit 0
fi
badf "有目标未就绪（安装成功=$INSTALL_OK，冒烟通过=$([ "$SMOKE_FAIL" -eq 0 ] && echo 1 || echo 0)）"
say "  修复后重跑本脚本即可；脚本幂等，已装好的部分不会重复安装。" >&2
say "  注意：缺工具的目标在流水线里记为「未执行」，整轮验证转人工裁决，" >&2
say "        不会被当成「该目标通过」——所以别在 .env 里配上没装好的目标。" >&2
exit 1
