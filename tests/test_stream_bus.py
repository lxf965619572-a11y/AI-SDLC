"""core/stream_bus 单元测试：事件合并、序号回放、跨线程推送、上下文隔离。

零依赖运行：.venv\\Scripts\\python.exe tests\\test_stream_bus.py
也可用 pytest 收集（函数名以 test_ 开头）。
"""
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import stream_bus

# 用高位假项目号，避免与真实项目的通道混淆
PID = 990001


def _deltas(ch):
    return [e["text"] for _, e in ch.events if e["type"] == "delta"]


def _thinks(ch):
    return [e["text"] for _, e in ch.events if e["type"] == "thinking"]

def test_feed_coalesces_by_chars():
    """增量按 FLUSH_CHARS 合并：不足阈值不发帧，够了才发一帧合并文本。"""
    ch = stream_bus.open_channel(PID)
    try:
        small = stream_bus.FLUSH_CHARS // 4
        for _ in range(3):
            ch.feed("hld", "x" * small)
        assert _deltas(ch) == [], "未达字符阈值不应发帧"
        ch.feed("hld", "x" * small)
        assert _deltas(ch) == ["x" * (small * 4)]
    finally:
        stream_bus.close_channel(PID)


def test_feed_coalesces_by_time():
    """token 来得慢时靠时间阈值兜底，保证前端不会长时间看不到字。"""
    ch = stream_bus.open_channel(PID)
    try:
        ch.feed("hld", "ab")
        assert _deltas(ch) == []
        time.sleep(stream_bus.FLUSH_SECS + 0.05)
        ch.feed("hld", "cd")
        assert _deltas(ch) == ["abcd"]
    finally:
        stream_bus.close_channel(PID)


def test_accum_keeps_full_text():
    """累计全文必须等于所有增量拼接（重连快照靠它，不能因合并丢字）。"""
    ch = stream_bus.open_channel(PID)
    try:
        pieces = ["需求", "分析", "abc", "x" * 500, "结束"]
        for p in pieces:
            ch.feed("lld", p)
        ch.flush("lld")
        assert ch.accum["lld"] == "".join(pieces)
        assert "".join(_deltas(ch)) == "".join(pieces)
    finally:
        stream_bus.close_channel(PID)


def test_reset_text_discards_previous_attempt():
    """重试重新生成时作废上一轮文本，并给前端发 reset。"""
    ch = stream_bus.open_channel(PID)
    try:
        ch.feed("hld", "y" * (stream_bus.FLUSH_CHARS + 1))
        assert ch.accum["hld"]
        ch.reset_text("hld")
        assert ch.accum["hld"] == ""
        types = [e["type"] for _, e in ch.events]
        assert types[-1] == "reset" and types[0] == "delta"
    finally:
        stream_bus.close_channel(PID)


def test_bind_routes_publish_delta_to_stage():
    """bind 后 publish_delta 自动落到对应项目+阶段；未绑定时静默丢弃。"""
    ch = stream_bus.open_channel(PID)
    try:
        stream_bus.publish_delta("无人接收")       # 未绑定：不应抛错也不应入队
        assert ch.events == []
        with stream_bus.bind(PID, "requirement"):
            assert [e["type"] for _, e in ch.events] == ["stage_start"]
            stream_bus.publish_delta("z" * (stream_bus.FLUSH_CHARS + 1))
            assert ch.accum["requirement"].startswith("z")
        # 退出 bind 会把缓冲冲干净，不留尾巴
        assert ch._buf == ""
        assert stream_bus._current.get() is None
    finally:
        stream_bus.close_channel(PID)


def test_worker_thread_does_not_inherit_binding():
    """解析阶段的并发抽取线程不继承上下文绑定，碎片不会串进同一个流。"""
    ch = stream_bus.open_channel(PID)
    try:
        with stream_bus.bind(PID, "parse"):
            stream_bus.publish_delta("main")
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(lambda _: stream_bus.publish_delta("worker"), range(3)))
            ch.flush("parse")
        assert ch.accum.get("parse") == "main"
    finally:
        stream_bus.close_channel(PID)


