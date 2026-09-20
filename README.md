# 多智能体软件开发流水线 · 航天嵌入式 AI 自动化代码验证系统

基于 **Flask + LangGraph + SQLite** 的多智能体软件研制流水线。上传 PRD / Word / PDF 需求文档后，由多个智能体按 V 模型依次产出结构化原始数据、需求规格说明书、概要设计、详细设计、测试用例设计、受限子集 C 代码与可执行测试；编译、用例执行、覆盖率采集在远端 Linux 验证机上跑，结论作为**确定性判据**回灌需求追溯矩阵，最终装配成可归档送审的交付件（软件测评报告 / 追溯矩阵 / 问题报告单 / 偏差单）。文档阶段均有人工评审门控，支持驳回带意见修订。

智能体的输出**边生成边推给浏览器**（SSE 逐字增量，含推理模型的思考过程），不必等整份文档校验落库；每条需求带稳定编号，产出后可在**需求追溯矩阵**里横向看到「素材来源 → 需求 → 概要设计 → 详细设计 → 测试用例 → 代码单元 → 静态检查 → 执行结果 → 分支覆盖率」是否贯通、哪里有缺口。

## 流程与门控

```
① 解析 → ② 需求分析 → ③ 概要设计 → ④ 详细设计 → ⑤ 测试用例设计
 (自动)    [评审门]      [评审门]      [评审门]       [评审门]
                                                      ↓
⑩ 测评报告 ← ⑨ 执行验证 ← ⑧ 测试实现 ← ⑦ 静态检查 ← ⑥ 代码实现
 (工具节点)   (工具节点)     [评审门]     (工具节点)     [评审门]
                 │                                       ↑
                 └─ 失败 → 归因（代码缺陷 / 测试缺陷）→ 回对应阶段重生 ─┘
```

- **阶段1 输入与解析**：docx/pdf/md/txt 解析 → 按标题分块（500-800字重叠100）→ jieba+BM25 索引 → LLM map-reduce 抽取业务对象/规则/流程，输出《结构化原始数据》。
- **阶段2 需求分析智能体**：产出《软件需求规格说明书》——功能需求清单、用户故事、业务逻辑、模糊点与风险项。
- **阶段3 概要设计智能体**：产出《概要设计说明书》——模块划分、技术架构（Mermaid graph）、数据库 DDL、核心 API。
- **阶段4 详细设计智能体**：产出《详细设计说明书》——Mermaid 类图、接口定义（函数签名即代码阶段的实现基线）、Mermaid 序列图。
- **阶段5 测试用例设计智能体**：覆盖功能/边界/异常/场景四维度，字段含编号、前置条件、步骤、预期结果；可导出 Excel / XMind。**用例设计排在代码之前**，保证测试独立于实现，不让「照着代码写用例」把判据写成同义反复。
- **阶段6 代码实现智能体**：按详细设计的函数签名产出受限子集 C 代码（`include/` + `src/`），提示词里的编码约束与静态检查规则表同源。硬校验 = 验证机上的编译退出码。
- **阶段7 静态检查（工具节点）**：无 LLM。pycparser 走规则表 + 远端 gcc 兜底，另做「设计覆盖检查」——详细设计的每个函数都必须在代码里存在且签名一致。
- **阶段8 测试实现智能体**：把文字用例翻译成可执行 C 测试（`tests/*.c`，含 `main`）。只有这一阶段允许读代码 API。硬校验 = 测试程序编译链接退出码。
- **阶段9 执行验证（工具节点）**：无 LLM。同步到验证机 → `build.sh`（`gcc -std=c99 -Wall -Wextra` + `-fprofile-arcs -ftest-coverage`）→ 解析用例结果与 `gcov -b -c` 文本 → 出「构建 / 用例 / 覆盖率」三维判定，并把原始日志、输入 sha256、工具链版本归档为证据。
- **阶段10 测评报告（工具节点）**：无 LLM。从库里已落库的产物装配结论，导出 docx / xlsx；失败过的那几轮登记为问题报告单，仍存在的必查项违规登记为偏差单。
- **人工评审门控**：6 个文档阶段完成后流水线 `interrupt()` 暂停（检查点落 SQLite，跨进程重启不丢），Web 页面渲染 Markdown+Mermaid 评审，通过才进入下一阶段；驳回附意见，智能体带意见重跑并输出修订说明。3 个工具节点不设文风评审门，但**超限失败与静态偏差会停在专门的人工裁决门**上。

