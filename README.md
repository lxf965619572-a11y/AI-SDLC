# 多智能体软件开发流水线

基于 **Flask + LangGraph + SQLite** 的六阶段多智能体软件开发流水线系统。上传 PRD / Word / PDF 需求文档后，由多个智能体依次产出结构化原始数据、需求规格说明书、概要设计、详细设计与测试用例，每个阶段均有人工评审门控，支持驳回带意见修订，测试用例可导出 Excel / XMind。

智能体的输出**边生成边推给浏览器**（SSE 逐字增量，含推理模型的思考过程），不必等整份文档校验落库；每条需求带稳定编号，产出后可在**需求追溯矩阵**里横向看到「素材来源 → 需求 → 概要设计 → 详细设计 → 测试用例」是否贯通、哪里有缺口。

## 六阶段流程

```
① 输入与解析(RAG抽取) → ② 需求分析 → ③ 概要设计 → ④ 详细设计 → ⑤ 测试用例 → ⑥ 完成
        ↓                    ↓            ↓            ↓             ↓
     (自动通过)          [人工评审门]  [人工评审门]  [人工评审门]   [人工评审门]
```

- **阶段1 输入与解析**：docx/pdf/md/txt 解析 → 按标题分块（500-800字重叠100）→ jieba+BM25 索引 → LLM map-reduce 抽取业务对象/规则/流程，输出《结构化原始数据》。
- **阶段2 需求分析智能体**：产出《软件需求规格说明书》——功能需求清单、用户故事、业务逻辑、模糊点与风险项。
- **阶段3 概要设计智能体**：产出《概要设计说明书》——模块划分、技术架构（Mermaid graph）、数据库 DDL、核心 API。
- **阶段4 详细设计智能体**：产出《详细设计说明书》——Mermaid 类图、接口定义、Mermaid 序列图。
- **阶段5 测试用例生成智能体**：覆盖功能/边界/异常/场景四维度，字段含编号、前置条件、步骤、预期结果；可导出 Excel / XMind。
- **阶段6 人工评审门控**：每阶段完成后流水线 `interrupt()` 暂停（检查点落 SQLite，跨进程重启不丢），Web 页面渲染 Markdown+Mermaid 评审，通过才进入下一阶段；驳回附意见，智能体带意见重跑并输出修订说明。

## 实时生成与需求追溯

### 实时流式推送（SSE）

长文档在真实模型下要跑几分钟到十几分钟，只靠轮询「已落库产物」的话，用户全程对着空白等。现在：

- 后台线程里 LLM 的每个增量经 `core/stream_bus.py` 的进程内事件总线广播，前端 `GET /api/projects/<id>/stream` 以 SSE 接收，产物卡边写边渲染（`● 实时生成中`）。
- 推理模型出正文前的**思考过程**单独推送并显示，附真实已耗时（服务端 `started_at`，刷新页面不会从 0 重数）。
- 增量按「240 字符 / 0.12 秒」双阈值合并，约 10 帧/秒，肉眼连续但不会把内存和帧数撑爆。
- 断线重连带 `Last-Event-ID`（或 `?since=N`）只补发其后的事件；历史被裁掉时整段快照 `resync`，不丢字不重复。
- 解析阶段是并发批次抽取、没有连续正文，改推批次进度（`第 N/M 批（x%）`）。
- 产物落库后发 `stage_done`，前端丢掉未校验的流式缓冲，改从 DB 拉带 Mermaid 渲染的干净版本。
- **SSE 只是「更快看到」的增强通道**：连接失败、事件丢失或浏览器不支持 `EventSource` 时，原有变速轮询仍会在产物落库后渲染出正确结果。

### 需求追溯矩阵

这条流水线的价值不在「生成了四份文档」，而在「任一需求能正向追到设计与用例、反向追到原始素材」。

- 编号在解析阶段统一分配并全程透传：素材 `OBJ-001` / `RULE-001` / `FLOW-001`，需求 `FR-001`，用例 `TC-001`。LLM 常把 `FR-001` 写成 `fr-1`、`FR1`，`core/trace.py` 统一规范化后再比较，避免假性断链。
- 需求元数据里用 `derived_from` 记素材来源，概设 / 详设用 `derived_from` 记「设计元素 → 覆盖的 FR 编号」，用例用 `fr_ids` 记「本用例验证哪些需求」。`GET /api/projects/<id>/traceability` 据此算出每条需求的覆盖情况、未被任何需求引用的孤儿用例、以及未覆盖的 P0 需求。
- 追溯完整性走 `run_agent(soft_validator=...)`：未达标同样回灌错误让模型修正，但最后一次仍不通过就**接受产物**并把缺口写进 `meta["_warnings"]` 与执行日志，交人工评审裁决，不会因为一个编号没对上就丢掉整份文档。
- 前端「需求追溯」卡支持「只看待补全」过滤；行状态区分 `✓ 贯通` / `⚠ 待补` / `◷ 待生成`（下游阶段还没产出）/ `— 未记录`（旧格式产物无追溯元数据），不会把「还没生成」误报成「已贯通」。

