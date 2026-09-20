"""需求追溯：编号规范化、覆盖计算、追溯矩阵。

第一性原理：这条流水线的价值不在「生成了四份文档」，而在「任一需求能正向追到
设计与用例、反向追到原始素材」。本模块只做纯计算（不碰 DB / LLM），
便于离线单测，也让各阶段 validator 与前端矩阵共用同一套规则。

编号约定：
  素材   OBJ-001 / RULE-001 / FLOW-001
  需求   FR-001
LLM 常把 FR-001 写成 fr-1 / FR1，这里统一规范化后再比较，避免假性断链。
"""
import re

_FR_RE = re.compile(r"^\s*FR\s*-?\s*0*(\d+)\s*$", re.IGNORECASE)
_SRC_RE = re.compile(r"^\s*(OBJ|RULE|FLOW)\s*-?\s*0*(\d+)\s*$", re.IGNORECASE)

SRC_PREFIXES = {
    "objects": "OBJ",
    "rules": "RULE",
    "flows": "FLOW",
}


def norm_fr(value) -> str | None:
    """把任意写法的需求编号规范成 FR-001；非法返回 None。"""
    if not isinstance(value, str):
        return None
    m = _FR_RE.match(value)
    return f"FR-{int(m.group(1)):03d}" if m else None


def norm_src(value) -> str | None:
    """把素材编号规范成 OBJ-001 / RULE-002 / FLOW-003；非法返回 None。"""
    if not isinstance(value, str):
        return None
    m = _SRC_RE.match(value)
    return f"{m.group(1).upper()}-{int(m.group(2)):03d}" if m else None


def norm_refs(values, kind: str = "fr") -> list[str]:
    """规范化一组编号：去重、保序、丢弃非法项。kind = fr | src。"""
    fn = norm_fr if kind == "fr" else norm_src
    out: list[str] = []
    if not isinstance(values, (list, tuple, set)):
        values = [values] if values else []
    for v in values:
        n = fn(v)
        if n and n not in out:
            out.append(n)
    return out


def assign_ids(structured: dict) -> dict:
    """给 parse 阶段的 objects/rules/flows 赋稳定编号，并规范 source。

    编号在 reduce 合并完成后统一分配（顺序即数组顺序），保证同一份抽取结果
    每次跑出的编号一致；source 保留 LLM 给出的分块标记（C12 形式）。
    """
    out = dict(structured or {})
    for key, prefix in SRC_PREFIXES.items():
        items = out.get(key) or []
        numbered = []
        for i, item in enumerate(items, 1):
            if not isinstance(item, dict):
                item = {"name": str(item)}
            it = dict(item)
            it["id"] = f"{prefix}-{i:03d}"
            it["source"] = norm_source_tags(it.get("source"))
            numbered.append(it)
        out[key] = numbered
    return out


def norm_source_tags(value) -> list[str]:
    """规范分块来源标记：D1C12 / d1-c12 / C12 / 12 → D1C12 或 C12。"""
    if isinstance(value, str):
        value = [value]
    out: list[str] = []
    if not isinstance(value, (list, tuple)):
        return out
    for v in value:
        if not isinstance(v, (str, int)):
            continue
        m = re.match(r"^\s*(?:D\s*-?\s*0*(\d+)\s*-?\s*)?C?\s*-?\s*0*(\d+)\s*$",
                     str(v), re.IGNORECASE)
        if not m:
            continue
        tag = f"C{int(m.group(2))}"
        if m.group(1):
            tag = f"D{int(m.group(1))}{tag}"
        if tag not in out:
            out.append(tag)
    return out


def collect_frs(srs_meta: dict | None) -> list[dict]:
    """从 SRS 元数据取需求清单：[{id, desc, priority}]，编号已规范化去重。"""
    rows: list[dict] = []
    seen = set()
    frs = (srs_meta or {}).get("functional_requirements") or []
    if not isinstance(frs, list):
        return rows
    for fr in frs:
        if not isinstance(fr, dict):
            continue
        fid = norm_fr(fr.get("id"))
        if not fid or fid in seen:
            continue
        seen.add(fid)
        prio = str(fr.get("priority") or "").strip().upper()
        rows.append({"id": fid,
                     "desc": str(fr.get("desc") or ""),
                     "priority": prio or "-",
                     "derived_from": norm_refs(fr.get("derived_from"), "src")})
    return rows


