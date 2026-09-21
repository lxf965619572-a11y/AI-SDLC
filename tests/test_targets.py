"""目标机表（verification/targets.py）的测试：解析、探测、以及安装脚本的防漂移。

第一性原理：目标表是整套多目标验证的唯一判据来源——构建脚本里的编译器名、
探测脚本要检查哪些可执行文件、报告里写字长字节序与「为什么要测这个目标」，
全部由这张表导出。所以这里钉的是三类契约：

  1. 配置解析不能静默降级：写了不存在的目标 id 必须当场抛错。
     静默退回「只测 host」会让报告写着「已在 3 个目标上验证」而证据只有 1 个；
  2. 探测结论不能把「没探测到」当成「没装」：脚本没跑完（unknown）与工具确实
     缺失（missing）处置不同，前者重跑、后者装包，两者都不允许被记成 ok；
  3. 表与安装脚本不能漂移：scripts/setup_verify_targets.sh 里手写了同一批工具名与
     目标事实，加一个新目标却忘了改安装脚本，是这套机制最可能坏的方式。

零第三方依赖、不连验证机（探测用 FakeRunner 替身）：
    .venv\\Scripts\\python.exe tests\\test_targets.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config
from verification import targets as T
from verification.runner import FakeRunner

SETUP_SH = ROOT / "scripts" / "setup_verify_targets.sh"


def _setup_text() -> str:
    return SETUP_SH.read_text(encoding="utf-8")


# ================= 目标表本身 =================
def test_table_ids_are_unique_and_host_present():
    ids = [t.id for t in T.TARGETS]
    assert len(ids) == len(set(ids)), ids
    assert "host" in ids
    assert set(T.BY_ID) == set(ids)


def test_table_covers_word_length_and_endianness_dimensions():
    """表存在的理由就是覆盖这几个维度：32/64 位与大端/小端都必须有目标。
    只测 x86-64 小端时，整型截断与字节序假设这两类缺陷根本测不出来。"""
    combos = {(t.bits, t.endian) for t in T.TARGETS}
    assert (32, "little") in combos
    assert (64, "little") in combos
    assert (32, "big") in combos


def test_host_target_runs_natively_without_qemu():
    h = T.by_id("host")
    assert h.host is True and h.qemu == ""
    assert h.run_prefix == ""
    assert h.tools == ("gcc", "gcov")


def test_cross_targets_are_static_and_carry_qemu():
    """交叉目标一律 -static：动态执行要 qemu 找得到目标架构的动态链接器与库路径，
    静态链接把这类环境依赖一次消掉，证据也就与验证机的库版本无关。"""
    for t in T.TARGETS:
        if t.host:
            continue
        assert t.qemu, t.id
        assert t.run_prefix == f"{t.qemu} "
        assert "-static" in t.extra_cflags, t.id
        assert t.tools == (t.cc, t.gcov, t.qemu)


def test_facts_are_serializable_and_record_toolchain():
    f = T.by_id("ppc32").facts()
    assert f["id"] == "ppc32" and f["bits"] == 32 and f["endian"] == "big"
    assert f["cc"] == "powerpc-linux-gnu-gcc"
    assert f["qemu"] == "qemu-ppc-static"
    assert T.by_id("host").facts()["qemu"] is None      # 本机执行不写 qemu


def test_every_target_explains_why_it_is_tested():
    """报告里要回答「为什么要测这个目标」，所以 why 不能是空的套话。"""
    for t in T.TARGETS:
        assert len(t.why) > 10, t.id
        assert t.label and t.label != t.id


# ================= resolve / ids =================
def test_resolve_default_is_host_only():
    """默认只测宿主机：既有项目的证据布局与耗时不变，多目标要显式开启。"""
    assert [t.id for t in T.resolve(None)] == ["host"]
    assert [t.id for t in T.resolve("")] == ["host"]
    assert [t.id for t in T.resolve([])] == ["host"]


def test_resolve_comma_string_keeps_order():
    """顺序即执行顺序，报告与日志都按它排，不能是集合的随机序。"""
    assert T.ids("host,arm32,ppc32") == ["host", "arm32", "ppc32"]
    assert T.ids(" ppc32 , host ") == ["ppc32", "host"]


def test_resolve_accepts_list_and_dedupes():
    assert T.ids(["arm64", "host", "arm64"]) == ["arm64", "host"]
    assert T.ids("host,host") == ["host"]


def test_resolve_does_not_auto_insert_host():
    """要不要对照组是使用方的决定：偷偷加一个目标会让耗时与证据数量
    对不上报告里的说明。"""
    assert T.ids("arm32") == ["arm32"]
    assert T.ids("arm64,ppc32") == ["arm64", "ppc32"]


def test_resolve_unknown_id_raises_not_degrades():
    """写错目标 id 必须当场炸。静默退回 host 会造成「报告说测了、其实没测」。"""
    for bad in ("arm", "x86", "host,arm33", "ARM32"):
        try:
            T.resolve(bad)
        except T.TargetError as e:
            assert "未知目标机" in str(e) and "可选" in str(e)
        else:
            raise AssertionError(f"{bad} 应当抛 TargetError")


def test_by_id_unknown_returns_none():
    assert T.by_id("nope") is None
    assert T.by_id("") is None
    assert T.by_id(None) is None


def test_config_verify_target_ids_parses_and_raises():
    """.env 的 VERIFY_TARGETS → 目标 id 列表；写错同样抛错，不静默降级。"""
    saved = config.VERIFY_TARGETS
    try:
        config.VERIFY_TARGETS = "host,ppc32"
        assert config.verify_target_ids() == ["host", "ppc32"]
        config.VERIFY_TARGETS = "  arm32 , arm64 "
        assert config.verify_target_ids() == ["arm32", "arm64"]
        config.VERIFY_TARGETS = ""
        assert config.verify_target_ids() == ["host"]
        config.VERIFY_TARGETS = "mips"
        try:
            config.verify_target_ids()
        except T.TargetError:
            pass
        else:
            raise AssertionError("非法 VERIFY_TARGETS 应当抛 TargetError")
    finally:
        config.VERIFY_TARGETS = saved


# ================= 探测脚本 =================
def test_probe_script_checks_every_tool_of_every_target():
    sh = T.probe_script(T.TARGETS)
    assert sh.startswith("#!/bin/sh")
    for t in T.TARGETS:
        for tool in t.tools:
            assert tool in sh, f"{t.id} 的 {tool} 没进探测脚本"
        assert f"WB_TARGET {t.id} " in sh


def test_probe_script_only_covers_requested_targets():
    """只探测本次要用的目标：多探一个就多一条「装了但用不上」的干扰信息。"""
    sh = T.probe_script(T.resolve("arm32"))
    assert "qemu-arm-static" in sh
    assert "qemu-ppc-static" not in sh


def test_probe_script_empty_targets_still_valid_sh():
    sh = T.probe_script([])
    assert sh.splitlines()[0] == "#!/bin/sh"
    assert "WB_TARGET" not in sh


# ================= 探测结果解析 =================
def test_parse_probe_ok_line():
    out = T.parse_probe("WB_TARGET arm32 ok cc=arm-linux-gnueabihf-gcc (Ubuntu) 7.5.0\n")
    info = out["arm32"]
    assert info["ok"] is True and info["status"] == "ok"
    assert info["cc"].startswith("arm-linux-gnueabihf-gcc")


def test_parse_probe_missing_records_which_tools():
    """缺哪些工具必须点名：装包的人要知道装什么，不能只给一句「不可用」。"""
    out = T.parse_probe("WB_TARGET ppc32 missing tools= powerpc-linux-gnu-gcov\n")
    info = out["ppc32"]
    assert info["ok"] is False and info["status"] == "missing"
    assert "powerpc-linux-gnu-gcov" in info["tools"]


def test_parse_probe_ignores_noise_and_malformed_lines():
    out = T.parse_probe("\n".join([
        "bash: warning: setlocale", "WB_TARGET", "WB_TARGET arm64",
        "WB_TARGET arm64 ok cc=aarch64-linux-gnu-gcc 7.5.0", ""]))
    assert list(out) == ["arm64"] and out["arm64"]["ok"] is True


def test_parse_probe_line_without_value_keeps_status():
    out = T.parse_probe("WB_TARGET host ok\n")
    assert out["host"] == {"status": "ok", "ok": True}


def test_probe_targets_marks_unreported_as_unknown_not_ok():
    """脚本没跑完（超时、被截断）与工具确实没装是两种情况：
    前者记 unknown 要重跑，后者记 missing 要装包，两者都不许被当成 ok。"""
    r = FakeRunner(responses={"#!/bin/sh": {
        "exit_code": 0,
        "stdout": "WB_TARGET arm32 ok cc=arm-linux-gnueabihf-gcc 7.5.0\n"}})
    probed = T.probe_targets(r, T.resolve("host,arm32,ppc32"))
    assert probed["arm32"]["ok"] is True
    assert probed["ppc32"]["status"] == "unknown" and probed["ppc32"]["ok"] is False
    assert probed["host"]["status"] == "unknown"
    # 目标事实随探测结果一并带回：证据里要能看出当时用的是哪套工具
    assert probed["arm32"]["bits"] == 32 and probed["arm32"]["cc"]


def test_probe_targets_no_extra_call_when_no_targets():
    """单目标 host 路径不产生任何新的远端调用：既有证据与耗时一字不变。"""
    r = FakeRunner()
    assert T.probe_targets(r, []) == {}
    assert r.calls == []


# ================= 不可用原因 =================
def test_unavailable_reason_names_each_dead_target():
    probed = {"arm32": {"ok": False, "tools": " qemu-arm-static"},
              "ppc32": {"ok": True}}
    why = T.unavailable_reason(probed, T.resolve("arm32,ppc32"))
    assert "arm32" in why and "qemu-arm-static" in why
    assert "ppc32" not in why


def test_unavailable_reason_empty_when_all_ready():
    probed = {t.id: {"ok": True} for t in T.TARGETS}
    assert T.unavailable_reason(probed, T.TARGETS) == ""


def test_unavailable_reason_treats_absent_probe_as_unavailable():
    """探测结果里根本没有这个目标（脚本被截断）也算不可用——缺证据不等于就位。"""
    why = T.unavailable_reason({}, T.resolve("arm64"))
    assert why and "arm64" in why


# ================= 报告用说明表 =================
def test_describe_table_has_row_per_target_with_why():
    md = T.describe_table(T.resolve("host,ppc32"))
    assert md.startswith("| 目标 |")
    # 表头 + 分隔行 + 数据行；分隔行是 "|---|" 不以 "| " 开头，故只跳过表头一行
    rows = [ln for ln in md.splitlines() if ln.startswith("| ")][1:]
    assert len(rows) == 2
    assert "大端" in rows[1] and "powerpc-linux-gnu-gcc" in rows[1]
    assert "本机直接执行" in rows[0] and "qemu-ppc-static" in rows[1]


# ================= 与安装脚本的一致性（防漂移） =================
def test_setup_script_exists_with_lf_endings():
    """CRLF 的 .sh 在 Linux 上根本跑不起来（bash 会把 \\r 当成命令名的一部分），
    而这个仓库是在 Windows 上开发的——行尾必须钉住。"""
    data = SETUP_SH.read_bytes()
    assert data, "安装脚本为空"
    assert b"\r" not in data, "安装脚本必须是 LF 行尾"
    assert data.startswith(b"#!/usr/bin/env bash")


def test_setup_script_mentions_every_target_tool():
    """加了一个新目标却忘了改安装脚本 → 装不上工具链 → 该目标永远「未执行」。
    这条测试把表与脚本绑在一起。"""
    text = _setup_text()
    for t in T.TARGETS:
        assert re.search(rf"^\s*{t.id}\)", text, re.M), f"安装脚本没有 {t.id} 分支"
        for tool in t.tools:
            assert tool in text, f"安装脚本缺工具名 {tool}（目标 {t.id}）"


def test_setup_script_facts_match_table():
    """安装脚本冒烟时按这张表核对实跑的字长/字节序，表错了就会误判成「目标不对」。"""
    text = _setup_text()
    block = re.search(r"# WB_SMOKE_FACTS_BEGIN(.*?)# WB_SMOKE_FACTS_END", text, re.S)
    assert block, "安装脚本缺 WB_SMOKE_FACTS 标记块"
    facts = {m[0]: (m[1], m[2]) for m in
             re.findall(r'(\w+)\)\s+echo\s+"(\d+)\s+(little|big)"', block.group(1))}
    assert facts, "没解析出任何目标事实"
    for t in T.TARGETS:
        assert t.id in facts, f"安装脚本没写 {t.id} 的目标事实"
        bits, endian = facts[t.id]
        assert int(bits) == t.bits, f"{t.id} 字长不符：脚本 {bits} / 表 {t.bits}"
        assert endian == t.endian, f"{t.id} 字节序不符：脚本 {endian} / 表 {t.endian}"


def test_setup_script_is_idempotent_and_never_silent():
    """幂等与「不静默」是安装脚本的两条硬要求，写成断言防止后续改动丢掉。"""
    text = _setup_text()
    assert "工具链已齐备，无需安装" in text            # 幂等：装齐就不碰 apt
    assert "exit 1" in text                            # 失败必须非零退出
    assert "WB_SUDO_PASSWORD" in text                  # 密码经 stdin，不入库
    assert "--no-install-recommends" in text
    assert "sudo -S -p" in text


def _main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except Exception as e:
            failed.append(name)
            print("FAIL  " + name + ": " + type(e).__name__ + ": " + str(e))
    print("\n%d/%d passed" % (len(tests) - len(failed), len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