def test_subscribe_since_skips_seen_events():
    """since 之后的事件才回放，断线重连不会重复推送。"""
    ch = stream_bus.open_channel(PID)
    try:
        ch.emit(type="stage_start", stage="hld")
        first = ch.events[0][0]
        ch.feed("hld", "q" * (stream_bus.FLUSH_CHARS + 1))
        sub = stream_bus.subscribe(PID, since=first, timeout=0.2)
        seq, ev = next(sub)
        assert seq > first and ev["type"] == "delta"
        sub.close()
    finally:
        stream_bus.close_channel(PID)


def test_subscribe_stale_seq_restarts():
    """since 大于当前 seq（通道换代）时从头回放，而不是永远等不到事件。"""
    ch = stream_bus.open_channel(PID)
    try:
        ch.emit(type="stage_start", stage="hld")
        sub = stream_bus.subscribe(PID, since=10 ** 6, timeout=0.2)
        seq, ev = next(sub)
        assert seq == ch.events[0][0] and ev["type"] == "stage_start"
        sub.close()
    finally:
        stream_bus.close_channel(PID)


def test_resync_when_history_trimmed():
    """事件被裁剪后改用整段快照重放，前端文本不会出现缺口。"""
    ch = stream_bus.open_channel(PID)
    old_max = stream_bus.MAX_EVENTS
    stream_bus.MAX_EVENTS = 3
    try:
        for i in range(10):
            ch.emit(type="progress", stage="parse", done=i, total=10, pct=i * 10)
            ch.feed("parse", str(i))
        ch.flush("parse")
        assert ch.min_seq > 0, "应已裁掉最老事件"
        sub = stream_bus.subscribe(PID, since=0, timeout=0.2)
        seq, ev = next(sub)
        assert seq is None and ev["type"] == "resync"
        assert ev["stages"]["parse"] == "".join(str(i) for i in range(10))
        sub.close()
    finally:
        stream_bus.MAX_EVENTS = old_max
        stream_bus.close_channel(PID)


def test_cross_thread_stream_until_closed():
    """生产端在后台线程（模拟 LangGraph 节点），订阅端在主线程，全文一字不差。"""
    stream_bus.open_channel(PID)
    received = []

    def produce():
        with stream_bus.bind(PID, "lld"):
            for _ in range(60):
                stream_bus.publish_delta("ab")
                time.sleep(0.002)
        stream_bus.close_channel(PID, "waiting_review")

    t = threading.Thread(target=produce, daemon=True)
    t.start()
    for _seq, ev in stream_bus.subscribe(PID, since=0, timeout=2.0):
        received.append(ev)
        if ev.get("type") == "closed":
            break
    t.join(5)

    text = "".join(e.get("text", "") for e in received if e["type"] == "delta")
    assert text == "ab" * 60, f"收到 {len(text)} 字，应为 120 字"
    types = [e["type"] for e in received]
    assert types[0] == "stage_start"
    assert "run_end" in types and types[-1] == "closed"
    assert received[-2]["reason"] == "waiting_review"


def test_subscribe_without_channel_returns_when_not_waiting():
    """wait_channel=False 且无通道时立即结束（不挂死连接）。"""
    stream_bus.close_channel(PID)
    assert list(stream_bus.subscribe(PID, since=0, timeout=0.1,
                                     wait_channel=False)) == []


def test_thinking_streams_separately_from_content():
    """思考增量走 thinking 事件，不能混进正文 delta（否则文档里会多出思考文本）。"""
    ch = stream_bus.open_channel(PID)
    try:
        ch.feed_thinking("hld", "先理清模块边界" * 40)
        ch.feed("hld", "# 概要设计" * 40)
        assert _thinks(ch) and not any("模块边界" in t for t in _deltas(ch))
        assert ch.think["hld"].startswith("先理清模块边界")
        assert ch.accum["hld"].startswith("# 概要设计")
        assert "先理清" not in ch.accum["hld"]
    finally:
        stream_bus.close_channel(PID)


def test_thinking_coalesces_by_chars():
    """思考增量同样按阈值合并，避免推理期刷屏式发帧。"""
    ch = stream_bus.open_channel(PID)
    try:
        small = stream_bus.FLUSH_CHARS // 4
        for _ in range(3):
            ch.feed_thinking("lld", "y" * small)
        assert _thinks(ch) == [], "未达阈值不应发帧"
        ch.feed_thinking("lld", "y" * small)
        assert _thinks(ch) == ["y" * (small * 4)]
    finally:
        stream_bus.close_channel(PID)