def critical_ids(srs_meta: dict | None) -> list[str]:
    """P0 需求编号（必须全链路可追溯的那一批）。"""
    return [f["id"] for f in collect_frs(srs_meta) if f["priority"] == "P0"]


def norm_ref_map(raw, valid_keys=None, keep_indirect: bool = True) -> dict[str, list[str]]:
    """规范 HLD/LLD 的 derived_from：{产物元素名: [FR 编号]}。

    - 键去空白；valid_keys 给定时只保留这些键（模型多写的键丢弃，避免脏数据）
    - 值支持字符串或数组，统一规范化为 FR 编号列表
    - keep_indirect=True 时，非 FR 写法（如 LLD 里写模块名）原样保留为
      「间接引用」，由 reverse_index 展开成该模块承接的 FR
    """
    out: dict[str, list[str]] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        name = str(k).strip()
        if not name:
            continue
        if valid_keys is not None and name not in valid_keys:
            continue
        refs = norm_refs(v, "fr")
        if keep_indirect:
            refs += _indirect_refs(v, refs)
        out[name] = refs
    return out


def _indirect_refs(value, already: list[str]) -> list[str]:
    """挑出非 FR 编号的引用（模块名等），保留原样以便后续按模块展开。"""
    items = value if isinstance(value, (list, tuple)) else [value]
    out = []
    for v in items:
        if not isinstance(v, str):
            continue
        s = v.strip()
        if not s or norm_fr(s) or s in already or s in out:
            continue
        out.append(s)
    return out


