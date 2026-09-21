"""软件工程包装配与导出：文档 + 源码基线 + 追溯件 + 验证证据 一整包。

第一性原理：交付物不是一个「结论」，而是一份能独立复核的工程包——拿到包的人
不访问本系统也能回答三个问题：交付了哪些文件、每个文件是哪一版、结论由哪一轮
执行支撑。所以本模块只做三件事：

  1. 选基线：源码基线取「最后一次验证执行真正跑过的那一版」，不取库里最新版。
     交付的是被证据支撑的代码；执行之后又改过的代码不能悄悄混进包里。
  2. 核对：把基线工作区重算 sha256，与该轮执行证据里的 manifest 逐文件比对，
     对不上就在包清单里显式标出来。哈希是「跑的就是这份代码」的唯一凭据，
     宁可整包标红，不可静默放行。
  3. 折叠：文档、追溯件、证据全部取已落库的产物重新装配，不调 LLM、不新造结论；
     未产出的阶段不写空文件占位，只在包清单里记「未产出」。

本模块不读数据库（取数在 services.bundle_service），因此可离线单测。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from core import c_files
from exporters.docx_exporter import export_docx
from exporters.excel_exporter import export_excel
from exporters.report_exporter import export_report_excel
from exporters.trace_exporter import export_trace_excel
from verification import buildkit
from verification.runner import BaseRunner

BUNDLE_FORMAT_VERSION = 1
BUNDLE_SUFFIX = "软件工程包"

# 包内目录：编号即阅读顺序，先清单、再文档、再源码、再追溯、最后原始证据
DIR_MANIFEST = "00_包清单"
DIR_DOCS = "01_文档"
DIR_SOURCE = "02_源码基线"
DIR_TRACE = "03_追溯"
DIR_EVIDENCE = "04_证据"

MANIFEST_MD = "包清单.md"
MANIFEST_JSON = "manifest.json"

# 文档阶段顺序与标题的兜底值；调用方（服务层）会传入流水线的权威顺序
DEFAULT_STAGE_ORDER = ("parse", "requirement", "hld", "lld", "testcase",
                       "code", "static", "test_impl", "exec", "report")
DEFAULT_STAGE_TITLES = {
    "parse": "结构化原始数据", "requirement": "软件需求规格说明书",
    "hld": "概要设计说明书", "lld": "详细设计说明书",
    "testcase": "测试用例设计", "code": "代码实现",
    "static": "静态检查报告", "test_impl": "测试实现",
    "exec": "代码验证执行报告", "report": "软件测评报告",
}

# 源码基线随执行标签走，文档也跟着基线版本走，保证「包里的文档描述包里的代码」
BASELINE_STAGES = ("code", "test_impl")

# buildkit 生成的固定构件：构建脚本与结果解析桩。它们是复现验证所必需的文件，
# 不属于「模型写到约定目录之外」的可疑产物，角色与告警都要单独对待。
GENERATED_FILES = frozenset({"build.sh", *buildkit.HARNESS_FILES})
GENERATED_LABEL = "验证机构件（buildkit 固定模板）"
_ROLE_TEXT = {"src": "被测实现", "include": "被测头文件",
              "tests": "可执行测试", "other": "约定目录外"}

ALIGN_MATCH = "match"                      # 与执行证据逐字节一致
ALIGN_CHANGED = "changed"                  # 同一路径内容不同
ALIGN_ONLY_BASELINE = "only_in_baseline"   # 基线里有、执行时没同步
ALIGN_ONLY_EVIDENCE = "only_in_evidence"   # 执行时同步过、基线里没有
ALIGN_TEXT = {ALIGN_MATCH: "一致", ALIGN_CHANGED: "不一致",
              ALIGN_ONLY_BASELINE: "执行证据中无此文件",
              ALIGN_ONLY_EVIDENCE: "基线中缺此文件"}


class BundleError(Exception):
    """装配不出工程包（缺代码产物、项目不存在等）。路由层转成 4xx。"""


def safe_name(name: str) -> str:
    """去掉文件系统非法字符：包名会同时用作目录名与 zip 名。"""
    return re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(name or "")).strip()[:60] or "project"


def parse_tag(tag) -> tuple:
    """执行标签 "2t1" → (代码版本 2, 测试实现版本 1)；解析不出返回 (None, None)。"""
    m = re.fullmatch(r"\s*(\d+)\s*t\s*(\d+)\s*", str(tag or ""))
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def _role_labels(roles: dict) -> dict:
    """{路径: 角色名}，供包清单「源码基线」表使用；生成构件单独标注。"""
    out = {}
    for role, paths in (roles or {}).items():
        for p in paths or []:
            out[p] = (GENERATED_LABEL if p in GENERATED_FILES
                      else _ROLE_TEXT.get(role, role))
    return out


# ---------------- 取产物 ----------------
def _versions(arts: dict, stage: str) -> list:
    rows = [a for a in (arts or {}).get(stage) or [] if isinstance(a, dict)]
    return sorted(rows, key=lambda a: int(a.get("version") or 0))


def pick(arts: dict, stage: str, version=None) -> dict | None:
    """取指定版本的产物；version 为空或找不到时取最新版。"""
    rows = _versions(arts, stage)
    if not rows:
        return None
    if version is not None:
        for a in rows:
            if int(a.get("version") or 0) == int(version):
                return a
    return rows[-1]


# ---------------- 基线选择 ----------------
def select_baseline(arts: dict) -> dict:
    """选定源码基线：优先取最后一次执行真正跑过的那一版代码与测试。

    为什么不取库里最新版：证据（用例结果、覆盖率、输入指纹）只对执行时那一版
    成立。执行之后代码又被改过时，最新版是「未经证实的代码」，把它当基线交付
    等于把结论套到别的代码上——这是交付件里最不能出的错。"""
    execs = _versions(arts, "exec")
    codes = {int(a["version"]): a for a in _versions(arts, "code")}
    tests = {int(a["version"]): a for a in _versions(arts, "test_impl")}
    base = {"tag": "", "exec_version": None, "exec_status": "", "exec_ok": None,
            "exec_skipped": False, "code_version": None, "test_version": None,
            "source": "", "reason": "", "manifest": {}, "evidence": "", "env": {},
            "verdict": {}, "commands": [], "targets": [], "warnings": []}
    warn = base["warnings"]

    last = execs[-1] if execs else None
    if last:
        meta = last.get("meta") or {}
        tag = str(meta.get("version") or "")
        cv, tv = parse_tag(tag)
        base.update({"tag": tag, "exec_version": int(last["version"]),
                     "exec_status": last.get("status") or "",
                     "exec_ok": bool(meta.get("ok")),
                     "exec_skipped": bool(meta.get("skipped")),
                     "manifest": meta.get("manifest") or {},
                     "evidence": str(meta.get("evidence") or ""),
                     "env": meta.get("env") or {},
                     "verdict": meta.get("verdict") or {},
                     "commands": meta.get("commands") or [],
                     "targets": meta.get("targets") or []})
        if cv in codes:
            base["code_version"] = cv
            base["test_version"] = tv if tv in tests else None
            base["source"] = "exec"
            base["reason"] = (f"取最后一次验证执行（exec v{base['exec_version']}"
                              f"，标签 {tag or '-'}）实际跑过的版本")
            if tv is not None and tv not in tests:
                warn.append(f"执行标签指向测试实现 v{tv}，但库里没有这一版，"
                            "测试文件改用最新版，与执行证据的对应关系可能不成立")
        else:
            warn.append(f"执行标签 {tag or '-'} 指向的代码版本不在库里"
                        f"（现有：{', '.join(f'v{v}' for v in sorted(codes)) or '无'}），"
                        "源码基线退回库内最新版，与执行证据无法核对")
    else:
        warn.append("没有任何验证执行记录：包内源码未经编译/运行/覆盖率证实，"
                    "不能作为已验证基线交付")

    if base["code_version"] is None:
        latest = pick(arts, "code")
        if latest:
            base["code_version"] = int(latest["version"])
            base["source"] = base["source"] or "latest"
            base["reason"] = base["reason"] or "无对应执行记录，取库内最新代码版本"
    if base["test_version"] is None:
        latest = pick(arts, "test_impl")
        if latest:
            base["test_version"] = int(latest["version"])

    # 执行之后又改过代码/测试：包仍以已验证版本为基线，但必须说出来
    for stage, key in (("code", "code_version"), ("test_impl", "test_version")):
        rows = _versions(arts, stage)
        if not rows or base[key] is None:
            continue
        newest = int(rows[-1]["version"])
        if newest != int(base[key]):
            warn.append(f"库内最新「{DEFAULT_STAGE_TITLES[stage]}」为 v{newest}，"
                        f"本包基线为 v{base[key]}（与最后一次执行对应）。"
                        "执行后产物又被修改过：新版本未经验证，如需交付请重跑验证")
    return base


# ---------------- 基线与证据的逐字节核对 ----------------
def _manifest_entry(value):
    """证据里的 manifest 值：新版是 {sha256, bytes}，早期产物只存了哈希字符串。"""
    if isinstance(value, dict):
        return {"sha256": str(value.get("sha256") or ""),
                "bytes": value.get("bytes")}
    return {"sha256": str(value or ""), "bytes": None}


def align(workspace: dict, evidence_manifest: dict | None) -> dict:
    """把基线工作区与执行证据的输入指纹逐文件比对。

    返回 aligned：True 全一致 / False 有出入 / None 没有证据可比。
    哈希一律在本模块重算（与 runner.sync 同一算法：utf-8 字节的 sha256），
    不复用产物里可能过期的数字。"""
    mine = BaseRunner.manifest(workspace or {})
    theirs = {str(k).replace("\\", "/"): _manifest_entry(v)
              for k, v in (evidence_manifest or {}).items()}
    files = []
    for path in sorted(set(mine) | set(theirs)):
        a, b = mine.get(path), theirs.get(path)
        if a and b:
            state = ALIGN_MATCH if a["sha256"] == b["sha256"] else ALIGN_CHANGED
        elif a:
            state = ALIGN_ONLY_BASELINE
        else:
            state = ALIGN_ONLY_EVIDENCE
        files.append({"path": path, "state": state, "state_text": ALIGN_TEXT[state],
                      "bytes": (a or {}).get("bytes"), "sha256": (a or {}).get("sha256"),
                      "evidence_bytes": (b or {}).get("bytes") if b else None,
                      "evidence_sha256": (b or {}).get("sha256") if b else None})
    aligned = None if not theirs else all(f["state"] == ALIGN_MATCH for f in files)
    return {"aligned": aligned, "files": files,
            "mismatched": [f["path"] for f in files if f["state"] != ALIGN_MATCH],
            "reason": "" if aligned is not False else
            "基线源码与执行证据的输入指纹不一致"}


# ---------------- 装配计划 ----------------
def plan_bundle(project_id: int, project_name: str, arts: dict,
                stage_order=None, stage_titles=None, evidence_root=None,
                thresholds: dict | None = None) -> dict:
    """算出整包的内容与结论（不落盘）。写盘在 write_bundle。"""
    stage_order = tuple(stage_order or DEFAULT_STAGE_ORDER)
    titles = {**DEFAULT_STAGE_TITLES, **(stage_titles or {})}
    base = select_baseline(arts)
    warnings = list(base.pop("warnings"))

    code_art = pick(arts, "code", base["code_version"])
    test_art = pick(arts, "test_impl", base["test_version"])
    if not code_art:
        raise BundleError("尚未生成代码产物，无法装配软件工程包")
    code_files = c_files.extract_files(code_art.get("markdown") or "")
    test_files = c_files.extract_files((test_art or {}).get("markdown") or "")
    if not code_files:
        raise BundleError("代码产物里没有解析出任何源文件，无法装配源码基线")
    workspace = buildkit.workspace_files(code_files, test_files)
    roles = buildkit.split_by_role(workspace)

    alignment = align(workspace, base.get("manifest"))
    if alignment["aligned"] is False:
        warnings.append("源码基线与执行证据不一致：" +
                        "、".join(alignment["mismatched"][:10]) +
                        ("…" if len(alignment["mismatched"]) > 10 else ""))
    if alignment["aligned"] is None and base.get("exec_version"):
        warnings.append("执行记录里没有输入指纹（manifest 为空），无法核对基线一致性")

    # 执行结论没通过 / 没跑起来：包照出，但封面必须写清它不是合格基线
    if base.get("exec_version"):
        if base.get("exec_skipped"):
            warnings.append(f"最后一次验证执行（exec v{base['exec_version']}）未跑起来，"
                            "包内源码没有真实执行结论支撑")
        elif not base.get("exec_ok"):
            v = base.get("verdict") or {}
            warnings.append(f"最后一次验证执行（exec v{base['exec_version']}）未通过"
                            f"（构建 {v.get('build', '-')} · 用例 {v.get('tests', '-')} · "
                            f"覆盖率 {v.get('coverage', '-')}），"
                            "本包不代表已验证合格的基线")

    docs = []
    for i, stage in enumerate(stage_order, 1):
        pinned = base.get("code_version") if stage == "code" else (
            base.get("test_version") if stage == "test_impl" else None)
        art = pick(arts, stage, pinned) if stage in BASELINE_STAGES else pick(arts, stage)
        title = titles.get(stage, stage)
        entry = {"index": i, "stage": stage, "title": title, "produced": bool(art),
                 "filename": "", "version": None, "status": "", "markdown": "",
                 "meta": {}, "baseline": stage in BASELINE_STAGES}
        if art:
            entry.update({"filename": f"{i:02d}_{safe_name(title)}_v{art['version']}.docx",
                          "version": int(art["version"]),
                          "status": art.get("status") or "",
                          "markdown": art.get("markdown") or "",
                          "meta": art.get("meta") or {}})
            if entry["status"] != "approved":
                warnings.append(f"「{title}」v{entry['version']} 评审状态为 "
                                f"{entry['status'] or '未知'}（未通过评审门），已随包归档")
        docs.append(entry)

    missing = [d["title"] for d in docs if not d["produced"]]
    if missing:
        warnings.append("以下阶段未产出，包内不写空文件：" + "、".join(missing))

    illegal = c_files.illegal_paths({**code_files, **test_files})
    if illegal:
        warnings.append("源文件不在约定目录（build.sh 不会编译它们）：" + "、".join(illegal))
    # build.sh 由 buildkit 生成、天然落在约定目录之外，不是告警对象
    other = sorted(p for p in (roles.get("other") or {}) if p not in GENERATED_FILES)
    if other:
        warnings.append("源码基线含约定目录之外的文件：" + "、".join(other))

    cases = ((pick(arts, "testcase") or {}).get("meta") or {}).get("testcases") or []
    report_art = pick(arts, "report")
    evidence = _plan_evidence(arts, project_id, evidence_root, warnings)

    name = safe_name(project_name)
    return {
        "format_version": BUNDLE_FORMAT_VERSION,
        "project_id": int(project_id),
        "project_name": str(project_name or ""),
        "bundle_name": f"{name}_{BUNDLE_SUFFIX}",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "baseline": base,
        "workspace": workspace,
        "roles": {k: sorted(v) for k, v in roles.items()},
        "alignment": alignment,
        "docs": docs,
        "missing_stages": missing,
        "testcases": cases,
        "report_meta": (report_art or {}).get("meta") or {},
        "report_version": (report_art or {}).get("version"),
        "evidence": evidence,
        "thresholds": thresholds or {},
        "warnings": warnings,
    }


def _plan_evidence(arts: dict, project_id: int, evidence_root, warnings: list) -> list:
    """每一轮验证执行的原始证据：目录取自产物 meta，缺失则按约定路径推。

    失败过的那几轮也要进包——归零材料里最关键的正是问题清单与处置过程，
    只留最后一轮成功记录等于把过程抹掉。"""
    out = []
    root = Path(evidence_root) if evidence_root else None
    for art in _versions(arts, "exec"):
        meta = art.get("meta") or {}
        tag = str(meta.get("version") or "") or f"v{art['version']}"
        recorded = str(meta.get("evidence") or "").replace("\\", "/")
        src_dir = Path(recorded).parent if recorded else (
            root / f"p{project_id}" / f"v{tag}" if root else None)
        files = []
        # 多目标机时每个跑过的目标另有一份原始输出，同样必须进包：汇总结论取的是
        # 各目标最差值，争议往往落在「到底是哪个架构上挂的」。期望文件从产物 meta 的
        # target_results 推，旧产物没有该字段，自然退化成 exec.log + exec.json。
        ran = [tid for tid, t in (meta.get("target_results") or {}).items()
               if (t or {}).get("ran")]
        names = ["exec.log", "exec.json"] + (
            [f"{tid}.log" for tid in ran] if len(ran) > 1 else [])
        for fname in names:
            src = (src_dir / fname) if src_dir else None
            exists = bool(src and src.is_file())
            files.append({"name": fname, "src": str(src) if src else "",
                          "exists": exists})
            if not exists:
                warnings.append(f"exec v{art['version']}（{tag}）的 {fname} 不在证据目录，"
                                f"包内不含该文件：{src or '证据目录未知'}")
        out.append({"tag": tag, "exec_version": int(art["version"]),
                    "status": art.get("status") or "", "ok": bool(meta.get("ok")),
                    "skipped": bool(meta.get("skipped")),
                    "reason": str(meta.get("reason") or ""),
                    "dirname": f"exec-v{art['version']}_{safe_name(tag)}",
                    "files": files})
    return out


# ---------------- 写盘 ----------------
def _sha_file(path: Path) -> dict:
    data = path.read_bytes()
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _walk(tree: Path, skip_dirs=()) -> list:
    out = []
    for p in sorted(tree.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(tree).as_posix()
        if any(rel.startswith(s) for s in skip_dirs):
            continue
        out.append(rel)
    return out


def _clean(tree: Path, root: Path) -> None:
    """重出包前清掉上一次的目录：残留旧文件会被一起打进 zip，且哈希对不上。"""
    if not tree.exists():
        return
    resolved, allowed = tree.resolve(), Path(root).resolve()
    if not str(resolved).startswith(str(allowed)):
        raise BundleError(f"拒绝清理工程包目录之外的路径：{resolved}")
    shutil.rmtree(resolved, ignore_errors=True)


def write_bundle(plan: dict, out_root, matrix: dict | None = None) -> dict:
    """把计划写成一棵目录树，返回 {dir, files, aligned, warnings}。"""
    root = Path(out_root)
    root.mkdir(parents=True, exist_ok=True)
    tree = root / plan["bundle_name"]
    _clean(tree, root)
    origin: dict[str, dict] = {}      # 相对路径 → 来源（阶段/版本/类别），写进 manifest.json

    def note(rel: str, kind: str, **kw):
        origin[rel.replace("\\", "/")] = {"kind": kind, **kw}

    # 01_文档：各阶段 docx（代码与测试实现取基线版本，与包内源码同版）
    for d in plan["docs"]:
        if not d["produced"]:
            continue
        path = tree / DIR_DOCS / d["filename"]
        path.parent.mkdir(parents=True, exist_ok=True)
        export_docx(d["markdown"], str(path), title=d["title"])
        note(f"{DIR_DOCS}/{d['filename']}", "文档", stage=d["stage"],
             version=d["version"], status=d["status"], title=d["title"])

    # 02_源码基线：真实源文件 + 构建脚本，按字节写（LF 不变），哈希才对得上证据
    for rel, content in sorted((plan.get("workspace") or {}).items()):
        path = buildkit.normalize_path(rel)
        if not path:
            continue
        target = tree / DIR_SOURCE / Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(str(content).encode("utf-8"))
        note(f"{DIR_SOURCE}/{path}", "源码基线",
             stage="test_impl" if path.startswith("tests/") else "code",
             version=(plan["baseline"].get("test_version") if path.startswith("tests/")
                      else plan["baseline"].get("code_version")),
             generated=path in GENERATED_FILES)

    # 03_追溯：追溯矩阵（服务层按基线版本算好传进来）+ 测试用例 + 测评报告附表
    if matrix and matrix.get("rows"):
        fname = f"{safe_name(plan['project_name'])}_需求追溯矩阵.xlsx"
        (tree / DIR_TRACE).mkdir(parents=True, exist_ok=True)
        export_trace_excel(matrix, str(tree / DIR_TRACE / fname),
                           project_name=plan["project_name"])
        note(f"{DIR_TRACE}/{fname}", "追溯件", stage="traceability")
    if plan.get("testcases"):
        fname = f"{safe_name(plan['project_name'])}_测试用例.xlsx"
        (tree / DIR_TRACE).mkdir(parents=True, exist_ok=True)
        export_excel(plan["testcases"], str(tree / DIR_TRACE / fname),
                     sheet_title="测试用例")
        note(f"{DIR_TRACE}/{fname}", "追溯件", stage="testcase")
    if plan.get("report_meta"):
        fname = (f"{safe_name(plan['project_name'])}_软件测评报告附表"
                 f"_v{plan.get('report_version') or 1}.xlsx")
        (tree / DIR_TRACE).mkdir(parents=True, exist_ok=True)
        export_report_excel(plan["report_meta"], str(tree / DIR_TRACE / fname))
        note(f"{DIR_TRACE}/{fname}", "追溯件", stage="report",
             version=plan.get("report_version"))

    # 04_证据：每轮执行的原始日志与结论 JSON，原样复制不做改写
    for run in plan.get("evidence") or []:
        for f in run["files"]:
            if not f["exists"]:
                continue
            dst = tree / DIR_EVIDENCE / run["dirname"] / f["name"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f["src"], dst)
            note(f"{DIR_EVIDENCE}/{run['dirname']}/{f['name']}", "验证证据",
                 stage="exec", version=run["exec_version"], tag=run["tag"])

    # 00_包清单：先算完所有已写文件的哈希，再写清单（清单里要印这些哈希）
    hashed = {rel: _sha_file(tree / rel) for rel in _walk(tree)}
    (tree / DIR_MANIFEST).mkdir(parents=True, exist_ok=True)
    (tree / DIR_MANIFEST / MANIFEST_MD).write_text(
        manifest_markdown(plan, hashed), encoding="utf-8", newline="\n")

    files = []
    for rel in _walk(tree, skip_dirs=(f"{DIR_MANIFEST}/{MANIFEST_JSON}",)):
        info = hashed.get(rel) or _sha_file(tree / rel)
        files.append({"path": rel, **info, **(origin.get(rel) or {"kind": "包清单"})})
    manifest = {
        "bundle": BUNDLE_SUFFIX, "format_version": plan["format_version"],
        "project_id": plan["project_id"], "project_name": plan["project_name"],
        "generated_at": plan["generated_at"],
        "baseline": {k: v for k, v in plan["baseline"].items() if k != "manifest"},
        "baseline_manifest": plan["baseline"].get("manifest") or {},
        "alignment": plan["alignment"],
        "stages": {d["stage"]: {"title": d["title"], "produced": d["produced"],
                                "version": d["version"], "status": d["status"],
                                "file": (f"{DIR_DOCS}/{d['filename']}"
                                         if d["produced"] else "")}
                   for d in plan["docs"]},
        "evidence_runs": [{k: v for k, v in r.items()} for r in plan.get("evidence") or []],
        "thresholds": plan.get("thresholds") or {},
        "environment": plan["baseline"].get("env") or {},
        "warnings": plan["warnings"],
        "file_count": len(files),
        "files": files,
        "note": ("manifest.json 自身的哈希不在表内（无法自包含）；"
                 "其余每个文件的 sha256 均为落盘后实算。"),
    }
    (tree / DIR_MANIFEST / MANIFEST_JSON).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8", newline="\n")

    return {"dir": str(tree), "name": plan["bundle_name"], "files": files,
            "file_count": len(files), "aligned": plan["alignment"]["aligned"],
            "mismatched": plan["alignment"]["mismatched"],
            "warnings": plan["warnings"], "baseline": plan["baseline"],
            "manifest": str(tree / DIR_MANIFEST / MANIFEST_JSON)}


def zip_bundle(tree_dir, zip_path=None) -> str:
    """打成 zip：包内保留顶层目录，解压即是完整工程包。"""
    tree = Path(tree_dir)
    dest = Path(zip_path) if zip_path else tree.parent / f"{tree.name}.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in _walk(tree):
            z.write(tree / rel, f"{tree.name}/{rel}")
    return str(dest)


# ---------------- 包清单（人读的那一份） ----------------
def _repro_cmd(c: dict) -> str:
    """一条可照抄复现的命令行（多目标时标出是哪个目标）。

    真实记录里的 cmd 本身已经是 `cd <cwd> && sh build.sh`，再按 cwd 补一行 cd
    就会把同一个 cd 印两遍；多目标时三条命令长得一模一样，不标目标就看不出
    哪一行复现哪个架构。退出码一并印出来，复核时能对上证据里的结论。"""
    cmd = str(c.get("cmd") or "").strip() or "sh build.sh"
    cwd = str(c.get("cwd") or "").strip()
    if cwd and not cmd.startswith("cd "):
        cmd = f"cd {cwd} && {cmd}"
    tid = str(c.get("target") or "").strip()
    head = f"# 目标机 {tid}\n" if tid else ""
    code = c.get("exit_code")
    tail = "" if code is None else f"\n# 当时退出码 {code}"
    return head + cmd + tail


def _short(sha) -> str:
    return f"{str(sha or '')[:16]}…" if sha else "-"


def _table(headers: list, rows: list) -> list:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out


def manifest_markdown(plan: dict, hashed: dict) -> str:
    """包清单：把「包里有什么、各是哪一版、结论由哪轮执行支撑、对不对得上」写清。"""
    base = plan["baseline"]
    align_info = plan["alignment"]
    th = plan.get("thresholds") or {}
    env = base.get("env") or {}
    aligned = align_info["aligned"]
    verdict_text = {True: "通过", False: "未通过", None: "未执行"}.get(
        base.get("exec_ok") if base.get("exec_version") else None, "未执行")

    L = [f"# {plan['bundle_name']} · 包清单", "",
         f"- 项目：{plan['project_name']}（ID {plan['project_id']}）",
         f"- 生成时间：{plan['generated_at']}",
         f"- 包格式版本：v{plan['format_version']}",
         f"- 源码基线：代码 v{base.get('code_version') or '-'} + "
         f"测试实现 v{base.get('test_version') or '-'}"
         f"（执行标签 {base.get('tag') or '-'}）",
         f"- 基线来源：{base.get('reason') or '-'}",
         f"- 最后一次验证执行：exec v{base.get('exec_version') or '-'} "
         f"{verdict_text}（评审状态 {base.get('exec_status') or '-'}）",
         f"- 执行环境：{env.get('uname', '-')} · {env.get('gcc', '-')} · "
         f"{env.get('gcov', '-')}",
         f"- 门限：分支覆盖 {th.get('branch_min', '-')}% · "
         f"自动修复上限 {th.get('max_fix_rounds', '-')} 轮",
         ""]

    L += ["## 一、基线一致性核对", ""]
    if aligned is True:
        L += ["> **一致**：源码基线每个文件的 sha256 与最后一次验证执行的输入指纹"
              "逐字节相符，包内结论确实由包内源码支撑。", ""]
    elif aligned is False:
        L += ["> **不一致（不得作为已验证基线交付）**：以下文件与执行证据的输入指纹"
              "对不上，说明执行之后代码又变过，或证据与本包不是同一批产物。", ""]
    else:
        L += ["> **无法核对**：没有可比对的执行证据（未跑验证，或证据里没有输入指纹）。"
              "包内源码未经编译/运行/覆盖率证实。", ""]
    rows = [[f["path"], f["bytes"] if f["bytes"] is not None else "-",
             _short(f["sha256"]), _short(f["evidence_sha256"]),
             ("**" + f["state_text"] + "**") if f["state"] != ALIGN_MATCH
             else f["state_text"]]
            for f in align_info["files"]]
    if rows:
        L += _table(["文件", "字节", "基线 sha256", "证据 sha256", "核对"], rows) + [""]

    L += ["## 二、告警与未产出项", ""]
    if plan["warnings"]:
        L += [f"{i}. ⚠️ {w}" for i, w in enumerate(plan["warnings"], 1)] + [""]
    else:
        L += ["> 无。基线与执行证据一致，各阶段产物均已通过评审门。", ""]

    L += ["## 三、文档清单", ""]
    rows = []
    for d in plan["docs"]:
        if d["produced"]:
            info = hashed.get(f"{DIR_DOCS}/{d['filename']}") or {}
            rows.append([f"{d['index']:02d}", d["title"], f"v{d['version']}",
                         d["status"] or "-", f"{DIR_DOCS}/{d['filename']}",
                         info.get("bytes", "-"), _short(info.get("sha256"))])
        else:
            rows.append([f"{d['index']:02d}", d["title"], "-", "-", "**未产出**",
                         "-", "-"])
    L += _table(["序号", "阶段", "版本", "评审", "文件", "字节", "sha256"], rows) + [""]

    L += ["## 四、源码基线", "",
          f"> 目录 `{DIR_SOURCE}/` 就是同步到验证机的工作区本体："
          "执行 `sh build.sh` 即可复现编译、跑用例、采覆盖率。", ""]
    role_of = _role_labels(plan.get("roles") or {})
    rows = []
    for f in align_info["files"]:
        p = f["path"]
        rows.append([p, role_of.get(p, "-"), f["bytes"] if f["bytes"] is not None
                     else "-", _short(f["sha256"])])
    L += _table(["路径", "角色", "字节", "sha256"], rows) + [""]

    L += ["## 五、验证证据", ""]
    if plan.get("evidence"):
        rows = []
        for run in plan["evidence"]:
            got = [f["name"] for f in run["files"] if f["exists"]]
            want = [f["name"] for f in run["files"]]
            state = "、".join(got) if got else "**缺失**"
            if len(got) != len(want):
                state += "（缺 " + "、".join(x for x in want if x not in got) + "）"
            rows.append([f"exec v{run['exec_version']}", run["tag"],
                         "通过" if run["ok"] else ("未执行" if run["skipped"] else "未通过"),
                         run["status"] or "-",
                         f"{DIR_EVIDENCE}/{run['dirname']}/", state,
                         str(run["reason"] or "-").replace("|", "/")[:60]])
        L += _table(["轮次", "执行标签", "判定", "评审", "包内目录", "文件", "结论"],
                    rows) + [""]
    else:
        L += ["> **未产出**：没有任何验证执行记录。", ""]

    L += ["## 六、复现方式", "",
          f"1. 解压后进入 `{DIR_SOURCE}/`；",
          "2. 在 Linux（gcc + gcov）下执行 `sh build.sh`；",
          "3. 输出中 `WB_SECTION run` 是用例结果（`TC-xxx PASS/FAIL`），"
          "`WB_SECTION coverage` 是 gcov 覆盖率；",
          "4. 与 `04_证据/` 里对应轮次的 `exec.log` 比对即可复核结论。"]
    tgt_ids = (plan.get("baseline") or {}).get("targets") or []
    if len(tgt_ids) > 1:
        # 说清楚包内 build.sh 只能复现哪一个目标：否则「已在 N 个目标上验证」
        # 的结论会被误当成「照包内脚本跑一遍就能全部复现」。
        L += ["",
              f"> **目标机矩阵**：本轮结论来自 {len(tgt_ids)} 个目标机"
              f"（{'、'.join(str(x) for x in tgt_ids)}），汇总取各目标最差值。",
              f"> 包内 `{DIR_SOURCE}/build.sh` 是验证机本机（host）变体，"
              "按上述步骤只复现 host 目标；其余目标需用对应交叉编译器与 "
              "`qemu-<arch>-static` 重新生成构建脚本"
              "（`verification/buildkit.py` 的 `build_script(target)`），"
              f"各目标原始输出见 `{DIR_EVIDENCE}/` 内的 `<目标id>.log`。"]
    L.append("")
    cmds = (base.get("commands") or [])
    if cmds:
        L += ["执行时的命令行：", ""]
        L += [f"```sh\n{_repro_cmd(c)}\n```" for c in cmds]
        L += [""]

    L += ["## 七、完整性校验", "",
          f"> 包内每个文件的 sha256 与字节数见 `{DIR_MANIFEST}/{MANIFEST_JSON}`"
          "（落盘后实算，manifest.json 自身除外）。", "",
          "> 本包由 WorkBuddy 自动折叠库内已落库产物装配，未调用大模型、"
          "未新增任何结论；每个文件的来源阶段与版本都记在 manifest.json 的 files 表里。",
          ""]
    return "\n".join(L).rstrip() + "\n"
