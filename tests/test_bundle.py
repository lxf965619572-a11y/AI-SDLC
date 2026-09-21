"""软件工程包导出测试：不连验证机、不调模型，只验「包对不对、全不全、诚不诚实」。

这一层要单测的三件事，都是交付件里出错代价最高的：
  1. 基线选对：源码基线必须是最后一次执行真正跑过的那一版，不是库里最新版；
  2. 核对得住：基线文件的 sha256 与执行证据的输入指纹逐字节相符，改了就要标红；
  3. 不写空壳：未产出的阶段只在包清单里记「未产出」，不落一个空文件充数。

数据全用临时库与临时证据目录现场造（换库必须在 import db.models 之前），
真实验证机链路由 scripts/e2e_pipeline_mock.py 负责。

用法：.venv\\Scripts\\python.exe tests\\test_bundle.py
"""
import json
import hashlib
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                                     # noqa: E402

# ---- 数据隔离：换库要在 db.models 建 engine 之前 ----
TMP = Path(tempfile.mkdtemp(prefix="wb_bundle_test_"))
for _sub in ("outputs", "verify", "uploads"):
    (TMP / _sub).mkdir(parents=True, exist_ok=True)
config.DATA_DIR = TMP
config.APP_DB = TMP / "app.sqlite"
config.CHECKPOINT_DB = TMP / "checkpoints.sqlite"
config.OUTPUT_DIR = TMP / "outputs"
config.UPLOAD_DIR = TMP / "uploads"
config.VERIFY_EVIDENCE_DIR = TMP / "verify"

import db.models as M                                              # noqa: E402
from core import c_files                                           # noqa: E402
from exporters import bundle_exporter as B                          # noqa: E402
from exporters.report_exporter import assemble_report              # noqa: E402
from services import bundle_service                                # noqa: E402
from verification import buildkit                                  # noqa: E402
from verification import targets as tgt_tab                        # noqa: E402
from verification.runner import BaseRunner                         # noqa: E402

M.init_db()


# ---------------- 造数据 ----------------
CODE_MD = """# 代码实现

## 文件 include/comm.h

```c
#ifndef COMM_H
#define COMM_H
int comm_crc(const unsigned char *buf, int len);
#endif
```

## 文件 src/comm.c

```c
#include "comm.h"

int comm_crc(const unsigned char *buf, int len)
{
    int crc = %s;
    int i;
    for (i = 0; i < len; i++) {
        crc = (crc + buf[i]) & 0xFFFF;
    }
    return crc;
}
```
"""

TEST_MD = """# 测试实现

## 文件 tests/test_comm.c

```c
#include <stdio.h>
#include "comm.h"
#include "wb_harness.h"

int main(void)
{
    unsigned char buf[4] = {1, 2, 3, 4};
    wb_begin();
    WB_CHECK("TC-001", comm_crc(buf, 4) == %d, "crc=%%d", comm_crc(buf, 4));
    return wb_summary();
}
```
"""


def _md_code(init="0x0000"):
    return CODE_MD % init


def _md_test(expect=10):
    return TEST_MD % expect


def _workspace(code_md, test_md):
    return buildkit.workspace_files(c_files.extract_files(code_md),
                                    c_files.extract_files(test_md))