## 代码生成与验证闭环

AI 生成的代码不能靠「看着像对的」验收。这一段的目标是让每个结论都能回溯到一条工具输出。

### 受限编码子集与规则表

`core/c_rules.py` 是唯一事实来源：每条规则 = `{id, 标题, 标准依据, 检查实现, 严重度, 可否偏差}`，代码生成智能体的提示词与静态检查器都从这张表导出，不会出现「提示词说一套、检查器判另一套」。首批 11 条：

| 规则 | 内容 | 严重度 | 可否偏差 |
|---|---|---|---|
| `WB-C-000` | 源码必须落在受限子集内且可被静态解析 | 必查 | 否 |
| `WB-C-001` | 禁止动态内存分配 | 必查 | 否 |
| `WB-C-002` | 禁止递归 | 必查 | 否 |
| `WB-C-003` | 禁止函数指针 | 必查 | 是 |
| `WB-C-004` | 数组必须静态定长 | 必查 | 是 |
| `WB-C-005` | 函数单出口 | 建议 | 是 |
| `WB-C-006` | 圈复杂度不超过上限（`COMPLEXITY_MAX`） | 必查 | 是 |
| `WB-C-007` | 禁止可写的静态存储变量 | 必查 | 是 |
| `WB-D-001` | 详细设计的每个函数都必须在代码中实现 | 必查 | 否 |
| `WB-D-002` | 实现签名必须与详细设计一致 | 必查 | 是 |
| `WB-D-003` | 代码中不得出现设计外的公开函数 | 建议 | 是 |

标准依据目前写的是 GJB 8114 / QJ 20084 的**标准名**，条款号待标准化部门核定后填进 `standard_ref`；规则 id 与检查行为本期冻结。不可偏差项（动态内存、递归、设计未实现）属安全性底线，人工亦无权放行，只能整改代码。

### 验证执行环境

编译 / 运行 / 覆盖率全部在远端 Linux 验证机上跑（Windows 侧只做编排、AI 生成与证据归档），因为需要可信的 gcc + gcov，且后续要换成 QEMU 目标机模拟。`verification/` 分四层，换执行环境只动 `runner`：

- `runner.py`：只做两件极窄的事——`sync(local, remote)` 送文件、`run(cmd, cwd)` 跑命令，原样带回退出码与 stdout/stderr，不做任何「看起来成功」的美化。`SshRunner` 直接 subprocess 调系统 OpenSSH（文件同步走 tar 管道），零新增 Python 依赖；`LocalRunner` 供本机自测。
- `buildkit.py`：生成固定的 `build.sh` 与测试桩模板。「怎么编译、怎么跑、怎么采覆盖率」不散落在 AI 生成的代码里，于是同一份输入必然走同一条命令序列，证据可比对。
- `parsers.py`：把工具链原始输出解析成可判定结论——`build.sh` 分段标记、用例结果行（`TC-xxx PASS/FAIL 原因`）、`gcov -b -c -f` 传统文本（兼容 gcov 7.5，不依赖 `--json-format`）。解析不出来的显式标 `missing`，绝不静默当成通过。
- `executor.py`：编排一次执行，固定带回四样东西——三维判定、输入指纹（同步前在本机算好的 sha256）、环境指纹（uname / gcc / gcov 版本）、原始输出全文。缺任何一样，结论都不能当交付证据用。

