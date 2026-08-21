# 多智能体软件开发流水线

基于 **Flask + LangGraph + SQLite** 的六阶段多智能体软件开发流水线系统。上传 PRD / Word / PDF 需求文档后，由多个智能体依次产出结构化原始数据、需求规格说明书、概要设计、详细设计与测试用例，每个阶段均有人工评审门控，支持驳回带意见修订，测试用例可导出 Excel / XMind。

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

## 快速开始

```bash
# 1. 安装依赖（已存在 .venv 可跳过）
.venv/Scripts/python.exe -m pip install -r requirements.txt

# 2. 配置 LLM（二选一）
cp .env.example .env
# 方式A：离线演示模式（默认，无需 key，用内置模板生成产物）—— .env 中 LLM_MOCK=1
# 方式B：真实模型 —— .env 中 LLM_MOCK=0，并填写 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
#        （OpenAI 兼容协议，DeepSeek/通义/火山等均可；支持按角色分别配置）

# 3. 启动
.venv/Scripts/python.exe app.py
# 浏览器打开 http://127.0.0.1:5100
```

操作流程：新建项目 → 上传需求文档 → 启动流水线 → 逐阶段评审（通过/驳回+意见）→ 测试用例阶段可导出 Excel/XMind。

## 技术要点

- **编排与门控**：LangGraph `interrupt()` + `SqliteSaver` 检查点；驳回用 `Command(resume={"approved":False,"comments":...})` 经条件边回环重跑当前阶段；进程重启后自动恢复"等待评审"状态。
- **RAG**：python-docx + pdfplumber 解析；jieba + rank_bm25 检索（纯 Python 无 GPU）；map-reduce 抽取。
- **输出健壮性**：统一「Markdown + 末尾 ```json 元数据块」约定，pydantic 风格校验失败自动重试 2 次，含 JSON 代码围栏剥离与平衡括号修复。
- **导出**：openpyxl 生成带格式 Excel；XMind 手工打包 Zen 格式（content.json + zip），不依赖老旧第三方库。

## 目录结构

```
app.py              Flask 入口（含重启状态恢复）
config.py           .env 配置与按角色 LLM 配置
core/               llm_client(OpenAI兼容) / mock_llm(离线演示) / json_utils(输出清洗)
parsing/            doc_parser / chunker / retriever(BM25) / extractor(map-reduce)
agents/             base_agent(重试校验) / stage_agents(4个智能体)
pipeline/           state / nodes(解析+智能体+门控) / graph(LangGraph 编排)
services/           pipeline_service(后台线程执行/恢复)
routes/             projects(项目/上传/启动/评审/日志) / export(Excel|XMind)
exporters/          excel_exporter / xmind_exporter
templates/ static/  前端单页（marked + mermaid 本地化渲染，2.5s 轮询）
data/               app.sqlite / checkpoints.sqlite / uploads / outputs / logs
scripts/            make_sample_prd.py（生成示例 PRD）
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
| GET | /api/projects/&lt;id&gt;/export/testcases?format=excel\|xmind | 导出测试用例 |

stage 取值：parse / requirement / hld / lld / testcase
