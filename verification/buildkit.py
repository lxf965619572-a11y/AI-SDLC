"""构建脚本与测试桩模板生成。

第一性原理：执行环境要可复现，就不能让「怎么编译、怎么跑、怎么采覆盖率」散落在
AI 生成的代码里。这三件事由本模块生成固定模板，AI 只负责填 src/ 与 tests/ 的内容。
于是同一份输入在验证机上必然走同一条命令序列，证据也就可比对。

目录约定（代码生成与测试实现两个智能体的提示词都按这个写）：
    include/*.h    被测模块头文件
    src/*.c        被测模块实现（唯一参与覆盖率判定的部分）
    tests/*.c      可执行测试（含 main），由 test_impl 阶段产出
    build.sh       本模块生成
"""
from __future__ import annotations

# 编译选项固定：C99 + 全告警 + 不优化（优化会让 gcov 的行/分支归属失真）
CFLAGS = "-std=c99 -Wall -Wextra -g -O0 -fprofile-arcs -ftest-coverage"
INCLUDES = "-Iinclude -Isrc -Itests"

# 退出码约定：0 = 工具链跑完（用例是否通过看解析结果）
#             2 = 编译或链接失败  3 = 目录里没有源文件
EXIT_OK, EXIT_BUILD_FAIL, EXIT_NO_SOURCE = 0, 2, 3

BUILD_SH = r"""#!/bin/sh
# 由 WorkBuddy buildkit 生成，勿手工修改：改动会让证据链与生成记录对不上。
# 退出码 0=跑完 2=编译/链接失败 3=无源文件
set -u
cd "$(dirname "$0")" || exit 2

sec() { printf 'WB_SECTION %s\n' "$1"; }

sec env
printf 'uname=%s\n' "$(uname -srm 2>/dev/null)"
printf 'cores=%s\n' "$(nproc 2>/dev/null || echo 1)"
printf 'gcc=%s\n' "$(gcc --version 2>/dev/null | head -1)"
printf 'gcov=%s\n' "$(gcov --version 2>/dev/null | head -1)"
printf 'date=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'cwd=%s\n' "$(pwd)"
printf 'cflags=%s\n' "__CFLAGS__"

rm -rf build
mkdir -p build

SRCS=""
for f in src/*.c tests/*.c; do
    [ -e "$f" ] || continue
    SRCS="$SRCS $f"
done
if [ -z "$SRCS" ]; then
    sec build
    printf 'WB_BUILD_RESULT fail\n'
    printf 'no source files under src/ or tests/\n'
    sec end
    exit 3
fi

sec build
status=0
for f in $SRCS; do
    obj="build/$(printf '%s' "$f" | tr '/' '_' | sed 's/\.c$/.o/')"
    gcc __CFLAGS__ __INCLUDES__ -c "$f" -o "$obj" 2>&1 || status=1
done
if [ "$status" -ne 0 ]; then
    printf 'WB_BUILD_RESULT fail\n'
    sec end
    exit 2
fi
gcc __CFLAGS__ __INCLUDES__ build/*.o -o build/wb_tests -lm 2>&1 || status=1
if [ "$status" -ne 0 ]; then
    printf 'WB_BUILD_RESULT fail\n'
    sec end
    exit 2
fi
printf 'WB_BUILD_RESULT ok\n'

sec run
./build/wb_tests
printf 'WB_RUN_EXIT %d\n' $?

sec coverage
# 只对被测模块采覆盖率：测试桩自身的覆盖率不是交付判据。
# 每个 gcda 前打一条 WB_GCOV_TARGET，解析器据此把 Function 块归属到源文件——
# gcov 的函数块本身不带文件名，多个编译单元里有同名 static 函数时无法区分。
for f in src/*.c; do
    [ -e "$f" ] || continue
    g="build/$(printf '%s' "$f" | tr '/' '_' | sed 's/\.c$/.gcda/')"
    [ -e "$g" ] || continue
    printf 'WB_GCOV_TARGET %s\n' "$f"
    gcov -b -c -f "$g" 2>&1
done
rm -f ./*.gcov

sec end
exit 0
"""