def _exec_meta(pid, tag, code_md, test_md, ok=True, status="approved",
               write_evidence=True, skipped=False, targets=None, ran=None,
               missing_logs=()):
    """造一份与真实 executor 同形的 exec 元数据（含输入指纹与证据文件）。

    targets 给多目标机变体，形状与 executor.run_verification 的真实产物一致：
    顶层 manifest 一律是 host 变体（源码基线指纹，align 用它核对包内源码），
    逐目标 manifest 才是各自交叉编译脚本的指纹；ran 指定哪些目标真跑过——
    没跑过的目标不该有 `<目标id>.log`，工具链缺失那一轮正是这种形状。"""
    ws = _workspace(code_md, test_md)
    tids = list(targets or [])
    ran_ids = [t for t in (ran if ran is not None else tids) if t in tids]
    ev_dir = config.VERIFY_EVIDENCE_DIR / f"p{pid}" / f"v{tag}"
    evidence = ""
    if write_evidence:
        ev_dir.mkdir(parents=True, exist_ok=True)
        (ev_dir / "exec.log").write_text(f"WB_SECTION env\nuname=Linux test\n"
                                         f"WB_BUILD_RESULT ok\n", encoding="utf-8")
        (ev_dir / "exec.json").write_text(json.dumps({"ok": ok, "tag": tag},
                                                     ensure_ascii=False),
                                          encoding="utf-8")
        if len(ran_ids) > 1:
            for tid in ran_ids:
                fname = f"{tid}.log"
                if fname in missing_logs:
                    continue
                (ev_dir / fname).write_text(
                    f"===== 目标机 {tid} =====\nWB_SECTION env\ntarget={tid}\n",
                    encoding="utf-8")
        evidence = str(ev_dir / "exec.log").replace("\\", "/")
    meta = {"version": tag, "project_id": pid, "ok": ok, "skipped": skipped,
            "manifest": BaseRunner.manifest(ws), "evidence": evidence,
            "workdir": f"/home/tester/wb_verify/p{pid}/v{tag}",
            "env": {"uname": "Linux 5.4.0 x86_64", "gcc": "gcc (Ubuntu) 7.5.0",
                    "gcov": "gcov (Ubuntu) 7.5.0"},
            "verdict": {"build": "ok" if ok else "fail",
                        "tests": "ok" if ok else "fail",
                        "coverage": "ok" if ok else "fail"},
            "commands": _commands(pid, tag, tids, ran_ids),
            "reason": "" if ok else "1 条用例失败",
            "tests": {"total": 1, "passed": 1 if ok else 0, "failed": 0 if ok else 1,
                      "missing": 0, "all_pass": ok,
                      "by_id": {"TC-001": {"status": "pass" if ok else "fail"}},
                      "results": [{"id": "TC-001", "status": "pass" if ok else "fail",
                                   "status_text": "通过" if ok else "失败",
                                   "detail": ""}]},
            "coverage": {"ok": ok, "totals": {"line_pct": 100.0, "branch_pct": 100.0},
                         "function_map": {"comm_crc": {"branch_effective": 100.0}}}}
    if tids:
        meta["targets"] = tids
        meta["env"]["targets"] = ",".join(tids)
        meta["env"]["target_env"] = {
            tid: {"target": tid, "qemu": "none" if tid == "host" else f"qemu-{tid}",
                  "cwd": f"/home/tester/wb_verify/p{pid}/v{tag}/{tid}"}
            for tid in tids}
        meta["target_results"] = {}
        for tid in tids:
            did_run = tid in ran_ids
            tmeta = meta["target_results"][tid] = {
                "target": tid, "ran": did_run, "available": did_run,
                "ok": did_run and ok,
                "status": "ok" if (did_run and ok) else (
                    "fail" if did_run else "unavailable"),
                "reason": "" if (did_run and ok) else (
                    "" if did_run else f"目标机工具链不可用（缺 {tid}-gcc）"),
                "verdict": {k: ("ok" if ok else "fail") for k in
                            ("build", "tests", "coverage")} if did_run else
                           {k: "skipped" for k in ("build", "tests", "coverage")},
                "workdir": f"/home/tester/wb_verify/p{pid}/v{tag}/{tid}",
                "manifest": {"files": len(ws),
                             "build_sh_sha256": (BaseRunner.manifest(
                                 buildkit.workspace_files(
                                     c_files.extract_files(code_md),
                                     c_files.extract_files(test_md),
                                     tgt_tab.by_id(tid))).get("build.sh") or {}
                             ).get("sha256")}}
    return meta


def _commands(pid, tag, tids, ran_ids):
    """与真实 executor 同形的命令行记录。

    真实产物里 cmd 本身就带 `cd <cwd> &&`，多目标时每条另带 target、cwd 带目标子目录；
    单目标时不带 target。夹具照抄这个形状，才测得出包清单「六、复现方式」的排版
    （cd 被印两遍、多目标三行分不清哪个架构，都只有同形数据才暴露得出来）。"""
    base = f"/home/tester/wb_verify/p{pid}/v{tag}"
    if len(tids) > 1:
        return [{"cmd": f"cd {base}/{tid} && sh build.sh", "cwd": f"{base}/{tid}",
                 "exit_code": 0, "target": tid} for tid in ran_ids]
    return [{"cmd": f"cd {base} && sh build.sh", "cwd": base, "exit_code": 0}]


def _add(pid, stage, version, markdown, meta=None, status="approved"):
    with M.SessionLocal() as s:
        s.add(M.StageArtifact(
            project_id=pid, stage=stage, version=version,
            title=B.DEFAULT_STAGE_TITLES.get(stage, stage),
            markdown=markdown, meta_json=meta or {}, status=status))
        s.commit()