def reverse_index(ref_map: dict[str, list[str]],
                  module_frs: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """把 {元素: [FR]} 翻成 {FR: [元素]}。

    module_frs 给定时，值为模块名的间接引用会先展开成该模块承接的 FR，
    这样 LLD 只写「属于哪个模块」也能连回需求。
    """
    idx: dict[str, list[str]] = {}
    for elem, refs in (ref_map or {}).items():
        for ref in refs:
            frs = [ref] if norm_fr(ref) else list((module_frs or {}).get(ref, []))
            for fid in frs:
                bucket = idx.setdefault(fid, [])
                if elem not in bucket:
                    bucket.append(elem)
    return idx


def uncovered(required: list[str], index: dict[str, list[str]]) -> list[str]:
    """required 中没有被 index 覆盖的编号（按 required 原序）。"""
    return [r for r in required if r not in index or not index[r]]


def filter_refs(refs, valid_frs=None, valid_modules=None) -> list[str]:
    """丢弃指向不存在元素的引用：FR 编号按 valid_frs 校验，模块名按 valid_modules 校验。

    对应的 valid 集合为空表示无从校验（上游产物缺失），此时原样保留，
    避免因为拿不到清单就把真实引用全删掉。
    """
    fr_set = set(valid_frs or ())
    mod_set = set(valid_modules or ())
    out = []
    for r in refs or []:
        if norm_fr(r):
            if not fr_set or r in fr_set:
                out.append(r)
        elif not mod_set or r in mod_set:
            out.append(r)
    return out


def norm_case_refs(cases) -> list[dict]:
    """规范测试用例的 fr_ids 字段（就地返回新列表，不改入参）。"""
    out = []
    for c in cases or []:
        if not isinstance(c, dict):
            continue
        cc = dict(c)
        cc["fr_ids"] = norm_refs(cc.get("fr_ids"), "fr")
        out.append(cc)
    return out


def build_matrix(srs_meta=None, hld_meta=None, lld_meta=None,
                 tc_meta=None, code_meta=None, static_meta=None,
                 test_impl_meta=None, exec_meta=None, coverage_min=None) -> dict:
    """构建需求追溯矩阵：每条 FR 一行，横向串起素材→设计→用例→代码→验证结论。

    后四个入参（code/static/test_impl/exec）是代码验证闭环上线后新增的，
    全部可选：老项目没有这些产物时对应列一律 None，展示为「未产出」，
    不参与链路缺口判定，也不改变原有四态状态的结论。"""
    frs = collect_frs(srs_meta)
    hld_map = norm_ref_map((hld_meta or {}).get("derived_from"),
                           valid_keys={str(m).strip()
                                       for m in (hld_meta or {}).get("modules") or []},
                           keep_indirect=False)
    hld_by_fr = reverse_index(hld_map)
    lld_map = norm_ref_map((lld_meta or {}).get("derived_from"))
    lld_by_fr = reverse_index(lld_map, hld_map)   # 函数只写模块名时按 HLD 展开
    tc_by_fr: dict[str, list[str]] = {}
    for c in norm_case_refs((tc_meta or {}).get("testcases")):
        for fid in c.get("fr_ids", []):
            tc_by_fr.setdefault(fid, []).append(str(c.get("id") or "-"))

    # 代码单元：代码产物里 derived_from 的键就是详细设计函数名，翻成 FR→函数
    code_by_fr = reverse_index(norm_ref_map((code_meta or {}).get("derived_from"),
                                            keep_indirect=False)) if code_meta else {}
    # 用例→需求：测试实现登记的 cases 才是真跑过的那批，设计里的用例可能没落地
    impl_case_fr: dict[str, list[str]] = {}
    for c in (test_impl_meta or {}).get("cases") or []:
        if isinstance(c, dict) and c.get("id"):
            impl_case_fr[str(c["id"])] = norm_refs(c.get("fr_ids"), "fr")
    exec_by_id = ((exec_meta or {}).get("tests") or {}).get("by_id") or {}
    fn_cov = ((exec_meta or {}).get("coverage") or {}).get("function_map") or {}
    static_funcs = {str(f.get("name")): f
                    for f in (static_meta or {}).get("functions") or []}
    static_by_func: dict[str, int] = {}
    unattributed = 0
    for v in (static_meta or {}).get("violations") or []:
        fn = str((v or {}).get("func") or "")
        if fn:
            static_by_func[fn] = static_by_func.get(fn, 0) + 1
        else:
            unattributed += 1      # 文件级违规挂不到具体需求，只在概览里计数

    rows = []
    for fr in frs:
        fid = fr["id"]
        units = code_by_fr.get(fid, []) if code_meta else None
        row_fns = units or lld_by_fr.get(fid, [])
        exec_result = None
        if test_impl_meta:
            mine = [cid for cid, refs in impl_case_fr.items() if fid in refs]
            if not exec_meta:
                exec_result = "not_run"
            elif not mine:
                exec_result = "no_case"
            else:
                sts = [(exec_by_id.get(cid) or {}).get("status") for cid in mine]
                npass = sum(1 for s in sts if s == "pass")
                exec_result = ("all" if npass == len(mine)
                               else "none" if npass == 0 else "partial")
        covs = [fn_cov[f]["branch_effective"] for f in row_fns
                if f in fn_cov and fn_cov[f].get("branch_effective") is not None]
        rows.append({
            "id": fid,
            "desc": fr["desc"],
            "priority": fr["priority"],
            "sources": fr["derived_from"],
            "hld_modules": hld_by_fr.get(fid, []),
            "lld_functions": lld_by_fr.get(fid, []),
            "testcases": tc_by_fr.get(fid, []),
            # ---- 代码验证闭环新增四列；None 一律表示「该产物未产出」----
            "code_units": units,
            "static_violations": (sum(static_by_func.get(f, 0) for f in row_fns)
                                  if static_meta else None),
            "exec_result": exec_result,
            "branch_coverage": (round(min(covs), 2) if covs else None),
        })

    p0 = [f["id"] for f in frs if f["priority"] == "P0"]
    gaps = {
        "hld": uncovered(p0, hld_by_fr),
        "lld": uncovered(p0, lld_by_fr),
        "testcase": uncovered(p0, tc_by_fr),
    }
    orphan_cases = [str(c.get("id") or "-")
                    for c in norm_case_refs((tc_meta or {}).get("testcases"))
                    if not c.get("fr_ids")]
    summary = {
        "fr_total": len(frs),
        "p0_total": len(p0),
        "covered": {k: len(p0) - len(v) for k, v in gaps.items()},
        "uncovered_p0": gaps,
        "orphan_testcases": orphan_cases,
        "has_hld": bool(hld_meta), "has_lld": bool(lld_meta), "has_tc": bool(tc_meta),
        "has_code": bool(code_meta), "has_static": bool(static_meta),
        "has_test_impl": bool(test_impl_meta), "has_exec": bool(exec_meta),
        "coverage_min": coverage_min,
        "static_unattributed": unattributed,
        "static_functions": len(static_funcs),
        "recorded": recorded_links(srs_meta, hld_meta, lld_meta, tc_meta),
    }
    # 行级链路状态在这里算一次，前端矩阵与 Excel 导出共用同一套口径，
    # 避免两边各写一份判定规则、日久漂移。
    summary["pending_links"] = pending_links(summary)
    for row in rows:
        row["missing"] = row_missing(row, summary)
        row["status"] = row_status(row, summary)
        row["status_text"] = row_status_text(row, summary)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    summary["status_counts"] = counts
    return {"rows": rows, "summary": summary}


def recorded_links(srs_meta=None, hld_meta=None, lld_meta=None,
                   tc_meta=None) -> dict:
    """各环节是否真的『记录过』追溯信息。

    追溯能力上线前生成的旧产物没有这些字段，链路为空是数据缺失，不是设计缺陷，
    必须与「新产物里确实漏标」区分开，否则老项目一打开就是满屏断链。
    判定依据是字段存在与否（新产物即使值为空也会写下 key），而不是值是否非空。
    """
    frs = (srs_meta or {}).get("functional_requirements") or []
    cases = (tc_meta or {}).get("testcases") or []
    return {
        "source": any(isinstance(f, dict) and "derived_from" in f for f in frs),
        "hld": isinstance(hld_meta, dict) and "derived_from" in hld_meta,
        "lld": isinstance(lld_meta, dict) and "derived_from" in lld_meta,
        "testcase": any(isinstance(c, dict) and "fr_ids" in c for c in cases),
    }


# ---------- 行级链路判定 ----------
# 「缺口」和「无从判断」必须分开：追溯能力上线前的旧产物没有关联字段，
# 链路为空是数据缺失而不是设计漏标，混为一谈会让老项目一打开就满屏报红。

LINK_LABELS = {"source": "素材来源", "hld": "概要设计",
               "lld": "详细设计", "testcase": "测试用例"}
DOWNSTREAM = ("hld", "lld", "testcase")
_PRODUCED_FLAG = {"hld": "has_hld", "lld": "has_lld", "testcase": "has_tc"}
_ROW_CELLS = {"hld": "hld_modules", "lld": "lld_functions", "testcase": "testcases"}

ST_OK = "ok"                    # 链路贯通
ST_GAP = "gap"                  # 环节已产出且记录过追溯信息，但这条需求是空的
ST_UNRECORDED = "unrecorded"    # 旧产物没记追溯信息，无从判断
ST_PENDING = "pending"          # 下游阶段还没产出，链路尚未走完
# 验证维度：用例跑挂了 / 覆盖率没到门限。这两个比文档链路缺口更严重——
# 链路缺了顶多是追溯不全，用例挂了说明实现与需求对不上，必须排在最前面。
ST_EXEC_FAIL = "exec_fail"
ST_LOW_COV = "low_cov"

STATUS_TEXT = {ST_OK: "贯通", ST_GAP: "待补全",
               ST_UNRECORDED: "未记录", ST_PENDING: "待生成",
               ST_EXEC_FAIL: "执行失败", ST_LOW_COV: "覆盖不足"}

# 执行结果列的取值与展示文字。None 单独处理为「未产出」。
EXEC_TEXT = {"all": "全部通过", "partial": "部分失败", "none": "全部失败",
             "not_run": "未执行", "no_case": "无关联用例"}


def link_produced(summary: dict, link: str) -> bool:
    """该环节的产物是否已经生成。"""
    return bool((summary or {}).get(_PRODUCED_FLAG.get(link, "")))


def link_recorded(summary: dict, link: str) -> bool:
    """该环节是否真的记录过追溯信息。缺 recorded 字段时按已记录处理。"""
    rec = (summary or {}).get("recorded")
    if not rec:
        return True
    return rec.get(link) is not False


def pending_links(summary: dict) -> list[str]:
    """还没产出的下游环节。"""
    return [k for k in DOWNSTREAM if not link_produced(summary, k)]


def judgeable(summary: dict) -> bool:
    """有没有任何一环是「可判断」的。全片未记录时报贯通是假结论。"""
    if link_recorded(summary, "source") and (summary or {}).get("fr_total"):
        return True
    return any(link_produced(summary, k) and link_recorded(summary, k)
               for k in DOWNSTREAM)


def row_missing(row: dict, summary: dict) -> list[str]:
    """这条需求缺哪几环：只统计已产出且记录了追溯信息的环节。"""
    miss = []
    if link_recorded(summary, "source") and not (row.get("sources") or []):
        miss.append(LINK_LABELS["source"])
    for k in DOWNSTREAM:
        if (link_produced(summary, k) and link_recorded(summary, k)
                and not (row.get(_ROW_CELLS[k]) or [])):
            miss.append(LINK_LABELS[k])
    return miss


def status_of(missing: list, summary: dict) -> str:
    """真断链 > 旧产物无从判断 > 下游还没产出 > 才算贯通。"""
    if missing:
        return ST_GAP
    if not judgeable(summary):
        return ST_UNRECORDED
    if pending_links(summary):
        return ST_PENDING
    return ST_OK


def status_text_of(missing: list, summary: dict) -> str:
    """导出/展示用的纯文字状态（不带图标），缺口写在后面便于在 Excel 里筛。"""
    st = status_of(missing, summary)
    if st == ST_GAP:
        return f"{STATUS_TEXT[st]}：" + "、".join(missing)
    if st == ST_PENDING:
        return (f"{STATUS_TEXT[st]}："
                + "、".join(LINK_LABELS[k] for k in pending_links(summary)))
    return STATUS_TEXT[st]


def row_status(row: dict, summary: dict) -> str:
    """行状态：执行失败 > 覆盖不足 > 文档链路四态。"""
    st = exec_status_of(row, summary)
    return st or status_of(row_missing(row, summary), summary)


def row_status_text(row: dict, summary: dict) -> str:
    st = exec_status_of(row, summary)
    if st == ST_EXEC_FAIL:
        detail = EXEC_TEXT.get(row.get("exec_result"), "")
        return f"{STATUS_TEXT[st]}：{detail}" if detail else STATUS_TEXT[st]
    if st == ST_LOW_COV:
        cmin = (summary or {}).get("coverage_min")
        cov = row.get("branch_coverage")
        tail = f"（最低分支覆盖 {cov:.1f}%，门限 {cmin:.0f}%）" \
            if cov is not None and cmin else ""
        return STATUS_TEXT[st] + tail
    return status_text_of(row_missing(row, summary), summary)


def exec_status_of(row: dict, summary: dict) -> str | None:
    """验证维度的状态；None 表示这一维度还不适用（未产出、未执行或已达标）。"""
    if (row or {}).get("exec_result") in ("partial", "none"):
        return ST_EXEC_FAIL
    cov = (row or {}).get("branch_coverage")
    cmin = (summary or {}).get("coverage_min")
    if cov is not None and cmin and cov < float(cmin):
        return ST_LOW_COV
    return None