验证机一次性配置：`scripts\setup_verify_vm.ps1` 生成密钥、用密码装公钥（**密码只在这一步用，不进仓库也不进 .env**）、把主机密钥钉进 known_hosts，此后全程免密。工作区为 `~/wb_verify/p<项目>/v<版本>/`，证据落在 `data/verify/p<项目>/v<版本>t<轮次>/`，每轮各自成目录不覆盖历史。未配置验证机时流水线退化为「只静态检查、不实际执行」，不会伪造执行结论。

### 失败闭环与人工裁决

执行失败先过确定性判据（构建失败 / 覆盖率不足 / 设计与实现不一致都能直接定责），定不了的交**归因智能体**读代码与用例，判定「代码缺陷 / 测试缺陷」，再回对应阶段重生：

- 回代码阶段的每一版都要**重新过代码评审门**——AI 可以提出修改，但改动产物一律经人工放行；代码侧修复不重跑测试实现阶段及其门。
- 自动整改最多 `MAX_FIX_ROUNDS` 轮（默认 2）。数满仍不通过就出问题报告单、停在人工裁决门；受理后仍装配交付件，但**测评结论必须是「不通过」**，失败绝不静默放行。
- 必查项静态违规同样先回灌重生 2 轮，剩余违规走偏差单 + 人工批准；不可偏差项无权放行。
- 评审门与自动整改额度的关系有明确语义（`nodes.gate_fix_rounds`）：文档门只在**驳回**时重置额度（驳回等于人工重新给基线），工具门两种裁决都重置。若文档门通过也清零，超限判定就永远触发不了，失败可以靠着「重生 → 过门 → 再失败」无限循环。

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
- 验证闭环给矩阵加了四列：**代码单元**（详细设计的函数是否都在代码里实现）、**静态检查**（与该需求相关函数的违规条数）、**执行结果**（其用例全过 / 部分失败 / 未跑）、**分支覆盖率**（相关函数里的最差值）。这四列是把工具链的确定性结论折回需求维度的唯一通道——它们错了，交付件就会给出与实测相反的结论，比断链更危险，因为断链一眼能看出来，而「明明用例挂了却显示贯通」看不出来。
- 行状态六态，优先级不许漂：**执行失败 > 覆盖不足 > 文档链路四态**（`✓ 贯通` / `⚠ 待补全` / `◷ 待生成` / `— 未记录`）。「未产出（`None`）」与「产出了但这条需求是空的」严格区分，老项目（只有文档阶段产物）不会报红，也不会把「还没生成」误报成「已贯通」。
- 前端「需求追溯」卡支持「只看待补全」过滤；矩阵可一键导出 Excel（`GET /api/projects/<id>/export/traceability`）：Sheet1 逐条需求展开全链路并按状态着色，Sheet2 是覆盖概览（P0 覆盖率、未覆盖清单、孤儿用例、状态分布）。追溯矩阵在航天 / 军工软件研制里本身就是交付件，导出后可直接归档送审。

## 交付件

`services/report_service.py` 是装配逻辑的唯一出口，流水线 report 节点与导出接口共用同一份——否则前端看到的报告和导出的 docx 会各算一套结论，那是交付件里最不能出的错。报告本身不调 LLM、不重新判断，只把已落库的工具结论折叠成人能审、能归档的文档；元数据里没有的一律写「未产出」，绝不用推测补齐。

- **软件测评报告**（`GET /api/projects/<id>/export/report?format=docx|xlsx`）：GJB 438B 风格取够用口径，含测评结论、用例结果、覆盖率、静态违规、需求追溯摘要、问题报告单、偏差单、证据清单。结论保守——静态与执行任一判据缺失或不通过，都不得给出「通过」。
- **需求追溯矩阵**（xlsx）：见上一节。
- **问题报告单 / 偏差单**：闭环过程中失败过的每一轮都登记为问题报告单（含后来修好的，标注已闭环与责任方判定）；最终仍存在的必查项违规登记为偏差单，可偏差项须人工批准，不可偏差项只能整改。
- **证据清单**：每条证据 = 路径 + 字节数 + sha256。sha256 必须与同步输入逐字节对得上，这是「报告所述代码 == 实际执行代码」的唯一凭据。

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