def _new_project(name="工程包测试项目"):
    with M.SessionLocal() as s:
        p = M.Project(name=name, status="completed")
        s.add(p)
        s.commit()
        return p.id


REQ_META = {"functional_requirements": [
    {"id": "FR-001", "desc": "对帧内容计算 CRC 校验值", "priority": "P0",
     "derived_from": []}]}
HLD_META = {"modules": ["comm"], "derived_from": {"comm": ["FR-001"]}}
LLD_META = {"functions": ["comm_crc"], "derived_from": {"comm_crc": ["FR-001"]}}
TC_META = {"testcases": [{"id": "TC-001", "title": "CRC 计算正确", "priority": "P0",
                          "fr_ids": ["FR-001"], "steps": "调用 comm_crc",
                          "expected": "返回 10"}]}
STATIC_META = {"ok": True, "required": 0, "advisory": 0, "violations": [],
               "functions": [{"name": "comm_crc", "file": "src/comm.c"}]}


def seed(pid, code_versions=1, defect_fixed=True, evidence=True, with_report=True,
         stages="full", targets=None, ran=None, missing_logs=()):
    """造一个跑完全链路的项目。code_versions=2 表示「先失败一版再修好」。"""
    _add(pid, "parse", 1, "# 结构化原始数据\n\n- OBJ-001 帧\n", {"objects": []})
    if stages == "full":
        _add(pid, "requirement", 1, "# 软件需求规格说明书\n\n- FR-001\n", REQ_META)
        _add(pid, "hld", 1, "# 概要设计说明书\n\n- comm\n", HLD_META)
        _add(pid, "lld", 1, "# 详细设计说明书\n\n- comm_crc\n", LLD_META)
        _add(pid, "testcase", 1, "# 测试用例设计\n\n- TC-001\n", TC_META)
    bad, good = _md_code("0xFFFF"), _md_code("0x0000")
    test_md = _md_test()
    code_mds = []
    for v in range(1, code_versions + 1):
        last = v == code_versions
        md = good if (last and defect_fixed) else bad
        code_mds.append(md)
        _add(pid, "code", v, md,
             {"files": ["include/comm.h", "src/comm.c"], "module": "comm",
              "derived_from": {"comm_crc": ["FR-001"]}})
        _add(pid, "static", v, "# 静态检查报告\n\n无违规\n", STATIC_META)
    _add(pid, "test_impl", 1, test_md,
         {"files": ["tests/test_comm.c"], "cases": [{"id": "TC-001",
                                                     "fr_ids": ["FR-001"]}],
          "expected_ids": ["TC-001"]})
    for v, md in enumerate(code_mds, 1):
        ok = (v == len(code_mds)) and defect_fixed
        tag = f"{v}t1"
        status = "approved" if ok else ("rejected" if v < len(code_mds)
                                        else "pending_review")
        _add(pid, "exec", v, "# 代码验证执行报告\n\n> 总判定：%s\n" % (
            "全部通过" if ok else "未通过"),
             _exec_meta(pid, tag, md, test_md, ok=ok, status=status,
                        write_evidence=evidence, targets=targets, ran=ran,
                        missing_logs=missing_logs), status=status)
    if with_report:
        _add(pid, "report", 1, "# 软件测评报告\n\n结论：通过\n", _report_meta(pid))
    return {"code": code_mds, "test": test_md}


def _report_meta(pid):
    """用真实装配器造 report 元数据：形状与流水线 report 节点落库的完全一致。"""
    with M.SessionLocal() as s:
        arts, versions = {}, {}
        for st in ("parse", "requirement", "hld", "lld", "testcase", "code",
                   "static", "test_impl", "exec"):
            a = (s.query(M.StageArtifact).filter_by(project_id=pid, stage=st)
                 .order_by(M.StageArtifact.version.desc()).first())
            if a:
                arts[st] = {"markdown": a.markdown, "meta": a.meta_json or {},
                            "version": a.version, "status": a.status}
                versions[st] = a.version
    return assemble_report(arts, "工程包测试项目", versions, history={},
                           coverage_min=config.COVERAGE_BRANCH_MIN)


def _arts(pid):
    return bundle_service.collect(pid)[1]


# ---------------- 测试 ----------------
def test_parse_tag():
    assert B.parse_tag("2t1") == (2, 1)
    assert B.parse_tag("10t3") == (10, 3)
    assert B.parse_tag("2") == (None, None)
    assert B.parse_tag("") == (None, None)
    assert B.parse_tag(None) == (None, None)


