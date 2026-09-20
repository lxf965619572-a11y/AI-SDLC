"""解析阶段来源标记测试：分块打标、编号分配、来源清洗、旧缓存兼容。

离线运行：打桩 llm_client.chat + 临时缓存目录，不产生任何真实调用。
零第三方依赖：直接 .venv\\Scripts\\python.exe tests\\test_extractor_ids.py 即可运行。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import llm_client
from parsing import extractor

_REAL_CHAT = llm_client.chat
_REAL_CACHE = extractor.CACHE_DIR


def _stub_chat(map_obj=None, reduce_obj=None):
    """按 prompt 类型返回预设 JSON，并记录收到的 prompt。"""
    calls = []

    def chat(messages, **kwargs):
        prompt = "\n".join(m.get("content", "") for m in messages
                           if isinstance(m.get("content"), str))
        calls.append(prompt)
        obj = map_obj if "__MAP__" in prompt else reduce_obj
        return json.dumps(obj if obj is not None else {}, ensure_ascii=False)

    return chat, calls


def _isolated(fn, *args, **kwargs):
    """在临时缓存目录 + 打桩 chat 下运行，结束恢复现场。"""
    chat = kwargs.pop("chat")
    tmp = Path(tempfile.mkdtemp(prefix="wb_extract_"))
    llm_client.chat, extractor.CACHE_DIR = chat, tmp
    try:
        return fn(*args, **kwargs)
    finally:
        llm_client.chat, extractor.CACHE_DIR = _REAL_CHAT, _REAL_CACHE
        for p in tmp.glob("*"):
            p.unlink()
        tmp.rmdir()


def _chunks(n, doc="prd.docx"):
    return [{"seq": i + 1, "heading": f"第{i + 1}节", "content": f"内容{i + 1}", "doc": doc}
            for i in range(n)]


# ---------- 分块打标 ----------

def test_tag_chunks_uses_doc_index_and_seq():
    chunks = _chunks(2, "prd.docx") + _chunks(1, "sow.docx")
    extractor._tag_chunks(chunks)
    assert [c["tag"] for c in chunks] == ["D1C1", "D1C2", "D2C1"]


def test_tag_chunks_falls_back_to_global_index():
    chunks = [{"seq": 5, "content": "a"}, {"seq": 6, "content": "b"}]
    extractor._tag_chunks(chunks)
    assert [c["tag"] for c in chunks] == ["C1", "C2"]


# ---------- 来源清洗 ----------

def test_clean_sources_drops_invented_tags():
    obj = {"objects": [{"name": "o", "source": ["d1c2", "D9C9", 5]}],
           "rules": [{"name": "r"}], "flows": []}
    out = extractor._clean_sources(obj, {"D1C2"})
    assert out["objects"][0]["source"] == ["D1C2"]
    assert out["rules"][0]["source"] == []        # 缺字段也不报错


def test_clean_sources_tolerates_non_dict():
    assert extractor._clean_sources(None, {"D1C1"}) is None


# ---------- 单批抽取：prompt 带编号 + 落盘缓存 ----------

MAP_OUT = {"objects": [{"name": "1553B 总线", "attrs": ["通道"], "desc": "总线控制器",
                        "source": ["D1C1", "D9C9"]}],
           "rules": [{"name": "超时重传", "desc": "重传三次", "source": ["D1C2"]}],
           "flows": []}


def test_extract_batch_puts_tags_in_prompt_and_cleans_sources():
    chat, calls = _stub_chat(map_obj=MAP_OUT)
    batch = _chunks(2)
    extractor._tag_chunks(batch)
    obj, cached = _isolated(extractor._extract_batch, batch, chat=chat)
    assert cached is False
    assert "片段 D1C1" in calls[0] and "片段 D1C2" in calls[0]
    assert obj["objects"][0]["source"] == ["D1C1"]      # D9C9 是编造的，已丢弃
    assert obj["rules"][0]["source"] == ["D1C2"]


def test_extract_batch_second_run_hits_cache():
    chat, calls = _stub_chat(map_obj=MAP_OUT)
    batch = _chunks(2)
    extractor._tag_chunks(batch)

    def run():
        first = extractor._extract_batch(batch)
        second = extractor._extract_batch(batch)
        return first, second

    (obj1, c1), (obj2, c2) = _isolated(run, chat=chat)
    assert (c1, c2) == (False, True)
    assert obj1 == obj2
    assert len(calls) == 1                              # 第二次没有再调 LLM


def test_extract_batch_reuses_legacy_cache_without_tags():
    # 旧格式缓存（片段不带编号）仍应命中，省掉重跑抽取的开销，只是没有来源标记
    chat, calls = _stub_chat(map_obj=MAP_OUT)
    batch = _chunks(1)
    extractor._tag_chunks(batch)
    plain = "--- 片段（标题：第1节）---\n内容1"

    def run():
        legacy = extractor._cache_path(plain)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"objects": [{"name": "旧对象"}],
                                      "rules": [], "flows": []}, ensure_ascii=False),
                          encoding="utf-8")
        return extractor._extract_batch(batch)

    obj, cached = _isolated(run, chat=chat)
    assert cached is True
    assert calls == []                                  # 完全没有调用 LLM
    assert obj["objects"][0]["name"] == "旧对象"
    assert obj["objects"][0]["source"] == []


# ---------- 端到端：编号 + reduce 合并来源 ----------

def test_extract_structured_numbers_single_batch():
    chat, _ = _stub_chat(map_obj=MAP_OUT)
    out = _isolated(extractor.extract_structured, _chunks(3), chat=chat)
    assert [o["id"] for o in out["objects"]] == ["OBJ-001"]
    assert [r["id"] for r in out["rules"]] == ["RULE-001"]
    assert out["flows"] == []
    assert out["objects"][0]["source"] == ["D1C1"]


def test_extract_structured_reduces_and_filters_sources():
    reduce_out = {"objects": [{"name": "1553B 总线", "attrs": ["通道", "波特率"],
                               "desc": "总线控制器", "source": ["D1C1", "D2C1", "D7C7"]}],
                  "rules": [{"name": "超时重传", "desc": "重传三次", "source": ["D1C2"]}],
                  "flows": [{"name": "消息收发", "steps": ["发送", "接收"], "source": ["D2C2"]}]}
    chat, calls = _stub_chat(map_obj=MAP_OUT, reduce_obj=reduce_out)
    chunks = _chunks(extractor.BATCH_SIZE + 1, "prd.docx")   # 两批 → 触发 reduce
    out = _isolated(extractor.extract_structured, chunks, chat=chat)
    assert len(calls) == 3                                # 2 次 map + 1 次 reduce
    assert out["objects"][0]["id"] == "OBJ-001"
    assert out["objects"][0]["source"] == ["D1C1"]        # reduce 编造的来源同样被清洗
    assert out["flows"][0] == {"name": "消息收发", "steps": ["发送", "接收"],
                               "source": [], "id": "FLOW-001"}


# ---------- 渲染 ----------

def test_render_markdown_shows_ids_and_sources():
    structured = {"objects": [{"id": "OBJ-001", "name": "总线", "attrs": ["通道"],
                               "desc": "控制器", "source": ["D1C1"]}],
                  "rules": [{"id": "RULE-001", "name": "重传", "desc": "三次",
                             "source": ["D1C2"]}],
                  "flows": [{"id": "FLOW-001", "name": "收发", "steps": ["发", "收"],
                             "source": []}]}
    md = extractor.render_markdown(structured, ["prd.docx"])
    assert "| 编号 | 对象 | 属性 | 说明 | 来源 |" in md
    assert "| OBJ-001 | 总线 | 通道 | 控制器 | D1C1 |" in md
    assert "| RULE-001 | 重传 | 三次 | D1C2 |" in md
    assert "| FLOW-001 | 收发 | 发 → 收 | - |" in md
    assert "D1C3" not in md


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