# 3.（可选）配置代码验证执行环境：一台可免密 SSH 的 Linux 机器（gcc + gcov + make + tar）
#    密码只在这一步用于装公钥，之后全程密钥认证
# Windows PowerShell: scripts\setup_verify_vm.ps1
# 然后在 .env 填 VERIFY_HOST / VERIFY_USER / VERIFY_KEY / VERIFY_WORKDIR
# 不配也能跑：流水线退化为「只静态检查、不实际执行」，执行结论标 skipped，不会假绿

# 4. 启动
# Windows: .venv\Scripts\python.exe app.py
# Linux/macOS: .venv/bin/python app.py
# 浏览器打开 http://127.0.0.1:5100
```

操作流程：新建项目 → 上传需求文档 → 启动流水线 → 逐阶段评审（通过/驳回+意见）→ 测试用例阶段可导出 Excel/XMind → 代码与测试实现阶段评审 → 静态检查/执行验证自动出判据 → 测评报告页导出交付件。

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
| `VERIFY_HOST` / `VERIFY_USER` / `VERIFY_PORT` | — / — / `22` | 验证机地址；`VERIFY_HOST` 为空则不实际执行 |
| `VERIFY_KEY` | `~/.ssh/id_ed25519_workbuddy_verify` | 私钥路径（由 setup 脚本生成） |
| `VERIFY_KNOWN_HOSTS` | 系统默认 | 主机密钥钉扎文件 |
| `VERIFY_WORKDIR` | `wb_verify` | 远端工作区根（相对 `$HOME`） |
| `VERIFY_TIMEOUT` | `300` | 单次远端命令超时（秒） |
| `VERIFY_LOCAL` | `0` | `=1` 忽略 `VERIFY_HOST`，用本机工具链（Linux/macOS 自测） |
| `COVERAGE_BRANCH_MIN` | `80` | 分支覆盖率门限，低于该值矩阵标红；`0` 表示不判 |
| `COVERAGE_LINE_MIN` | `0` | 行覆盖率门限 |
| `COMPLEXITY_MAX` | `10` | 圈复杂度上限（规则 `WB-C-006`） |
| `MAX_FIX_ROUNDS` | `2` | 自动整改轮数上限，超出出问题报告单转人工裁决 |

## 技术要点

- **编排与门控**：LangGraph `interrupt()` + `SqliteSaver` 检查点；驳回用 `Command(resume={"approved":False,"comments":...})` 经条件边回环重跑当前阶段；进程重启后自动恢复"等待评审"状态。
- **RAG**：python-docx + pdfplumber 解析；jieba + rank_bm25 检索（纯 Python 无 GPU）；map-reduce 抽取。
- **输出健壮性**：统一「Markdown + 末尾 ```json 元数据块」约定，pydantic 风格校验失败自动重试 2 次，含 JSON 代码围栏剥离与平衡括号修复。
- **实时推送**：`core/stream_bus.py` 用「项目 → 事件通道」表把后台线程与 HTTP 线程接起来；阶段绑定用 `ContextVar`，LangGraph 同步节点在自己的工作线程里直接可见，而解析阶段的 `ThreadPoolExecutor` 子线程不继承上下文，天然不会把并发抽取的碎片串进同一个流。
- **需求追溯**：`core/trace.py` 是纯函数（不碰 DB / LLM），各阶段 validator 与前端矩阵共用同一套编号规范与覆盖计算，可离线单测。
- **抽取缓存隔离**：解析缓存 key 带 flavor（`mock` 或真实模型名）。否则先在离线演示模式跑过的文档，之后切回真实模型会直接命中 mock 那份假数据，产物看着完整其实全是编的。
- **导出**：openpyxl 生成带格式 Excel；XMind 手工打包 Zen 格式（content.json + zip），不依赖老旧第三方库。
- **状态判定单点**：行级链路状态（贯通 / 待补全 / 待生成 / 未记录）由 `core/trace.py` 一次算出并随 `/traceability` 下发，前端与 Excel 导出读同一份结果，不会出现「页面上是绿的、导出的表里是黄的」。
- **规则表单点**：编码子集规则由 `core/c_rules.py` 定义一次，代码生成提示词（`prompt_block`）与静态检查器（`core/c_static.py`）都从这张表导出，规则改了不会两边漂。检查实现用 pycparser（纯 Python C99 AST）+ 远端 gcc 编译兜底，不引 LLVM/clang-tidy；子集外的写法一律判违规，不做兼容处理。
- **判据与证据分离**：`verification/` 四层各司一职（runner 送文件跑命令 / buildkit 生成固定命令序列 / parsers 只认工具真实打印的文本 / executor 出三维判定并归档），都不碰 DB 与 LLM；节点层负责落库与路由。未配置验证机时结论是 `skipped` 而不是 `ok`——离线能走，但假绿不行。
- **归因分层**：能靠确定判据定责的（构建失败 / 覆盖率不足 / 设计实现不一致）由 `executor.auto_decision` 直接判死方向，只有「用例挂了但构建与覆盖率都正常」这类需要读代码权衡的情形才交归因智能体，且其结论照样落在人工评审门后面。