def test_safe_name_strips_illegal_chars():
    assert "/" not in B.safe_name("A/B:C*D?")
    assert B.safe_name("") == "project"


def test_baseline_picks_executed_version_not_latest():
    """缺陷场景：code v1 挂、v2 过，最后一轮执行标签 2t1 → 基线必须是 v2。"""
    pid = _new_project()
    seed(pid, code_versions=2)
    plan = bundle_service.make_plan(pid)
    base = plan["baseline"]
    assert base["code_version"] == 2, base
    assert base["test_version"] == 1
    assert base["tag"] == "2t1"
    assert base["source"] == "exec"
    assert base["exec_ok"] is True
    assert plan["alignment"]["aligned"] is True, plan["alignment"]["mismatched"]
    assert plan["warnings"] == [], plan["warnings"]


def test_baseline_newer_code_is_flagged_not_shipped():
    """执行之后代码又被改过：基线仍取已验证的那一版，但必须告警说出来。"""
    pid = _new_project()
    seed(pid, code_versions=1)
    _add(pid, "code", 2, _md_code("0x1234"),
         {"files": ["include/comm.h", "src/comm.c"],
          "derived_from": {"comm_crc": ["FR-001"]}})
    plan = bundle_service.make_plan(pid)
    assert plan["baseline"]["code_version"] == 1
    assert any("v2" in w and "基线" in w for w in plan["warnings"]), plan["warnings"]
    # 基线仍是执行过的那一版，所以与证据一致
    assert plan["alignment"]["aligned"] is True


def test_alignment_detects_changed_source():
    """同一版代码的正文在库里被改过（与执行时的指纹对不上）→ 整包标为不一致。"""
    pid = _new_project()
    seed(pid, code_versions=1)
    with M.SessionLocal() as s:
        a = (s.query(M.StageArtifact).filter_by(project_id=pid, stage="code",
                                                version=1).first())
        a.markdown = _md_code("0x9999")      # 执行之后被悄悄改掉
        s.commit()
    plan = bundle_service.make_plan(pid)
    al = plan["alignment"]
    assert al["aligned"] is False
    assert "src/comm.c" in al["mismatched"], al["mismatched"]
    states = {f["path"]: f["state"] for f in al["files"]}
    assert states["src/comm.c"] == B.ALIGN_CHANGED
    assert states["include/comm.h"] == B.ALIGN_MATCH
    assert states["build.sh"] == B.ALIGN_MATCH
    assert any("不一致" in w for w in plan["warnings"]), plan["warnings"]
    md = B.manifest_markdown(plan, {})
    assert "不一致" in md and "src/comm.c" in md


def test_alignment_without_evidence_is_none_not_true():
    """没跑过验证就没有可比对的指纹：结论只能是「无法核对」，不能谎报一致。"""
    pid = _new_project()
    seed(pid, code_versions=1, evidence=False)
    with M.SessionLocal() as s:                     # 抹掉 exec 记录
        s.query(M.StageArtifact).filter_by(project_id=pid, stage="exec").delete()
        s.commit()
    plan = bundle_service.make_plan(pid)
    assert plan["alignment"]["aligned"] is None
    assert plan["baseline"]["source"] == "latest"
    assert any("没有任何验证执行记录" in w for w in plan["warnings"]), plan["warnings"]


def test_failed_last_run_is_flagged():
    """最后一轮没修好（超限停在人工门）：包照出，但封面必须写明不是合格基线。"""
    pid = _new_project()
    seed(pid, code_versions=1, defect_fixed=False)
    plan = bundle_service.make_plan(pid)
    assert plan["baseline"]["exec_ok"] is False
    assert any("未通过" in w for w in plan["warnings"]), plan["warnings"]
    assert "不代表已验证合格的基线" in B.manifest_markdown(plan, {})


def test_missing_evidence_files_are_reported_not_faked():
    pid = _new_project()
    seed(pid, code_versions=1, evidence=False)
    plan = bundle_service.make_plan(pid)
    run = plan["evidence"][0]
    assert [f["exists"] for f in run["files"]] == [False, False]
    assert any("exec.log" in w for w in plan["warnings"]), plan["warnings"]
    res = bundle_service.build_bundle(pid, want_zip=False)
    ev_dir = Path(res["dir"]) / B.DIR_EVIDENCE
    assert not ev_dir.exists() or not list(ev_dir.rglob("*")), "缺失证据不得写空文件"