# 测试桩：用例结果必须由测试代码显式上报，格式与 parsers.parse_test_output 对齐。
# 这里刻意用「函数 + 一个便捷宏」而不是自造断言框架：
#   1. 进程中途崩溃时，已 fflush 的结果行仍然可用，能看出崩在哪条用例之后；
#   2. WB_TOTAL / WB_FAILED 由桩自己汇总，与解析出的条数交叉校验，防止漏报。
HARNESS_H = r"""#ifndef WB_HARNESS_H
#define WB_HARNESS_H
/* WorkBuddy 测试桩接口。用例结果统一通过 wb_report 上报。 */

#include <stdio.h>   /* snprintf：WB_CHECK 需要，不依赖调用方先包含 stdio */

void wb_begin(void);
void wb_report(const char *id, int ok, const char *detail);
int  wb_summary(void);
int  wb_failed(void);

/* 便捷宏：条件成立记 PASS，否则把 fmt 格式化成单行原因后记 FAIL。
   detail 必须是单行文本，换行会破坏结果行的解析。
   cond 只求值一次并存进 wb_ok_：测试条件里常有带副作用的调用（发包、解帧、
   推进序号），求值两次会让被测状态走两遍，用例结果就与真实行为无关了。 */
#define WB_CHECK(id, cond, fmt, ...)                                        \
    do {                                                                    \
        char wb_msg_[256];                                                  \
        int wb_ok_ = (cond) ? 1 : 0;                                        \
        wb_msg_[0] = '\0';                                                  \
        if (!wb_ok_) { snprintf(wb_msg_, sizeof(wb_msg_), (fmt), ##__VA_ARGS__); } \
        wb_report((id), wb_ok_, wb_msg_);                                   \
    } while (0)

#define WB_PASS(id) wb_report((id), 1, "")
#define WB_FAIL(id, fmt, ...)                                               \
    do {                                                                    \
        char wb_msg_[256];                                                  \
        snprintf(wb_msg_, sizeof(wb_msg_), (fmt), ##__VA_ARGS__);            \
        wb_report((id), 0, wb_msg_);                                        \
    } while (0)

#endif /* WB_HARNESS_H */
"""

HARNESS_C = r"""#include <stdio.h>
#include <string.h>

#include "wb_harness.h"

static int wb_total_ = 0;
static int wb_failed_ = 0;

void wb_begin(void)
{
    wb_total_ = 0;
    wb_failed_ = 0;
    printf("WB_BEGIN\n");
    fflush(stdout);
}

/* detail 里的换行会被压成空格：结果行必须一行一条，否则解析器读不出来 */
void wb_report(const char *id, int ok, const char *detail)
{
    char buf[256];
    size_t i;

    wb_total_++;
    if (ok) {
        printf("%s PASS\n", id ? id : "TC-???");
    } else {
        wb_failed_++;
        buf[0] = '\0';
        if (detail != NULL) {
            strncpy(buf, detail, sizeof(buf) - 1);
            buf[sizeof(buf) - 1] = '\0';
            for (i = 0; buf[i] != '\0'; i++) {
                if (buf[i] == '\n' || buf[i] == '\r') {
                    buf[i] = ' ';
                }
            }
        }
        printf("%s FAIL %s\n", id ? id : "TC-???", buf);
    }
    fflush(stdout);   /* 崩溃时已上报的结果不能丢 */
}

int wb_failed(void)
{
    return wb_failed_;
}

int wb_summary(void)
{
    printf("WB_TOTAL %d\n", wb_total_);
    printf("WB_FAILED %d\n", wb_failed_);
    printf("WB_END\n");
    fflush(stdout);
    return wb_failed_;
}
"""

HARNESS_FILES = {"tests/wb_harness.h": HARNESS_H, "tests/wb_harness.c": HARNESS_C}


def build_script() -> str:
    return (BUILD_SH.replace("__CFLAGS__", CFLAGS)
                    .replace("__INCLUDES__", INCLUDES))


# 编译探针：只做「能不能编译/能不能链接」这一件事，不运行、不采覆盖率。
# 代码与测试实现两个阶段的硬校验器用它——AI 说写完了不算，工具链说能编才算。
CHECK_SH = r"""#!/bin/sh
# 由 WorkBuddy buildkit 生成的编译探针。退出码 0=通过 2=编译或链接失败 3=无源文件
set -u
cd "$(dirname "$0")" || exit 2

rm -rf build
mkdir -p build

SRCS=""
for f in src/*.c tests/*.c; do
    [ -e "$f" ] || continue
    SRCS="$SRCS $f"
done
if [ -z "$SRCS" ]; then
    printf 'no source files under src/ or tests/\n'
    exit 3
fi

status=0
for f in $SRCS; do
    obj="build/$(printf '%s' "$f" | tr '/' '_' | sed 's/\.c$/.o/')"
    gcc __CFLAGS_NO_COV__ __INCLUDES__ -c "$f" -o "$obj" 2>&1 || status=1
done
if [ "$status" -ne 0 ]; then
    printf 'WB_BUILD_RESULT fail\n'
    exit 2
fi
__LINK_BLOCK__
printf 'WB_BUILD_RESULT ok\n'
exit 0
"""