## 目录结构

```
app.py              Flask 入口（含重启状态恢复）
config.py           .env 配置与按角色 LLM 配置
core/               llm_client(OpenAI兼容) / mock_llm(离线演示) / json_utils(输出清洗)
                    stream_bus(实时事件总线) / trace(追溯编号与覆盖计算)
                    c_rules(编码子集规则表) / c_static(静态检查器) / c_files(C 产物抽取渲染)
                    mock_c(离线固定 C 产物：合格样本 + 测试 + 归因结论)
parsing/            doc_parser / chunker / retriever(BM25) / extractor(map-reduce)
agents/             base_agent(重试校验) / stage_agents(需求|概设|详设|用例|代码|测试实现|归因)
pipeline/           state / nodes(解析+智能体+门控+3个工具节点+失败闭环路由) / graph(LangGraph 编排)
verification/       runner(SSH/本机执行环境) / buildkit(构建脚本与测试桩) / parsers(工具输出解析)
                    executor(执行编排：判定+指纹+证据归档)
services/           pipeline_service(后台线程执行/恢复) / trace_service(追溯矩阵取数)
                    report_service(测评报告装配，节点与导出共用)
routes/             projects(项目/上传/启动/评审/日志/SSE流/追溯) / export(用例|文档|追溯矩阵|测评报告)
exporters/          excel_exporter / docx_exporter / xmind_exporter / trace_exporter / report_exporter
templates/ static/  前端单页（marked + mermaid 本地化渲染；SSE 实时增量 + 变速轮询兜底）
data/               app.sqlite / checkpoints.sqlite / uploads / outputs / logs / verify(执行证据)
scripts/            make_sample_prd.py / make_comm_prd.py（示例 PRD）/ setup_verify_vm.ps1（验证机免密）
                    e2e_verify_vm.py（判据层验收）/ e2e_pipeline_mock.py（全链路三场景验收）
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
| GET | /api/projects/&lt;id&gt;/stages/&lt;stage&gt;/export?format=docx\|md | 导出阶段文档 |
| GET | /api/projects/&lt;id&gt;/export/traceability?format=excel | 导出需求追溯矩阵 |
| GET | /api/projects/&lt;id&gt;/export/report?format=docx\|xlsx | 导出软件测评报告（含问题单/偏差单/证据清单） |
| GET | /api/meta | 阶段定义与显示名（前端单一数据源） |

stage 取值（按流水线真实顺序，`static` 夹在 `code` 与 `test_impl` 之间）：parse / requirement / hld / lld / testcase / code / static / test_impl / exec / report

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
| `test_trace.py` | 编号规范化、覆盖计算、孤儿用例、recorded 标记、行级链路状态 |
| `test_trace_export.py` | 追溯矩阵导出：写盘后重新打开校验表头、链路状态、P0 覆盖概览 |
| `test_trace_verify.py` | 验证闭环四列：未产出与空值区分、执行失败 > 覆盖不足 > 文档四态的优先级 |
| `test_stage_prompts.py` | 各智能体的 prompt 约定与 validator（含追溯软校验、编码子集约束） |
| `test_extractor_ids.py` | 素材编号分配、分块来源标记（`D1C12`）与抽取缓存复用 |
| `test_json_utils.py` | 代码围栏剥离、平衡括号修复等输出清洗 |
| `test_c_static.py` | 规则表自洽性 + 每条规则的「命中」与「不误报」双向用例、设计覆盖、反馈渲染 |
| `test_buildkit.py` | 构建脚本/编译探针/测试桩/工作区拼装；路径穿越防护、测试桩不可被生成物覆盖 |
| `test_parsers.py` | build.sh 分段、用例结果行、gcov 文本解析（夹具取自验证机实跑原文） |
| `test_executor.py` | 未配置验证机必须 skipped、三维判定互相独立、归因只在需要读代码时返回 None |
| `test_mock_c.py` | 离线 C 样本过静态门零违规、产物往返抽取一致、归因结论结构 |
| `test_report.py` | 结论保守、证据 sha256 可回溯、问题单/偏差单留痕、docx/xlsx 导出 |
| `test_pipeline_loop.py` | 失败闭环图级路由：额度保留/恢复、超限→人工门、代码侧修复跳过测试门、路由表 ⊆ 图节点 |

每条规则都要有「命中」与「不误报」两个方向的用例：只测命中会把检查器越写越激进，而误报在流水线里表现为一条永远修不好的假失败，比漏报更难查——生成智能体每次都被要求去改一段本来正确的代码。

### 端到端验收（连真实验证机）

单测不连验证机、不调模型；下面两个脚本才验「真跑起来对不对」，都不碰 `data/app.sqlite` 里的真实项目：

```powershell
# 判据层：静态检查 / 编译 / 用例 / 覆盖率这些确定性结论对不对
.venv\Scripts\python.exe scripts\e2e_verify_vm.py