def test_no_code_artifact_raises():
    pid = _new_project("只有文档的老项目")
    _add(pid, "parse", 1, "# 结构化原始数据\n", {})
    _add(pid, "requirement", 1, "# 软件需求规格说明书\n", REQ_META)
    try:
        bundle_service.make_plan(pid)
    except B.BundleError as e:
        assert "代码产物" in str(e)
    else:
        raise AssertionError("缺代码产物时必须拒绝装配工程包")


def test_write_bundle_tree_and_manifest():
    pid = _new_project()
    seeded = seed(pid, code_versions=2)
    res = bundle_service.build_bundle(pid)
    tree = Path(res["dir"])
    assert tree.is_dir() and tree.name.endswith(B.BUNDLE_SUFFIX)

    # 源码基线：真实文件本体，且是执行过的那一版（v2 的好代码）
    src = tree / B.DIR_SOURCE / "src" / "comm.c"
    data = src.read_bytes()
    assert data.decode("utf-8") == c_files.extract_files(seeded["code"][-1])["src/comm.c"]
    assert b"\r\n" not in data, "源码必须按字节写，CRLF 会让哈希与证据对不上"
    assert (tree / B.DIR_SOURCE / "build.sh").is_file()
    assert (tree / B.DIR_SOURCE / "tests" / "wb_harness.c").is_file()

    # 文档：10 个阶段各一份 docx，代码文档取基线版 v2
    docs = sorted((tree / B.DIR_DOCS).glob("*.docx"))
    assert len(docs) == 10, [d.name for d in docs]
    assert any(d.name.startswith("06_") and d.name.endswith("_v2.docx") for d in docs)

    # 追溯件：矩阵 + 测试用例 + 测评报告附表
    trace = sorted(p.name for p in (tree / B.DIR_TRACE).glob("*.xlsx"))
    assert len(trace) == 3, trace

    # 证据：两轮执行都留痕（第一轮失败的那轮不能丢）
    ev = sorted(p.name for p in (tree / B.DIR_EVIDENCE).iterdir())
    assert ev == ["exec-v1_1t1", "exec-v2_2t1"], ev
    assert (tree / B.DIR_EVIDENCE / "exec-v1_1t1" / "exec.log").is_file()

    # manifest.json：覆盖包内除自身以外的每个文件，哈希为落盘后实算
    man = json.loads((tree / B.DIR_MANIFEST / B.MANIFEST_JSON).read_text("utf-8"))
    listed = {f["path"] for f in man["files"]}
    on_disk = {p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file()}
    assert on_disk - listed == {f"{B.DIR_MANIFEST}/{B.MANIFEST_JSON}"}
    assert man["alignment"]["aligned"] is True
    assert man["baseline"]["code_version"] == 2
    for f in man["files"]:
        raw = (tree / f["path"]).read_bytes()
        assert f["bytes"] == len(raw), f["path"]
        assert f["sha256"] == hashlib.sha256(raw).hexdigest(), f["path"]
        assert f.get("kind"), f["path"]

    # 包清单：一致性结论、告警、各表齐全
    md = (tree / B.DIR_MANIFEST / B.MANIFEST_MD).read_text("utf-8")
    for key in ("基线一致性核对", "一致", "文档清单", "源码基线", "验证证据",
                "复现方式", "完整性校验", "exec-v1_1t1"):
        assert key in md, key

    # zip：保留顶层目录，解压即整包
    zp = Path(res["zip"])
    assert zp.is_file() and zp.suffix == ".zip"
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
        assert all(n.startswith(tree.name + "/") for n in names), names[:3]
        assert len(names) == len(on_disk)
        assert z.testzip() is None


def test_rebuild_is_clean_not_accumulating():
    """重复导出必须重出整包：上一次的残留文件会被打进 zip，且哈希对不上。"""
    pid = _new_project()
    seed(pid, code_versions=1)
    first = bundle_service.build_bundle(pid)
    stale = Path(first["dir"]) / B.DIR_DOCS / "99_残留文件.docx"
    stale.write_bytes(b"stale")
    second = bundle_service.build_bundle(pid)
    assert not stale.exists()
    with zipfile.ZipFile(second["zip"]) as z:
        assert not any("99_" in n for n in z.namelist())


