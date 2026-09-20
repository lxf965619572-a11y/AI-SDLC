"""流水线实时事件总线：把后台线程里 LLM 的逐字增量推给浏览器。

为什么需要它：智能体节点跑在 LangGraph 的后台线程，HTTP 请求线程拿不到中间产物，
此前前端只能轮询 /status，必须等整份文档校验通过并落库才看得见（长文档要等几分钟）。
这里用一张进程内的「项目 → 事件通道」表把两端接起来：
生产端（core.llm_client / pipeline.nodes）publish，SSE 端点 subscribe 后转发给浏览器。

设计取舍：
- 只做进程内广播，不跨进程。部署形态是单实例 Flask（app.py 有单实例锁），
  流水线线程与 HTTP 线程同进程，够用；跨进程要引 Redis 或轮询 DB，代价不值。
- 增量按「字符数 / 时间」双阈值合并后才入队。真实模型一份 LLD 有三万多字、
  上万个 token 片段，逐片段发事件会把内存和 SSE 帧数撑爆，合并后约 10 帧/秒，
  肉眼仍是连续打字效果。
- 通道保留各阶段累计全文，订阅方可用 since=0 拿到 snapshot 重放，
  浏览器刷新或断线重连不会丢掉已生成的前半段。
- 阶段绑定用 ContextVar：LangGraph 的同步节点在自己的工作线程里直接调用智能体，
  上下文可见；解析阶段的 ThreadPoolExecutor 子线程不继承上下文，
  天然不会把并发抽取的碎片串进同一个流。

  事件类型（event["type"]）：
    stage_start {stage, started_at}    某阶段开始生成；started_at 为服务端 epoch 秒，
                                       前端刷新后据此显示真实已耗时，而不是从 0 重数
    thinking    {stage, text}          模型的思考增量（推理模型在出正文前先思考，
                                       长文档这段可达数分钟，不推给前端就像卡死）
    delta       {stage, text}          增量文本（已合并）
    reset       {stage}                上一轮输出作废，前端清空重画（校验/网络重试）
    resync      {stages:{stage:text}, thinking:{stage:text},
                 started_at:{stage:ts}}  历史被裁掉时的整段快照重放
    progress    {stage, ...}           非文本型进度（解析阶段的批次计数）
  stage_done  {stage, version, artifact_id}  已落库，前端应改从 DB 拉干净版本
  run_end     {reason}               本轮流水线结束（通道随即关闭）
控制事件（seq 为 None，SSE 端点写成注释行或独立帧，不入事件日志）：
  heartbeat / closed
"""
import contextvars
import threading
import time
from contextlib import contextmanager

# 单个通道最多保留的事件数；超出后裁掉最老的，订阅方凭 min_seq 判断是否需要 resync。
# 按 ~10 事件/秒、单阶段最长 20 分钟估算，8000 条足够覆盖一整轮而不会 OOM。
MAX_EVENTS = 8000
FLUSH_CHARS = 240        # 增量缓冲达到该字符数立即发一帧
FLUSH_SECS = 0.12        # 或距上次发帧超过该秒数（token 来得慢时保证不卡顿）
HEARTBEAT_SECS = 15.0    # 订阅端无事件时的保活间隔
THINK_KEEP = 40000       # 单阶段思考文本最多保留的字符数（只用于重连快照，留尾部）

# 只保护 _registry 的增删（通道创建/销毁），与通道自身的事件锁分离，避免互相阻塞
_reg_cond = threading.Condition()
_registry: "dict[int, Channel]" = {}

# 当前线程正在生成的 (project_id, stage)；llm_client 据此把增量投进对应通道
_current = contextvars.ContextVar("stream_bus_current", default=None)


