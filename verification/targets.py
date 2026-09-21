"""目标机矩阵：同一份源码在多个指令集 / 字长 / 字节序上分别编译并执行。

第一性原理：宿主机 x86-64 上跑绿，只证明「在这台机器的 gcc 与 ABI 下行为正确」。
星载软件的真实风险恰恰落在宿主机测不出来的地方——long 是 4 字节还是 8 字节、
大端还是小端、非对齐访问、32 位截断。这些不是靠人评审能看出来的，只能换目标机跑。

qemu-user 把「换目标机」的成本压到一条命令：交叉编译出静态二进制，
用 qemu-<arch>-static 直接执行，gcov 插桩数据照常落盘。这条路已在验证机实测通过
（arm32 小端 / aarch64 / ppc32 大端三个目标都能跑，且 *-gcov -b -c 能正常解析 .gcda）。

所以本模块只做两件事：一张目标表，以及把表翻译成构建脚本参数与探测命令的适配层。
与 core/c_rules.py 同一个套路——判据先落成表，再由表导出脚本与报告，
不出现「表里说要测大端、脚本里却只编了 x86」这种漂移。

术语（重要）：本模块的 target 指**目标架构**；runner.target 指**验证机地址**
（user@host:port）。验证机是跑 qemu 的那台 x86 机器，目标机是被模拟的架构。
"""
from __future__ import annotations

from dataclasses import dataclass


class TargetError(ValueError):
    """目标 id 不在表内。配置写错必须当场炸，不能静默降级成只测宿主机——
    那样报告上仍写着「已在 N 个目标上验证」，而实际证据只有一个目标。"""


@dataclass(frozen=True)
class Target:
    """一个可执行验证的目标架构。

    qemu 为空串表示本机直接执行（验证机自身架构）。extra_cflags 里对交叉目标
    一律加 -static：动态执行需要 qemu 找到目标架构的动态链接器与库搜索路径，
    静态链接把这类环境依赖一次性消掉，证据也就与验证机的库版本无关。"""
    id: str
    label: str
    cc: str
    gcov: str
    qemu: str
    extra_cflags: str
    bits: int
    endian: str                 # "little" | "big"
    why: str
    host: bool = False

    @property
    def run_prefix(self) -> str:
        """执行测试二进制时要加的前缀（含尾随空格）；本机执行时为空。"""
        return f"{self.qemu} " if self.qemu else ""

    @property
    def tools(self) -> tuple:
        """这个目标要用到的可执行文件，探测时逐个 command -v 检查。"""
        return (self.cc, self.gcov) + ((self.qemu,) if self.qemu else ())

    def facts(self) -> dict:
        """写进证据的目标指纹。结论要能复现，就得记下当时用的是哪套工具。"""
        return {"id": self.id, "label": self.label, "cc": self.cc,
                "gcov": self.gcov, "qemu": self.qemu or None,
                "bits": self.bits, "endian": self.endian,
                "extra_cflags": self.extra_cflags or None}


TARGETS: list = [
    Target(
        id="host", label="验证机本机 x86-64", cc="gcc", gcov="gcov", qemu="",
        extra_cflags="", bits=64, endian="little", host=True,
        why="基线目标：与既有证据链完全一致，速度最快，用作交叉目标之间的对照组。"
            "只有 host 通过、交叉目标失败时，才能把问题定位成可移植性缺陷。"),
    Target(
        id="arm32", label="ARM 32 位小端（armhf）",
        cc="arm-linux-gnueabihf-gcc", gcov="arm-linux-gnueabihf-gcov",
        qemu="qemu-arm-static", extra_cflags="-static", bits=32, endian="little",
        why="32 位 ABI：long 与指针都是 4 字节，能抓出宿主机 64 位下被掩盖的整型截断、"
            "sizeof 误用、指针与整型互转；ARM 也是星载/弹载最常见的主力架构。"),
    Target(
        id="arm64", label="ARM 64 位小端（aarch64）",
        cc="aarch64-linux-gnu-gcc", gcov="aarch64-linux-gnu-gcov",
        qemu="qemu-aarch64-static", extra_cflags="-static", bits=64, endian="little",
        why="64 位 ARM：与 arm32 配成一对，专门暴露「同一份代码在 32/64 位下行为不同」，"
            "典型是结构体对齐、位域布局与 long 宽度依赖。"),
    Target(
        id="ppc32", label="PowerPC 32 位大端",
        cc="powerpc-linux-gnu-gcc", gcov="powerpc-linux-gnu-gcov",
        qemu="qemu-ppc-static", extra_cflags="-static", bits=32, endian="big",
        why="大端字节序：把「按字节拼帧、按整型读缓冲、memcpy 传结构体上链路」这类"
            "隐含小端假设的写法直接判死；PowerPC 也是航天传统架构（如 RAD750）。"),
]

BY_ID: dict = {t.id: t for t in TARGETS}