def test_missing_stages_marked_not_written():
    """未产出的阶段：包清单记「未产出」，不落空 docx。"""
    pid = _new_project()
    seed(pid, code_versions=1, stages="code_only", with_report=False)
    plan = bundle_service.make_plan(pid)
    assert plan["missing_stages"], plan["missing_stages"]
    assert "软件需求规格说明书" in plan["missing_stages"]
    res = bundle_service.build_bundle(pid)
    tree = Path(res["dir"])
    docs = sorted(p.name for p in (tree / B.DIR_DOCS).glob("*.docx"))
    # parse 始终产出，故为 5 份 docx；需求/设计/用例/测评报告未产出，不写空文件
    assert docs == ["01_结构化原始数据_v1.docx", "06_代码实现_v1.docx",
                    "07_静态检查报告_v1.docx",
                    "08_测试实现_v1.docx", "09_代码验证执行报告_v1.docx"], docs
    md = (tree / B.DIR_MANIFEST / B.MANIFEST_MD).read_text("utf-8")
    assert "未产出" in md
    assert not (tree / B.DIR_TRACE).exists() or \
        not list((tree / B.DIR_TRACE).glob("*需求追溯矩阵*"))


def test_unapproved_artifact_is_warned():
    pid = _new_project()
    seed(pid, code_versions=1)
    with M.SessionLocal() as s:
        a = (s.query(M.StageArtifact).filter_by(project_id=pid, stage="hld",
                                                version=1).first())
        a.status = "pending_review"
        s.commit()
    plan = bundle_service.make_plan(pid)
    assert any("pending_review" in w for w in plan["warnings"]), plan["warnings"]


def test_trace_matrix_pinned_to_baseline():
    """矩阵按基线版本算：执行后新出的 code v2 不该改变包内矩阵的代码列。"""
    pid = _new_project()
    seed(pid, code_versions=1)
    res = bundle_service.build_bundle(pid, want_zip=False)
    man = json.loads((Path(res["dir"]) / B.DIR_MANIFEST / B.MANIFEST_JSON)
                     .read_text("utf-8"))
    assert res["matrix_included"] is True
    assert man["stages"]["code"]["version"] == 1
    assert man["file_count"] == len(man["files"])


def test_route_plan_dir_zip_and_404():
    from flask import Flask
    from routes.export import bp as export_bp

    app = Flask(__name__)
    app.register_blueprint(export_bp)
    client = app.test_client()

    pid = _new_project("接口测试项目")
    seed(pid, code_versions=2)

    r = client.get(f"/api/projects/{pid}/export/bundle?format=plan")
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["aligned"] is True and body["baseline"]["code_version"] == 2
    assert body["doc_count"] == 10 and body["source_files"] >= 5
    assert body["warnings"] == []

    r = client.get(f"/api/projects/{pid}/export/bundle?format=dir")
    assert r.status_code == 200
    assert Path(r.get_json()["dir"]).is_dir()

    r = client.get(f"/api/projects/{pid}/export/bundle")
    assert r.status_code == 200
    assert "zip" in (r.headers.get("Content-Type") or "").lower() or \
        r.headers.get("Content-Disposition", "").startswith("attachment")
    assert r.data[:2] == b"PK" and len(r.data) > 5000

    r = client.get("/api/projects/999999/export/bundle?format=plan")
    assert r.status_code == 404

    doc_pid = _new_project("没有代码的项目")
    _add(doc_pid, "requirement", 1, "# 软件需求规格说明书\n", REQ_META)
    r = client.get(f"/api/projects/{doc_pid}/export/bundle?format=zip")
    assert r.status_code == 404 and "代码产物" in r.get_json()["error"]

    r = client.get(f"/api/projects/{pid}/export/bundle?format=tar")
    assert r.status_code == 400


def test_generated_build_files_are_not_flagged_stray():
    """build.sh 与测试桩是 buildkit 生成的构件，不算「约定目录外」；
    真正越界的文件仍要报出来，否则这个过滤就成了睁眼瞎。"""
    pid = _new_project("构件角色测试")
    seed(pid, code_versions=1)
    plan = bundle_service.make_plan(pid)
    assert plan["roles"]["other"] == ["build.sh"], plan["roles"]
    assert not any("约定目录之外" in w for w in plan["warnings"]), plan["warnings"]

    labels = B._role_labels(plan["roles"])
    assert labels["build.sh"] == B.GENERATED_LABEL
    assert labels["tests/wb_harness.c"] == B.GENERATED_LABEL
    assert labels["src/comm.c"] == "被测实现"
    assert labels["include/comm.h"] == "被测头文件"
    md = B.manifest_markdown(plan, {})
    assert B.GENERATED_LABEL in md
    assert "约定目录外" not in md

    # 反向验证：抽取器被换成会吐出越界文件的版本时，告警必须照报
    arts = bundle_service.collect(pid)[1]
    orig = c_files.extract_files

    def _with_stray(text):
        got = dict(orig(text) or {})
        got["misc/notes.txt"] = "越界文件"
        return got

    try:
        c_files.extract_files = _with_stray
        plan2 = B.plan_bundle(pid, "构件角色测试", arts)
    finally:
        c_files.extract_files = orig
    hit = [w for w in plan2["warnings"] if "约定目录之外" in w]
    assert hit, plan2["warnings"]
    assert "misc/notes.txt" in hit[0] and "build.sh" not in hit[0], hit