class Channel:
    """单个项目的事件通道。所有可变状态都在 self.cond 保护下访问。"""

    def __init__(self, project_id: int):
        self.project_id = project_id
        self.cond = threading.Condition()
        self.events: "list[tuple[int, dict]]" = []   # [(seq, event)]
        self.seq = 0
        self.min_seq = 0        # 仍在 events 里的最小 seq 的前一个值（裁剪下界）
        self.accum: "dict[str, str]" = {}   # stage -> 累计全文，供重连快照
        self.think: "dict[str, str]" = {}   # stage -> 累计思考文本，供重连快照
        self.started_at: "dict[str, float]" = {}   # stage -> 阶段开始时刻（epoch 秒）
        self.stage: "str | None" = None     # 最近一次有输出的阶段
        self.closed = False
        self._buf = ""
        self._buf_since = 0.0
        self._tbuf = ""
        self._tbuf_since = 0.0

    # ---- 以下 _ 前缀方法要求调用方已持有 self.cond ----
    def _emit(self, **event) -> int:
        self.seq += 1
        self.events.append((self.seq, event))
        if len(self.events) > MAX_EVENTS:
            self.events = self.events[-MAX_EVENTS:]
            self.min_seq = self.events[0][0] - 1
        self.cond.notify_all()
        return self.seq

    def _flush(self, stage):
        if not self._buf:
            return
        text, self._buf = self._buf, ""
        self._emit(type="delta", stage=stage, text=text)

    def _flush_thinking(self, stage):
        if not self._tbuf:
            return
        text, self._tbuf = self._tbuf, ""
        self._emit(type="thinking", stage=stage, text=text)

    # ---- 对外 API ----
    def feed(self, stage: str, text: str):
        """喂入一段增量文本，按双阈值合并后再入队。"""
        if not text:
            return
        with self.cond:
            if self.closed:
                return
            self.stage = stage
            self.accum[stage] = self.accum.get(stage, "") + text
            now = time.monotonic()
            if not self._buf:
                self._buf_since = now
            self._buf += text
            if len(self._buf) >= FLUSH_CHARS or now - self._buf_since >= FLUSH_SECS:
                self._flush(stage)

    def feed_thinking(self, stage: str, text: str):
        """喂入模型的思考增量。与正文分开缓冲，前端据此显示「思考中」而不是空白。"""
        if not text:
            return
        with self.cond:
            if self.closed:
                return
            self.stage = stage
            acc = self.think.get(stage, "") + text
            self.think[stage] = acc[-THINK_KEEP:] if len(acc) > THINK_KEEP else acc
            now = time.monotonic()
            if not self._tbuf:
                self._tbuf_since = now
            self._tbuf += text
            if len(self._tbuf) >= FLUSH_CHARS or now - self._tbuf_since >= FLUSH_SECS:
                self._flush_thinking(stage)

    def emit(self, **event):
        """发一条结构化事件；发送前先把增量缓冲冲干净，保证前端看到的顺序正确。"""
        with self.cond:
            if self.closed:
                return
            if event.get("stage"):
                self.stage = event["stage"]
            if event.get("type") == "stage_start" and event.get("stage"):
                # 记下服务端开始时刻，重连/刷新时前端能算出真实已耗时
                event = dict(event, started_at=self.started_at.setdefault(
                    event["stage"], time.time()))
            self._flush_thinking(self.stage)
            self._flush(self.stage)
            self._emit(**event)

    def reset_text(self, stage: str):
        """作废该阶段已推送的文本（重试重新生成时用），前端收到 reset 应清空重画。"""
        with self.cond:
            had = bool(self._buf) or bool(self.accum.get(stage)) \
                or bool(self._tbuf) or bool(self.think.get(stage))
            self._buf = ""
            self._tbuf = ""
            self.accum[stage] = ""
            self.think[stage] = ""
            if had and not self.closed:
                self._emit(type="reset", stage=stage)

    def flush(self, stage: "str | None" = None):
        """把缓冲里剩余的增量立即发出去（阶段收尾/通道关闭前调用）。"""
        with self.cond:
            if self._buf and (stage is None or stage == self.stage):
                self._flush(self.stage)
            if self._tbuf and (stage is None or stage == self.stage):
                self._flush_thinking(self.stage)

    def close(self, reason: str = ""):
        with self.cond:
            if not self.closed:
                self._flush_thinking(self.stage)
                self._flush(self.stage)
                self._emit(type="run_end", reason=reason)
                self.closed = True
                self.cond.notify_all()


def open_channel(project_id: int) -> Channel:
    """为一轮流水线执行开通道；同项目已有通道则先关旧的（seq 从头计）。"""
    ch = Channel(project_id)
    with _reg_cond:
        old = _registry.get(project_id)
        _registry[project_id] = ch
        _reg_cond.notify_all()
    if old is not None:
        old.close("replaced")
    return ch


