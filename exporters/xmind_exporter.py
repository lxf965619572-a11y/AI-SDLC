"""测试用例导出 XMind（Zen 格式：content.json 打包成 zip，扩展名 .xmind）。
不依赖第三方 xmind 库（其版本老旧），直接生成 XMind 2020+ 兼容文件。
层级：根节点(项目) → 模块 → 用例标题 → 前置条件/测试步骤/预期结果。"""
import json
import uuid
import zipfile


def _node(title: str, children: list | None = None) -> dict:
    node = {
        "id": uuid.uuid4().hex,
        "class": "topic",
        "title": title,
        "structureClass": "org.xmind.ui.map.unbalanced",
    }
    if children:
        node["children"] = {"attached": children}
    return node


def export_xmind(testcases: list[dict], path: str, root_title: str = "测试用例"):
    # 按模块分组
    by_module: dict[str, list] = {}
    for c in testcases:
        by_module.setdefault(c.get("module") or "未分组", []).append(c)

    module_nodes = []
    for module, cases in by_module.items():
        case_nodes = []
        for c in cases:
            steps = " / ".join(c.get("steps", []))
            detail_children = [
                _node(f"前置条件：{c.get('preconditions', '-')}" ),
                _node(f"步骤：{steps or '-'}"),
                _node(f"预期结果：{c.get('expected', '-')}"),
            ]
            badge = c.get("type", "")
            title = f"[{c.get('id','')}] {c.get('title','')}"
            if badge:
                title = f"[{badge}] {title}"
            case_nodes.append(_node(title, detail_children))
        module_nodes.append(_node(f"{module}（{len(cases)}）", case_nodes))

    root = _node(root_title, module_nodes)

    content = [{
        "id": uuid.uuid4().hex,
        "class": "sheet",
        "title": "测试用例",
        "rootTopic": root,
    }]

    manifest = {
        "file-entries": {
            "content.json": {},
            "metadata.json": {},
            "Thumbnails/thumbnail.png": {},
        }
    }
    metadata = {"creator": {"name": "multi-agent-pipeline", "version": "1.0"}}

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.json", json.dumps(content, ensure_ascii=False))
        zf.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False))
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    return path