## 快速开始

### 方式一：一键脚本（推荐新环境使用）

```bash
# Windows：
setup.bat          # 自动创建 .venv、安装依赖、生成 .env
# （编辑 .env 配置 LLM，离线演示可不改）
start.bat          # 启动，浏览器打开 http://127.0.0.1:5100

# Linux / macOS：
./setup.sh && ./start.sh
```

### 方式二：手动安装

```bash
# 1. 创建虚拟环境并安装依赖（需要 Python 3.10+）
python -m venv .venv
# Windows:
.venv\Scripts\python.exe -m pip install -r requirements.txt
# Linux/macOS:
# .venv/bin/python -m pip install -r requirements.txt

# 2. 配置 LLM（二选一）
cp .env.example .env   # Windows 用 copy .env.example .env
# 方式A：离线演示模式（默认，无需 key，用内置模板生成产物）—— .env 中 LLM_MOCK=1
# 方式B：真实模型 —— .env 中 LLM_MOCK=0，并填写 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
#        （OpenAI 兼容协议，DeepSeek/通义/火山等均可；支持按角色分别配置）

# 3. 启动
# Windows: .venv\Scripts\python.exe app.py
# Linux/macOS: .venv/bin/python app.py
# 浏览器打开 http://127.0.0.1:5100
```

操作流程：新建项目 → 上传需求文档 → 启动流水线 → 逐阶段评审（通过/驳回+意见）→ 测试用例阶段可导出 Excel/XMind。

## 部署与团队使用

### 换电脑 / 给同事安装

1. `git clone` 本仓库（或直接拷贝代码目录，**不含** `.venv/` 和 `data/`）；
2. 目标机器安装 Python 3.10+（仅 Windows 需要 Git Bash 或直接双击 `.bat`）；
3. 运行 `setup.bat`（Windows）或 `./setup.sh`（Linux/macOS）；
4. 编辑 `.env` 填入自己的 `LLM_API_KEY`（离线演示可跳过），`start.bat` / `./start.sh` 启动。

注意：
- `.env` 含密钥，**已被 git 忽略**，不会随仓库分发，每台机器各自配置；
- `data/`（数据库、上传文件、解析缓存、日志）不进仓库。新机器首次启动会自动创建空库，从零开始建项目；
- 若要把旧机器上的**历史项目数据**带过去：整个拷贝旧机器的 `data/` 目录到新机器同位置即可（含项目、产物、上传的文档、解析缓存）；
- 依赖全部是纯 Python 包（无 C 编译依赖），无需编译器，pip 直接装。

### 局域网共享（让同事通过浏览器访问，不装环境）

在一台常开的机器上部署后，把服务绑定到局域网：`.env` 中改

```
APP_HOST=0.0.0.0
APP_DEBUG=0        # 共享时务必关闭 debug
```

重启后同事用 `http://<服务器IP>:5100` 访问。注意：

- 系统当前**没有登录鉴权**，仅限内网可信环境使用；
- 评审/导出等所有操作都会生效到同一个数据库，多人协作时注意项目归属；
- 如需同时多人并行跑流水线，当前后台线程模型可以支持，但 LLM 并发受 `MAP_CONCURRENCY` 与 API 限流约束；
- **必须单进程部署**（`python app.py`，或 gunicorn/uwsgi 单 worker）：实时事件总线是进程内的，多 worker 下 SSE 订阅可能落到没有流水线线程的那个进程上，实时推送会失效（轮询与产物不受影响）。`app.py` 自带单实例锁。