def close_channel(project_id: int, reason: str = ""):
    with _reg_cond:
        ch = _registry.pop(project_id, None)
        _reg_cond.notify_all()
    if ch is not None:
        ch.close(reason)


def get_channel(project_id: int) -> "Channel | None":
    with _reg_cond:
        return _registry.get(project_id)


def has_channel(project_id: int) -> bool:
    with _reg_cond:
        return project_id in _registry


@contextmanager
def bind(project_id: int, stage: str):
    """把 (项目, 阶段) 绑到当前线程上下文，并发出 stage_start。

    期间该线程内所有 llm_client.chat() 的增量都会自动进这个通道，
    智能体代码不需要知道流式推送的存在。"""
    token = _current.set((project_id, stage))
    ch = get_channel(project_id)
    try:
        if ch is not None:
            ch.emit(type="stage_start", stage=stage)
        yield ch
    finally:
        _current.reset(token)
        if ch is not None:
            ch.flush(stage)


def publish_delta(text: str):
    """把一段 LLM 增量文本投进当前绑定的通道；未绑定时静默丢弃。"""
    cur = _current.get()
    if cur is None or not text:
        return
    project_id, stage = cur
    ch = get_channel(project_id)
    if ch is not None:
        ch.feed(stage, text)


def publish_thinking(text: str):
    """把一段模型思考增量投进当前绑定的通道；未绑定时静默丢弃。"""
    cur = _current.get()
    if cur is None or not text:
        return
    project_id, stage = cur
    ch = get_channel(project_id)
    if ch is not None:
        ch.feed_thinking(stage, text)


def reset_attempt():
    """一次新的生成尝试开始：丢弃上一轮已推送的文本。

    llm_client 的网络重试和 run_agent 的输出校验重试都会走到这里，
    否则前端会把失败那次的半截文本和重跑的正文拼在一起。"""
    cur = _current.get()
    if cur is None:
        return
    project_id, stage = cur
    ch = get_channel(project_id)
    if ch is not None:
        ch.reset_text(stage)


def publish(project_id: int, **event):
    """显式发事件（不依赖上下文绑定），供解析阶段这类并发场景报进度。"""
    ch = get_channel(project_id)
    if ch is not None:
        ch.emit(**event)


def subscribe(project_id: int, since: int = 0, timeout: float = HEARTBEAT_SECS,
              wait_channel: bool = True):
    """生成器：yield (seq, event)。seq 为 None 表示控制事件（heartbeat/closed/resync）。

    since 为已收到的最大 seq，只回放其后的事件；传 0 表示从头回放（含累计快照）。
    通道尚未创建时可阻塞等待（wait_channel），便于前端在流水线启动前就挂上连接。
    通道关闭且事件读完后正常结束。"""
    idx = max(0, int(since or 0))
    ch = get_channel(project_id)
    while True:
        if ch is None:
            if not wait_channel:
                return
            with _reg_cond:
                if project_id not in _registry:
                    _reg_cond.wait(timeout)
                ch = _registry.get(project_id)
            if ch is None:
                yield (None, {"type": "heartbeat"})
            continue

        heartbeat = False
        with ch.cond:
            if idx > ch.seq:
                idx = 0        # 通道已换代（新一轮流水线），seq 从头计
            resync = None
            think = {}
            started = {}
            if idx < ch.min_seq:
                # 老事件已被裁掉：整段快照重放，避免前端文本出现缺口
                resync = {s: t for s, t in ch.accum.items() if t}
                think = {s: t for s, t in ch.think.items() if t}
                started = dict(ch.started_at)
                idx = ch.seq
            pending = [(s, e) for s, e in ch.events if s > idx]
            closed = ch.closed
            if not pending and not closed and resync is None:
                if ch.cond.wait(timeout):
                    pending = [(s, e) for s, e in ch.events if s > idx]
                    closed = ch.closed
                else:
                    heartbeat = True

        if resync is not None:
            yield (None, {"type": "resync", "stages": resync, "thinking": think,
                          "started_at": started})
        if heartbeat:
            yield (None, {"type": "heartbeat"})
            continue
        for seq, event in pending:
            idx = seq
            yield (seq, event)
        if closed and not pending:
            yield (None, {"type": "closed"})
            return