def test_thinking_keeps_only_tail_over_cap():
    """思考快照只留尾部：推理模型的思考可以很长，不能无界占内存。"""
    ch = stream_bus.open_channel(PID)
    old = stream_bus.THINK_KEEP
    stream_bus.THINK_KEEP = 100
    try:
        ch.feed_thinking("hld", "z" * 400)
        assert len(ch.think["hld"]) == 100
    finally:
        stream_bus.THINK_KEEP = old
        stream_bus.close_channel(PID)


def test_reset_clears_thinking_too():
    """重试作废上一轮时，思考文本也要清空，否则会和新正文拼在一起。"""
    ch = stream_bus.open_channel(PID)
    try:
        ch.feed_thinking("hld", "旧思考" * 50)
        ch.feed("hld", "旧正文" * 100)
        ch.reset_text("hld")
        assert ch.think["hld"] == "" and ch.accum["hld"] == ""
        assert [e["type"] for _, e in ch.events][-1] == "reset"
    finally:
        stream_bus.close_channel(PID)


def test_resync_includes_thinking_snapshot():
    """历史被裁掉后重放快照，思考文本也要带上（刷新页面不能丢掉思考期内容）。"""
    ch = stream_bus.open_channel(PID)
    old_max = stream_bus.MAX_EVENTS
    stream_bus.MAX_EVENTS = 3
    try:
        for i in range(10):
            ch.emit(type="progress", stage="hld", message=f"p{i}")
            ch.feed_thinking("hld", str(i))
        ch.flush("hld")
        assert ch.min_seq > 0
        sub = stream_bus.subscribe(PID, since=0, timeout=0.2)
        seq, ev = next(sub)
        assert seq is None and ev["type"] == "resync"
        assert ev["thinking"]["hld"] == "".join(str(i) for i in range(10))
        sub.close()
    finally:
        stream_bus.MAX_EVENTS = old_max
        stream_bus.close_channel(PID)


def test_bind_routes_publish_thinking_to_stage():
    """bind 上下文内 publish_thinking 自动落到对应阶段；未绑定时静默丢弃。"""
    ch = stream_bus.open_channel(PID)
    try:
        stream_bus.publish_thinking("无绑定时应被丢弃")
        assert _thinks(ch) == []
        with stream_bus.bind(PID, "requirement"):
            stream_bus.publish_thinking("思考" * 200)
        assert ch.think["requirement"] == "思考" * 200
        assert "".join(_thinks(ch)) == "思考" * 200
    finally:
        stream_bus.close_channel(PID)


def test_stage_start_carries_started_at():
    """stage_start 带服务端开始时刻，前端刷新后仍能显示真实已耗时。"""
    ch = stream_bus.open_channel(PID)
    try:
        t0 = time.time()
        ch.emit(type="stage_start", stage="hld")
        ev = ch.events[-1][1]
        assert ev["type"] == "stage_start"
        assert t0 - 1 <= ev["started_at"] <= time.time() + 1, ev
        # 同一阶段重复 emit 不刷新开始时刻（断点续跑里 bind 可能重入）
        ch.emit(type="stage_start", stage="hld")
        assert ch.events[-1][1]["started_at"] == ev["started_at"]
    finally:
        stream_bus.close_channel(PID)


def test_resync_includes_started_at():
    """历史被裁掉后重放快照时，开始时刻也要带上，否则刷新后计时从 0 重数。"""
    ch = stream_bus.open_channel(PID)
    old_max = stream_bus.MAX_EVENTS
    stream_bus.MAX_EVENTS = 3
    try:
        ch.emit(type="stage_start", stage="lld")
        started = ch.started_at["lld"]
        for i in range(10):
            ch.emit(type="progress", stage="lld", message=f"p{i}")
            ch.feed("lld", str(i))
        ch.flush("lld")
        assert ch.min_seq > 0
        sub = stream_bus.subscribe(PID, since=0, timeout=0.2)
        seq, ev = next(sub)
        assert seq is None and ev["type"] == "resync"
        assert ev["started_at"]["lld"] == started
        sub.close()
    finally:
        stream_bus.MAX_EVENTS = old_max
        stream_bus.close_channel(PID)


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