### 环境变量一览

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_MOCK` | `0` | `=1` 离线演示，不调真实 LLM |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | DeepSeek | OpenAI 兼容端点配置 |
| `LLM_<角色>_*` | — | 按角色覆盖：EXTRACTION / REQUIREMENT / HLD / LLD / TESTCASE |
| `APP_HOST` / `APP_PORT` | `127.0.0.1` / `5100` | 监听地址与端口 |
| `APP_DEBUG` | `1` | 共享部署时置 `0` |
| `MAP_CONCURRENCY` | `8` | 解析阶段 LLM 抽取并发批数 |

## 技术要点

- **编排与门控**：LangGraph `interrupt()` + `SqliteSaver` 检查点；驳回用 `Command(resume={"approved":False,"comments":...})` 经条件边回环重跑当前阶段；进程重启后自动恢复"等待评审"状态。
- **RAG**：python-docx + pdfplumber 解析；jieba + rank_bm25 检索（纯 Python 无 GPU）；map-reduce 抽取。
- **输出健壮性**：统一「Markdown + 末尾 ```json 元数据块」约定，pydantic 风格校验失败自动重试 2 次，含 JSON 代码围栏剥离与平衡括号修复。
- **实时推送**：`core/stream_bus.py` 用「项目 → 事件通道」表把后台线程与 HTTP 线程接起来；阶段绑定用 `ContextVar`，LangGraph 同步节点在自己的工作线程里直接可见，而解析阶段的 `ThreadPoolExecutor` 子线程不继承上下文，天然不会把并发抽取的碎片串进同一个流。
- **需求追溯**：`core/trace.py` 是纯函数（不碰 DB / LLM），各阶段 validator 与前端矩阵共用同一套编号规范与覆盖计算，可离线单测。
- **抽取缓存隔离**：解析缓存 key 带 flavor（`mock` 或真实模型名）。否则先在离线演示模式跑过的文档，之后切回真实模型会直接命中 mock 那份假数据，产物看着完整其实全是编的。
- **导出**：openpyxl 生成带格式 Excel；XMind 手工打包 Zen 格式（content.json + zip），不依赖老旧第三方库。

## 目录结构

```
app.py              Flask 入口（含重启状态恢复）
config.py           .env 配置与按角色 LLM 配置
core/               llm_client(OpenAI兼容) / mock_llm(离线演示) / json_utils(输出清洗)
                    stream_bus(实时事件总线) / trace(追溯编号与覆盖计算)
parsing/            doc_parser / chunker / retriever(BM25) / extractor(map-reduce)
agents/             base_agent(重试校验) / stage_agents(4个智能体)
pipeline/           state / nodes(解析+智能体+门控) / graph(LangGraph 编排)
services/           pipeline_service(后台线程执行/恢复) / trace_service(追溯矩阵取数)
routes/             projects(项目/上传/启动/评审/日志/SSE流/追溯) / export(Excel|XMind)
exporters/          excel_exporter / xmind_exporter
templates/ static/  前端单页（marked + mermaid 本地化渲染；SSE 实时增量 + 变速轮询兜底）
data/               app.sqlite / checkpoints.sqlite / uploads / outputs / logs
scripts/            make_sample_prd.py（生成示例 PRD）
tests/              零依赖测试脚本（run_all.py 一键全跑）
```

## 主要 API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/projects | 新建项目 |
| POST | /api/projects/&lt;id&gt;/upload | 上传需求文档 |
| POST | /api/projects/&lt;id&gt;/start | 启动流水线 |
| GET | /api/projects/&lt;id&gt;/status | 轮询状态 |
| GET | /api/projects/&lt;id&gt;/stages/&lt;stage&gt;/artifact | 查看阶段产物 |
| POST | /api/projects/&lt;id&gt;/stages/&lt;stage&gt;/review | 提交评审（approved+comments） |
| GET | /api/projects/&lt;id&gt;/stream?since=N | SSE 实时事件流（思考/增量/进度/落库） |
| GET | /api/projects/&lt;id&gt;/traceability | 需求追溯矩阵 |
| GET | /api/projects/&lt;id&gt;/export/testcases?format=excel\|xmind | 导出测试用例 |

stage 取值：parse / requirement / hld / lld / testcase

## 测试

测试是**零依赖脚本**（不需要 pytest，装完 `requirements.txt` 就能跑），每个文件自带断言与 `N/N passed` 摘要：

```bash
# Windows:
.venv\Scripts\python.exe tests\run_all.py        # 全部（-v 看逐条用例）
# Linux/macOS:
# .venv/bin/python tests/run_all.py
```

| 文件 | 覆盖内容 |
|---|---|
| `test_stream_bus.py` | 事件合并阈值、快照重放、resync、started_at、通道换代、工作线程不继承绑定 |
| `test_trace.py` | 编号规范化、覆盖计算、孤儿用例、recorded 标记 |
| `test_stage_prompts.py` | 四个智能体的 prompt 约定与 validator（含追溯软校验） |
| `test_extractor_ids.py` | 素材编号分配、分块来源标记（`D1C12`）与抽取缓存复用 |
| `test_json_utils.py` | 代码围栏剥离、平衡括号修复等输出清洗 |