# 编排层：整张图跑通 + 失败闭环，三个场景（可只跑其中一个）
.venv\Scripts\python.exe scripts\e2e_pipeline_mock.py            # green + defect + overlimit
.venv\Scripts\python.exe scripts\e2e_pipeline_mock.py defect     # 只验缺陷闭环
```

`e2e_pipeline_mock.py` 用 `LLM_MOCK=1` 把模型换成 `core/mock_c.py` 的固定产物（可重复、零 token、无需 Web UI），判据层仍打真实验证机：

| 场景 | 验什么 |
|---|---|
| `green` | 6 道评审门 + 3 个工具节点一路全绿到交付件，追溯四列由真实产物填满 |
| `defect` | 注入一处代码缺陷（金卡 95 折算成 90 折，只有 TC-006 判据对不上）：exec 失败 → 归因智能体定责代码缺陷 → 回代码阶段重生 → 再过代码评审门 → 转绿；失败那轮在问题报告单里留痕并标注已闭环 |
| `overlimit` | 每轮都生成同一份坏代码：数满 `MAX_FIX_ROUNDS` 轮后出问题报告单、停在人工裁决门；受理后仍装配交付件，但测评结论必须是「不通过」 |

缺陷之所以选「改坏一个常量表达式」：编译照过、静态照过、分支结构不变（覆盖率不动），纯粹是判据对不上，正好落在「确定性判据不足以定责、必须读代码与用例」的归因智能体分支上，也是航天软件里最典型的「实现与设计常量不一致」。