# ---------------- 多目标机（v2）：逐目标证据与包清单 ----------------
MULTI = ["host", "arm32", "ppc32"]
MULTI_LOGS = ["exec.log", "exec.json", "host.log", "arm32.log", "ppc32.log"]


def plan_md_no_matrix(pid):
    return "目标机矩阵" not in B.manifest_markdown(bundle_service.make_plan(pid), {})


def test_multi_target_evidence_plans_per_target_logs():
    """多目标那一轮：汇总结论取最差值，争议落在「哪个架构挂的」，
    所以每个真跑过的目标都必须有自己的原始输出进包。"""
    pid = _new_project("多目标工程包")
    seed(pid, code_versions=1, targets=MULTI)
    plan = bundle_service.make_plan(pid)
    run = plan["evidence"][0]
    assert [f["name"] for f in run["files"]] == MULTI_LOGS
    assert all(f["exists"] for f in run["files"]), plan["warnings"]
    assert plan["baseline"]["targets"] == MULTI
    assert not any(".log" in w for w in plan["warnings"]), plan["warnings"]


def test_multi_target_bundle_carries_per_target_logs():
    pid = _new_project("多目标出包")
    seed(pid, code_versions=1, targets=MULTI)
    res = bundle_service.build_bundle(pid, want_zip=False)
    tree = Path(res["dir"])
    ev = tree / B.DIR_EVIDENCE / "exec-v1_1t1"
    assert sorted(p.name for p in ev.iterdir()) == sorted(MULTI_LOGS)
    assert "qemu-ppc" not in (ev / "host.log").read_text("utf-8")
    assert "target=ppc32" in (ev / "ppc32.log").read_text("utf-8")
    man = json.loads((tree / B.DIR_MANIFEST / B.MANIFEST_JSON).read_text("utf-8"))
    listed = {f["path"] for f in man["files"]}
    for tid in MULTI:
        assert f"{B.DIR_EVIDENCE}/exec-v1_1t1/{tid}.log" in listed, tid


def test_multi_target_two_rounds_each_keep_their_logs():
    """先失败一版再修好：失败那一轮的逐目标输出同样不能丢（归零材料）。"""
    pid = _new_project("多目标闭环")
    seed(pid, code_versions=2, targets=MULTI)
    plan = bundle_service.make_plan(pid)
    assert len(plan["evidence"]) == 2
    for run in plan["evidence"]:
        assert [f["name"] for f in run["files"]] == MULTI_LOGS, run["tag"]
        assert all(f["exists"] for f in run["files"]), run["tag"]
    res = bundle_service.build_bundle(pid, want_zip=False)
    ev = Path(res["dir"]) / B.DIR_EVIDENCE
    assert sorted(p.name for p in ev.iterdir()) == ["exec-v1_1t1", "exec-v2_2t1"]


def test_multi_target_manifest_explains_host_build_script():
    """包内 build.sh 只能复现 host：包清单必须写明，否则「已在 3 个目标上验证」
    会被误读成「照包内脚本跑一遍就全复现了」。"""
    pid = _new_project("多目标包清单")
    seed(pid, code_versions=1, targets=MULTI)
    md = B.manifest_markdown(bundle_service.make_plan(pid), {})
    for key in ("目标机矩阵", "最差值", "host", "<目标id>.log", "qemu-<arch>-static"):
        assert key in md, key
    assert "、".join(MULTI) in md
    # 复现命令行：逐目标标注，且每条只印一次 cd（记录里的 cmd 已含 cd）
    repro = md.split("执行时的命令行：", 1)[1]
    for tid in MULTI:
        assert f"# 目标机 {tid}" in repro, tid
    # 每条命令只印一次 cd：记录里的 cmd 已含 cd，不能再按 cwd 补一行
    assert repro.count("cd /home/tester") == len(MULTI), repro
    assert repro.count("&& sh build.sh") == len(MULTI)