# 默认只测宿主机：保持既有项目的证据布局与耗时不变，多目标由 .env 显式开启。
DEFAULT_TARGET_IDS: tuple = ("host",)


def by_id(target_id: str):
    return BY_ID.get(str(target_id or "").strip())


def resolve(spec=None) -> list:
    """把配置里的目标声明解析成 Target 列表，顺序即执行顺序。

    接受 None（默认）、"host,arm32" 逗号串、或 id 列表。未知 id 抛 TargetError。
    host 若未显式声明也不自动插入：要不要对照组是使用方的决定，
    偷偷加一个目标会让耗时与证据数量对不上报告里的说明。"""
    if spec is None:
        raw = list(DEFAULT_TARGET_IDS)
    elif isinstance(spec, str):
        raw = [x.strip() for x in spec.split(",") if x.strip()]
    else:
        raw = [str(x).strip() for x in spec if str(x).strip()]
    if not raw:
        raw = list(DEFAULT_TARGET_IDS)
    out, seen = [], set()
    for tid in raw:
        tgt = by_id(tid)
        if tgt is None:
            raise TargetError(
                f"未知目标机 {tid!r}，可选：{', '.join(BY_ID)}")
        if tgt.id in seen:
            continue
        seen.add(tgt.id)
        out.append(tgt)
    return out


def ids(spec=None) -> list:
    return [t.id for t in resolve(spec)]


# ---------------- 工具链探测 ----------------
# 探测必须在跑之前做完：配了 arm32 而验证机没装交叉编译器时，
# 结论是「该目标未验证」（blocked），绝不能当成「该目标通过」。
_PROBE_TMPL = """miss=""
for c in __TOOLS__; do command -v "$c" >/dev/null 2>&1 || miss="$miss $c"; done
if [ -z "$miss" ]; then
    printf 'WB_TARGET __ID__ ok cc=%s\\n' "$(__CC__ --version 2>/dev/null | head -1)"
else
    printf 'WB_TARGET __ID__ missing tools=%s\\n' "$miss"
fi
"""


def probe_script(targets) -> str:
    """生成一段 sh：逐个目标检查交叉编译器与 qemu 是否就位。"""
    parts = ["#!/bin/sh", "set -u"]
    for t in targets:
        parts.append(_PROBE_TMPL
                     .replace("__TOOLS__", " ".join(t.tools))
                     .replace("__CC__", t.cc)
                     .replace("__ID__", t.id))
    return "\n".join(parts)


def parse_probe(stdout: str) -> dict:
    """解析 WB_TARGET 行 → {id: {"ok": bool, "status": str, ...}}。

    没出现在输出里的目标一律记为 unknown（而不是 missing）：
    脚本没跑完与工具确实没装是两种情况，前者要重跑，后者要装包。"""
    out: dict = {}
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("WB_TARGET "):
            continue
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        tid, status = parts[1], parts[2]
        info = {"status": status, "ok": status == "ok"}
        rest = parts[3] if len(parts) > 3 else ""
        key, sep, val = rest.partition("=")
        if sep:
            info[key.strip()] = val.strip()
        out[tid] = info
    return out


def probe_targets(runner, targets, timeout: int = 60) -> dict:
    """在验证机上探测目标工具链。返回 {id: {...}}，未探测到的记 unknown。"""
    targets = list(targets or [])
    if not targets:
        return {}               # 没有交叉目标就不发远端调用：单 host 路径耗时不变
    script = probe_script(targets)
    res = runner.run(script, timeout=timeout)
    found = parse_probe(res.stdout or "")
    out = {}
    for t in targets:
        info = found.get(t.id)
        if info is None:
            info = {"status": "unknown", "ok": False,
                    "tools": "探测脚本未返回该目标的结果"}
        out[t.id] = {**t.facts(), **info}
    return out


def unavailable_reason(probed: dict, targets) -> str:
    """有目标工具链缺失时给出人话原因；全部就位返回空串。"""
    bad = []
    for t in targets:
        info = (probed or {}).get(t.id) or {}
        if not info.get("ok"):
            bad.append(f"{t.id}（缺 {str(info.get('tools') or '工具链').strip()}）")
    return "；".join(bad)


def describe_table(targets) -> str:
    """报告里的目标机说明表：id / 架构事实 / 用哪个编译器 / 为什么要测它。"""
    lines = ["| 目标 | 字长/字节序 | 编译器 | 执行方式 | 这个目标能抓出什么 |",
             "|---|---|---|---|---|"]
    for t in targets:
        run = f"`{t.qemu}`" if t.qemu else "本机直接执行"
        lines.append(f"| {t.id} · {t.label} | {t.bits} 位 / "
                     f"{'大端' if t.endian == 'big' else '小端'} | `{t.cc}` | {run} "
                     f"| {t.why} |")
    return "\n".join(lines)
