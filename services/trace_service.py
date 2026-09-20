"""需求追溯服务：从各阶段最新产物的元数据拼出「需求 → 设计 → 用例」链路。

计算逻辑全在 core.trace（纯函数、可离线单测），这里只负责取数与补充展示信息。
"""
import config
from core import trace
from db.models import SessionLocal, StageArtifact


def _latest(session, project_id: int, stage: str, version=None):
    """取某阶段产物；给了 version 就锁定该版本（工程包按基线版本取数时用）。"""
    q = session.query(StageArtifact).filter_by(project_id=project_id, stage=stage)
    if version:
        q = q.filter_by(version=int(version))
    art = q.order_by(StageArtifact.version.desc()).first()
    if not art:
        return {}, None
    return (art.meta_json or {}), art.version


def build_project_matrix(project_id: int, pin: dict | None = None) -> dict:
    """返回 {rows, summary, versions, sources}；rows 每条需求一行。

    代码验证闭环的四个产物（code/static/test_impl/exec）取不到时传 None，
    矩阵里对应列显示「未产出」——老项目不会因此报错或误判为断链。

    pin 是 {阶段: 版本号}，用来把矩阵钉在某一版产物上：装配软件工程包时按
    「最后一次执行真正跑过的那一版」计算，否则执行后代码又改过，
    包里的矩阵会去描述一份没被验证过的代码。"""
    pin = pin or {}
    with SessionLocal() as session:
        srs, srs_v = _latest(session, project_id, "requirement", pin.get("requirement"))
        hld, hld_v = _latest(session, project_id, "hld", pin.get("hld"))
        lld, lld_v = _latest(session, project_id, "lld", pin.get("lld"))
        tc, tc_v = _latest(session, project_id, "testcase", pin.get("testcase"))
        code, code_v = _latest(session, project_id, "code", pin.get("code"))
        static, static_v = _latest(session, project_id, "static", pin.get("static"))
        test_impl, ti_v = _latest(session, project_id, "test_impl", pin.get("test_impl"))
        exec_meta, exec_v = _latest(session, project_id, "exec", pin.get("exec"))
        parse_meta, _ = _latest(session, project_id, "parse")

    data = trace.build_matrix(srs, hld, lld, tc,
                              code_meta=code or None,
                              static_meta=static or None,
                              test_impl_meta=test_impl or None,
                              exec_meta=exec_meta or None,
                              coverage_min=config.COVERAGE_BRANCH_MIN)
    data["versions"] = {"requirement": srs_v, "hld": hld_v,
                        "lld": lld_v, "testcase": tc_v,
                        "code": code_v, "static": static_v,
                        "test_impl": ti_v, "exec": exec_v}

    # 素材索引：编号 → 名称/出处，让前端能把 FR 的来源编号翻译成人能读的内容
    sources: dict[str, dict] = {}
    for key, prefix in trace.SRC_PREFIXES.items():
        for it in parse_meta.get(key) or []:
            if isinstance(it, dict) and it.get("id"):
                sources[it["id"]] = {"kind": prefix,
                                     "name": it.get("name", ""),
                                     "chunks": it.get("source") or []}
    data["sources"] = sources
    for row in data["rows"]:
        row["source_names"] = [(sources.get(s) or {}).get("name") or s
                               for s in row["sources"]]
    return data
