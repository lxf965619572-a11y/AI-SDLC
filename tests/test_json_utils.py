"""core.json_utils 输出解析测试。

零第三方依赖：直接 .venv\\Scripts\\python.exe tests\\test_json_utils.py 即可运行；
装了 pytest 时 pytest tests/ 也能收集（函数名以 test_ 开头）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.json_utils import (extract_json, split_markdown_and_meta,
                             strip_code_fence)

# 概要设计的真实形态：元数据块之后还跟着 ```sql DDL
SQL_AFTER_META = (
    "# 概要设计说明书\n\n## 数据库设计\n\n"
    '```json\n{"modules": ["comm", "nav"], "tables": [], "apis": []}\n```\n\n'
    "```sql\nCREATE TABLE t (id INT);\n```\n"
)

# 详细设计的真实形态：元数据块之后还跟着 ```c 代码
C_AFTER_META = (
    "# 详细设计说明书\n\n"
    '```json\n{"data_structures": ["frame_t"], "functions": [{"name": "init"}]}\n```\n\n'
    "```c\nstruct frame_s { int len; };\n```\n"
)

META_IN_MIDDLE = (
    '# 文档\n\n```json\n{"modules": ["a"]}\n```\n\n'
    "```mermaid\ngraph TD; A-->B;\n```\n\n结尾说明。\n"
)

NO_META = "# 文档\n\n```sql\nCREATE TABLE t (id INT);\n```\n"


def test_meta_found_when_sql_block_follows():
    md, meta = split_markdown_and_meta(SQL_AFTER_META)
    assert meta == {"modules": ["comm", "nav"], "tables": [], "apis": []}
    assert "概要设计说明书" in md
    # 非元数据的代码块必须原样留在正文里
    assert "```sql" in md and "CREATE TABLE t" in md


def test_meta_found_when_c_block_follows():
    md, meta = split_markdown_and_meta(C_AFTER_META)
    assert meta["data_structures"] == ["frame_t"]
    assert "```c" in md and "frame_s" in md


def test_meta_block_is_excised_from_markdown():
    md, meta = split_markdown_and_meta(META_IN_MIDDLE)
    assert meta == {"modules": ["a"]}
    assert "modules" not in md
    assert "```mermaid" in md
    assert md.endswith("结尾说明。")


def test_returns_none_when_no_json_meta():
    md, meta = split_markdown_and_meta(NO_META)
    assert meta is None
    assert md == NO_META.strip()


def test_json_array_is_not_accepted_as_meta():
    # validator 一律调 meta.get(...)，数组会炸；宁可判为缺失走重试
    md, meta = split_markdown_and_meta('# d\n```json\n[1, 2]\n```\n')
    assert meta is None


def test_last_json_block_wins():
    text = '# d\n```json\n{"v": 1}\n```\n中间\n```json\n{"v": 2}\n```\n'
    _, meta = split_markdown_and_meta(text)
    assert meta == {"v": 2}


def test_extract_json_tolerates_bare_and_fenced():
    assert extract_json('前言\n```json\n{"objects": []}\n```') == {"objects": []}
    assert extract_json('{"objects": []}') == {"objects": []}
    # 语言标记不再被吞进候选内容
    assert extract_json('```sql\nCREATE TABLE t;\n```\n{"a": 1}') == {"a": 1}


def test_extract_json_repairs_truncated_tail():
    broken = '```json\n{"objects": [{"name": "x"}], "rules": ['
    assert extract_json(broken + "]}") == {"objects": [{"name": "x"}], "rules": []}


def test_strip_code_fence():
    assert strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_code_fence('{"a": 1}') == '{"a": 1}'


def _main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except AssertionError as e:
            failed.append(name)
            print("FAIL  " + name + ": " + str(e))
        except Exception as e:
            failed.append(name)
            print("ERROR " + name + ": " + type(e).__name__ + ": " + str(e))
    print("\n%d/%d passed" % (len(tests) - len(failed), len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