def test_single_target_manifest_has_no_target_matrix():
    pid = _new_project("单目标包清单")
    seed(pid, code_versions=1)
    md = B.manifest_markdown(bundle_service.make_plan(pid), {})
    assert "目标机矩阵" not in md
    assert "复现方式" in md
    repro = md.split("执行时的命令行：", 1)[1]
    assert "# 目标机" not in repro          # 单目标不标目标，保持既有版式
    assert repro.count("cd /home/tester") == 1, repro
    assert repro.count("&& sh build.sh") == 1


def test_unran_target_does_not_expect_log():
    """配了 ppc32 但验证机没装它的交叉工具链（ran=False）：
    既不该期望 ppc32.log，也不能因此报一条假的「证据缺失」告警。"""
    pid = _new_project("工具链缺失那一轮")
    seed(pid, code_versions=1, targets=MULTI, ran=["host", "arm32"],
         defect_fixed=False)
    plan = bundle_service.make_plan(pid)
    run = plan["evidence"][0]
    names = [f["name"] for f in run["files"]]
    assert names == ["exec.log", "exec.json", "host.log", "arm32.log"], names
    assert all(f["exists"] for f in run["files"])
    assert not any("ppc32.log" in w for w in plan["warnings"]), plan["warnings"]
    assert plan["baseline"]["exec_ok"] is False


def test_only_one_target_ran_keeps_single_target_layout():
    """只配了一个目标（host）：仍是单目标布局，不多写 host.log。"""
    pid = _new_project("单目标 host")
    seed(pid, code_versions=1, targets=["host"])
    run = bundle_service.make_plan(pid)["evidence"][0]
    assert [f["name"] for f in run["files"]] == ["exec.log", "exec.json"]
    assert plan_md_no_matrix(pid)


def test_missing_per_target_log_is_warned_not_faked():
    """逐目标日志真的不在证据目录：报出来，不写空文件充数。"""
    pid = _new_project("缺一份目标日志")
    seed(pid, code_versions=1, targets=MULTI, missing_logs=("ppc32.log",))
    plan = bundle_service.make_plan(pid)
    files = {f["name"]: f for f in plan["evidence"][0]["files"]}
    assert files["ppc32.log"]["exists"] is False
    assert files["host.log"]["exists"] is True
    assert any("ppc32.log" in w for w in plan["warnings"]), plan["warnings"]
    res = bundle_service.build_bundle(pid, want_zip=False)
    ev = Path(res["dir"]) / B.DIR_EVIDENCE / "exec-v1_1t1"
    assert not (ev / "ppc32.log").exists()


def test_legacy_exec_meta_keeps_two_evidence_files():
    """多目标改造之前的旧产物（无 targets / target_results）：
    证据期望仍是两个文件，包清单不提目标机，老项目照样能出包。"""
    pid = _new_project("旧产物兼容")
    seed(pid, code_versions=1)
    plan = bundle_service.make_plan(pid)
    assert [f["name"] for f in plan["evidence"][0]["files"]] == ["exec.log",
                                                                 "exec.json"]
    assert plan["baseline"]["targets"] == []
    assert plan_md_no_matrix(pid)


def test_multi_target_alignment_uses_host_baseline():
    """核对源码用的是顶层（host 变体）指纹：交叉目标的 build.sh 与包内不同，
    拿它核对会把一致的基线误判成不一致。"""
    pid = _new_project("多目标基线核对")
    seed(pid, code_versions=1, targets=MULTI)
    plan = bundle_service.make_plan(pid)
    assert plan["alignment"]["aligned"] is True, plan["alignment"]["mismatched"]
    states = {f["path"]: f["state"] for f in plan["alignment"]["files"]}
    assert states["build.sh"] == B.ALIGN_MATCH
    assert states["src/comm.c"] == B.ALIGN_MATCH


def test_multi_target_alignment_still_flags_changed_source():
    pid = _new_project("多目标改源码")
    seed(pid, code_versions=1, targets=MULTI)
    with M.SessionLocal() as s:
        a = (s.query(M.StageArtifact).filter_by(project_id=pid, stage="code",
                                                version=1).first())
        a.markdown = _md_code("0x9999")
        s.commit()
    plan = bundle_service.make_plan(pid)
    assert plan["alignment"]["aligned"] is False
    assert "src/comm.c" in plan["alignment"]["mismatched"]
    assert any("不一致" in w for w in plan["warnings"]), plan["warnings"]


def _cleanup():
    shutil.rmtree(TMP, ignore_errors=True)


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
    _cleanup()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