_LINK_BLOCK = """gcc __CFLAGS_NO_COV__ __INCLUDES__ build/*.o -o build/wb_tests -lm 2>&1 || {
    printf 'WB_BUILD_RESULT link_fail\\n'
    exit 2
}"""

# 探针不带覆盖率插桩：插桩会引入额外告警面，而这里只判「能不能编」
CFLAGS_NO_COV = "-std=c99 -Wall -Wextra -g -O0"


def check_script(link: bool = False) -> str:
    # 注意替换顺序：链接块自身也含编译选项占位符，必须先把块内的占位符换掉，
    # 再塞进主脚本，否则会把 __CFLAGS_NO_COV__ 原样交给 gcc。
    block = (_LINK_BLOCK.replace("__CFLAGS_NO_COV__", CFLAGS_NO_COV)
                        .replace("__INCLUDES__", INCLUDES) if link else "")
    return (CHECK_SH.replace("__LINK_BLOCK__", block)
                    .replace("__CFLAGS_NO_COV__", CFLAGS_NO_COV)
                    .replace("__INCLUDES__", INCLUDES))


def workspace_files(code_files: dict | None = None,
                    test_files: dict | None = None) -> dict:
    """拼出要同步到验证机的完整工作区：被测代码 + 测试 + 桩 + 构建脚本。

    路径一律规范化成正斜杠相对路径，并按目录约定过滤——把模型可能写出的
    绝对路径、`./`、反斜杠挡在同步之前，否则 tar 解包会落到工作区外面。"""
    out = {"build.sh": build_script()}
    for files in (code_files, test_files):
        for rel, content in (files or {}).items():
            path = normalize_path(rel)
            if path:
                out[path] = content
    # 测试桩最后写入并保持权威：结果行的格式是解析器的契约，
    # 被生成物覆盖掉就意味着「全绿」再也无法被信任。
    out.update(HARNESS_FILES)
    return out


def normalize_path(rel: str) -> str:
    """把模型给出的文件路径规范成工作区内的相对路径；越界或非法返回空串。"""
    p = str(rel or "").replace("\\", "/").strip()
    if not p:
        return ""
    while p.startswith("./"):
        p = p[2:]
    if p.startswith("/") or ".." in p.split("/"):
        return ""           # 绝对路径与上跳一律拒绝
    if len(p) >= 2 and p[1] == ":" and p[0].isalpha():
        return ""           # Windows 盘符绝对路径（C:/…）同样拒绝：不是工作区内相对路径
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts:
        return ""
    return "/".join(parts)


def split_by_role(files: dict) -> dict:
    """按目录约定把 {路径: 内容} 分成 src / include / tests / other。"""
    out = {"src": {}, "include": {}, "tests": {}, "other": {}}
    for rel, content in (files or {}).items():
        p = normalize_path(rel)
        if not p:
            continue
        if p.startswith("src/") and p.endswith(".c"):
            out["src"][p] = content
        elif p.startswith("include/") and p.endswith(".h"):
            out["include"][p] = content
        elif p.startswith("tests/"):
            out["tests"][p] = content
        else:
            out["other"][p] = content
    return out


def has_main(test_files: dict) -> bool:
    """测试代码里是否有 main（链接期才会暴露的问题，提前给出明确反馈）。"""
    for content in (test_files or {}).values():
        text = str(content or "")
        if "int main" in text or "void main" in text:
            return True
    return False


def layout_feedback(code_files: dict | None = None,
                    test_files: dict | None = None) -> str:
    """目录约定不符时给生成智能体的整改反馈（validator 用）。"""
    problems = []
    roles = split_by_role(code_files or {})
    for rel in (code_files or {}):
        p = normalize_path(rel)
        if not p:
            problems.append(f"文件路径非法或越出工作区：{rel}")
        elif p not in roles["src"] and p not in roles["include"]:
            problems.append(
                f"被测代码 {p} 不在约定目录：实现放 src/*.c，头文件放 include/*.h")
    if code_files and not roles["src"]:
        problems.append("src/ 下没有任何 .c 实现文件，无法编译与采集覆盖率")
    if test_files is not None:
        troles = split_by_role(test_files)
        for rel in test_files:
            p = normalize_path(rel)
            if p and p not in troles["tests"]:
                problems.append(f"测试代码 {p} 不在约定目录：测试一律放 tests/*.c")
        if test_files and not has_main(test_files):
            problems.append("tests/ 下没有定义 main 函数，测试程序无法链接")
    return "\n".join(f"- {x}" for x in problems)
