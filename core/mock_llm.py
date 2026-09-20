"""离线演示模式：不调用真实 LLM，根据 prompt 关键词返回各阶段模板产物。
用于无 API key 时快速体验全流程，也用于自动化回归测试。"""
import hashlib
import json
import re


def _prompt_of(messages: list[dict]) -> str:
    return "\n".join(m.get("content", "") for m in messages if isinstance(m.get("content"), str))


def _digest(text: str, n: int = 4) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:n]


# ---- 追溯编号提取：让离线演示也能产出完整的可追溯链 ----
_TAG_RE = re.compile(r"\bD\d+C\d+\b|\bC\d+\b")
_SRC_RE = re.compile(r"\b(?:OBJ|RULE|FLOW)-\d+\b")
_FR_RE = re.compile(r"\bFR-\d+\b")
_P0_RE = re.compile(r'"id"\s*:\s*"(FR-\d+)"[^{}]*?"priority"\s*:\s*"P0"')
_P0_REV_RE = re.compile(r'"priority"\s*:\s*"P0"[^{}]*?"id"\s*:\s*"(FR-\d+)"')


def _uniq(seq) -> list[str]:
    out = []
    for x in seq:
        if x and x not in out:
            out.append(x)
    return out


def _tags(prompt: str, limit: int = 3) -> list[str]:
    return _uniq(_TAG_RE.findall(prompt))[:limit]


def _src_ids(prompt: str) -> list[str]:
    return _uniq(m.group(0) for m in _SRC_RE.finditer(prompt))


def _fr_ids(prompt: str) -> list[str]:
    return _uniq(m.group(0) for m in _FR_RE.finditer(prompt))


def _p0_ids(prompt: str) -> list[str]:
    return _uniq(_P0_RE.findall(prompt) + _P0_REV_RE.findall(prompt))


def _spread(items: list[str], n: int) -> list[list[str]]:
    """把 items 轮流分配到 n 个桶，保证每个 item 至少落一个桶。"""
    buckets: list[list[str]] = [[] for _ in range(max(n, 1))]
    for i, it in enumerate(items):
        b = buckets[i % len(buckets)]
        if it not in b:
            b.append(it)
    return buckets[:n]


def mock_complete(messages: list[dict], role: str) -> str:
    prompt = _prompt_of(messages)

    # ---- AI 助手对话（离线演示）----
    if role == "chat":
        # 取最后一条用户消息
        user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break
        has_ctx = "阶段产物" in prompt
        ctx_note = ("我已基于当前项目各阶段产物（结构化原始数据 / 需求规格 / "
                    "概要设计 / 详细设计 / 测试用例）进行回答。"
                    if has_ctx else
                    "当前项目暂无阶段产物，以下为通用回答。")
        return (
            f"**【离线演示模式回复】**\n\n{ctx_note}\n\n"
            f"针对你的问题：「{user_msg[:80]}」\n\n"
            "这是离线演示模式（`LLM_MOCK=1`）的模拟回复，不会调用真实大模型。\n"
            "如需真实对话能力，请在 `.env` 中设置 `LLM_MOCK=0` 并配置 "
            "`LLM_API_KEY`。\n\n"
            "配置完成后，我可以帮你：\n"
            "- 提取并汇总需求清单、接口列表、测试用例；\n"
            "- 对比各阶段产物的一致性与遗漏项；\n"
            "- 解答软件工程与本项目相关的通用问题。"
        )

    # ---- 阶段1：分块抽取 ----
    if role == "extraction" and "__MAP__" in prompt:
        src = _tags(prompt)
        return json.dumps({
            "objects": [
                {"name": "订单", "attrs": ["订单编号", "客户", "商品明细", "金额", "状态"],
                 "desc": "客户下单后生成的交易单据", "source": src},
                {"name": "用户", "attrs": ["用户ID", "姓名", "手机号", "会员等级"],
                 "desc": "系统中的注册用户", "source": src},
            ],
            "rules": [
                {"name": "订单金额校验", "desc": "订单金额必须大于0且不超过50000元",
                 "source": src},
                {"name": "库存扣减", "desc": "下单成功后立即扣减库存，支付超时30分钟自动释放",
                 "source": src},
            ],
            "flows": [
                {"name": "下单流程", "steps": ["选择商品", "确认订单", "提交订单", "支付", "支付成功通知"],
                 "source": src},
            ],
        }, ensure_ascii=False)

    if role == "extraction":  # reduce
        src = _tags(prompt)
        return json.dumps({
            "objects": [
                {"name": "订单", "attrs": ["订单编号", "客户", "商品明细", "金额", "状态"],
                 "desc": "客户下单后生成的交易单据", "source": src},
                {"name": "用户", "attrs": ["用户ID", "姓名", "手机号", "会员等级"],
                 "desc": "系统中的注册用户", "source": src},
                {"name": "商品", "attrs": ["SKU", "名称", "价格", "库存"], "desc": "可售商品",
                 "source": src},
            ],
            "rules": [
                {"name": "订单金额校验", "desc": "订单金额必须大于0且不超过50000元",
                 "source": src},
                {"name": "库存扣减", "desc": "下单成功后立即扣减库存，支付超时30分钟自动释放",
                 "source": src},
                {"name": "会员折扣", "desc": "金卡会员享受95折，银卡会员享受98折",
                 "source": src},
            ],
            "flows": [
                {"name": "下单流程", "steps": ["选择商品", "确认订单", "提交订单", "支付", "支付成功通知"],
                 "source": src},
                {"name": "退款流程", "steps": ["发起退款申请", "客服审核", "原路退回", "通知用户"],
                 "source": src},
            ],
        }, ensure_ascii=False)

    # ---- 阶段2：需求分析 ----
    if role == "requirement":
        srcs = _src_ids(prompt)
        fr_src = _spread(srcs, 4) if srcs else [[] for _ in range(4)]
        meta = {
            "functional_requirements": [
                {"id": "FR-001", "desc": "用户可以注册并登录系统", "priority": "P0",
                 "derived_from": fr_src[0]},
                {"id": "FR-002", "desc": "用户可以浏览商品并加入购物车", "priority": "P0",
                 "derived_from": fr_src[1]},
                {"id": "FR-003", "desc": "用户可以提交订单并完成支付", "priority": "P0",
                 "derived_from": fr_src[2]},
                {"id": "FR-004", "desc": "用户可以发起退款申请", "priority": "P1",
                 "derived_from": fr_src[3]},
            ],
            "ambiguities": [
                {"id": "AMB-001", "desc": "PRD未明确并发下单时库存超卖的处理策略", "severity": "高"},
            ],
            "risks": [
                {"id": "RISK-001", "desc": "支付渠道依赖第三方，需考虑降级方案", "severity": "中"},
            ],
        }
        doc = f"""# 软件需求规格说明书（SRS）

## 1. 引言
本文档基于《结构化原始数据》对电商交易系统的需求进行分析，形成功能需求清单、业务逻辑与用户故事。

## 2. 功能需求清单

| 编号 | 需求描述 | 优先级 | 来源编号 |
|---|---|---|---|
| FR-001 | 用户可以注册并登录系统 | P0 | {"、".join(fr_src[0]) or "-"} |
| FR-002 | 用户可以浏览商品并加入购物车 | P0 | {"、".join(fr_src[1]) or "-"} |
| FR-003 | 用户可以提交订单并完成支付 | P0 | {"、".join(fr_src[2]) or "-"} |
| FR-004 | 用户可以发起退款申请 | P1 | {"、".join(fr_src[3]) or "-"} |

## 3. 用户故事

- **US-001** 作为【注册用户】，我想要【浏览商品并加入购物车】，以便【快速完成购买】。
- **US-002** 作为【买家】，我想要【提交订单并完成支付】，以便【获得所购商品】。
- **US-003** 作为【买家】，我想要【发起退款】，以便【在商品有问题时挽回损失】。

## 4. 业务逻辑
1. 订单金额必须大于 0 且不超过 50000 元。
2. 下单成功后立即扣减库存；支付超时 30 分钟自动释放库存。
3. 金卡会员享受 95 折，银卡会员享受 98 折。

## 5. 模糊点与风险项

| 类型 | 编号 | 描述 | 严重度 |
|---|---|---|---|
| 模糊点 | AMB-001 | PRD未明确并发下单时库存超卖的处理策略 | 高 |
| 风险项 | RISK-001 | 支付渠道依赖第三方，需考虑降级方案 | 中 |

```json
{json.dumps(meta, ensure_ascii=False)}
```
"""
        return doc

    # ---- 阶段3：概要设计 ----
    if role == "hld":
        modules = ["用户模块", "商品模块", "订单模块", "支付模块"]
        frs = _fr_ids(prompt)
        buckets = _spread(frs, len(modules)) if frs else [[] for _ in modules]
        accept_rows = "\n".join(f"| {m} | {'、'.join(buckets[i]) or '-'} |"
                                for i, m in enumerate(modules))
        meta = {
            "modules": modules,
            "tables": ["user", "product", "order", "order_item", "payment"],
            "apis": [
                {"method": "POST", "path": "/api/auth/register", "desc": "用户注册"},
                {"method": "POST", "path": "/api/orders", "desc": "创建订单"},
                {"method": "GET", "path": "/api/products", "desc": "商品列表"},
            ],
            "derived_from": {m: buckets[i] for i, m in enumerate(modules)},
        }
        doc = f"""# 概要设计说明书

## 1. 系统模块划分

| 模块 | 职责 | 依赖 |
|---|---|---|
| 用户模块 | 注册、登录、会员管理 | 无 |
| 商品模块 | 商品展示、库存管理 | 无 |
| 订单模块 | 下单、订单状态流转 | 用户模块、商品模块 |
| 支付模块 | 支付、退款 | 订单模块 |

## 2. 技术架构

采用前后端分离架构：Vue 前端 + FastAPI 后端 + MySQL + Redis。

```mermaid
graph TD
    A[Vue 前端] --> B[API 网关 Nginx]
    B --> C[用户服务]
    B --> D[商品服务]
    B --> E[订单服务]
    B --> F[支付服务]
    E --> D
    F --> E
    C --> G[(MySQL)]
    D --> G
    E --> G
    F --> G
    E --> H[(Redis 库存缓存)]
```

## 3. 核心下单流程

```mermaid
flowchart LR
    S[选择商品] --> T[确认订单]
    T --> U[提交订单]
    U --> V[扣减库存]
    V --> W[发起支付]
    W --> X[支付成功通知]
```

## 4. 数据库设计（核心 DDL）

```sql
CREATE TABLE user (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  username VARCHAR(64) NOT NULL UNIQUE,
  phone VARCHAR(20),
  member_level VARCHAR(16) DEFAULT 'normal'
);

CREATE TABLE `order` (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  order_no VARCHAR(32) NOT NULL UNIQUE,
  user_id BIGINT NOT NULL,
  amount DECIMAL(12,2) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'CREATED'
);
```

## 5. 核心 API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/auth/register | 用户注册 |
| POST | /api/orders | 创建订单 |
| GET | /api/products | 商品列表 |

## 6. 需求承接

| 模块 | 承接需求 |
|---|---|
{accept_rows}

```json
{json.dumps(meta, ensure_ascii=False)}
```
"""
        return doc

    # ---- 阶段4：详细设计 ----
    if role == "lld":
        funcs = ["order_create", "order_pay"]
        frs = _fr_ids(prompt)
        fbuckets = _spread(frs, len(funcs)) if frs else [[] for _ in funcs]
        trace_rows = "\n".join(f"| {f} | {'、'.join(fbuckets[i]) or '-'} |"
                               for i, f in enumerate(funcs))
        meta = {
            "data_structures": ["Order", "OrderItem", "InventorySlot", "PayResult"],
            "functions": [
                {"name": "order_create", "sig": "int order_create(uint32_t user_id, const OrderItem *items, size_t n_items, Order *out)",
                 "desc": "创建订单并扣减库存"},
                {"name": "order_pay", "sig": "PayResult order_pay(const char *order_no, const char *channel)",
                 "desc": "发起订单支付"},
            ],
            "derived_from": {f: fbuckets[i] for i, f in enumerate(funcs)},
        }
        doc = f"""# 详细设计说明书

## 1. 订单模块数据结构设计

```mermaid
flowchart LR
    subgraph api[api 层]
        C[order_ctrl.c]
    end
    subgraph service[service 层]
        S[order_svc.c]
        I[inventory_svc.c]
    end
    subgraph data[data 层]
        R[order_repo.c]
    end
    C --> S
    S --> I
    S --> R
```

关键数据结构：

```c
typedef struct {{
    uint32_t sku_id;      /* 商品编号 */
    uint32_t qty;         /* 数量 */
    int64_t  price_cent;  /* 单价（分） */
}} OrderItem;

typedef struct {{
    char     order_no[33];   /* 订单号，NUL 结尾 */
    uint32_t user_id;        /* 下单用户 */
    int64_t  amount_cent;    /* 订单总额（分） */
    int      status;         /* ORDER_STATUS_* 枚举 */
    OrderItem items[MAX_ITEMS_PER_ORDER];
    size_t   n_items;
}} Order;
```

## 2. 接口定义

### 2.1 order_create
- 签名：`int order_create(uint32_t user_id, const OrderItem *items, size_t n_items, Order *out)`
- 说明：校验金额与库存，生成订单号，扣减库存，落库。
- 返回：0 成功；负值为错误码（`-E_INVENTORY_SHORTAGE` 库存不足；`-E_INVALID_AMOUNT` 金额非法）。

### 2.2 order_pay
- 签名：`PayResult order_pay(const char *order_no, const char *channel)`
- 说明：调用支付渠道创建支付单，返回支付凭证。

## 3. 下单核心序列图

```mermaid
sequenceDiagram
    participant U as 用户
    participant C as order_ctrl
    participant S as order_svc
    participant I as inventory_svc
    participant DB as MySQL
    U->>C: POST /api/orders
    C->>S: order_create(user_id, items)
    S->>I: inventory_deduct(sku, qty)
    I-->>S: 扣减成功
    S->>DB: order_repo_save(order)
    DB-->>S: 订单已落库
    S-->>C: Order
    C-->>U: 201 订单创建成功
```

## 4. 需求实现对照

| 函数 | 实现需求 |
|---|---|
{trace_rows}

```json
{json.dumps(meta, ensure_ascii=False)}
```
"""
        return doc

    # ---- 阶段5：测试用例 ----
    if role == "testcase":
        frs = _fr_ids(prompt)
        p0 = _p0_ids(prompt)
        # 先铺 P0，再铺其余需求，保证 mock 产物一定通过追溯校验
        pool = p0 + [f for f in frs if f not in p0]
        cases = [
            {"id": "TC-001", "module": "订单", "title": "正常下单成功", "type": "功能",
             "priority": "P0", "preconditions": "用户已登录；商品A库存≥1",
             "steps": ["将商品A加入购物车", "点击提交订单", "完成支付"],
             "expected": "订单创建成功，状态为已支付，库存减1"},
            {"id": "TC-002", "module": "订单", "title": "订单金额为0", "type": "边界",
             "priority": "P0", "preconditions": "用户已登录",
             "steps": ["构造金额为0的订单请求", "调用创建订单接口"],
             "expected": "接口返回400，提示订单金额必须大于0"},
            {"id": "TC-003", "module": "订单", "title": "订单金额超过50000上限", "type": "边界",
             "priority": "P1", "preconditions": "用户已登录",
             "steps": ["构造金额为50001的订单请求", "调用创建订单接口"],
             "expected": "接口返回400，提示金额超出上限"},
            {"id": "TC-004", "module": "订单", "title": "库存不足下单", "type": "异常",
             "priority": "P0", "preconditions": "商品A库存为0",
             "steps": ["将商品A加入购物车", "提交订单"],
             "expected": "下单失败，提示库存不足，不生成订单"},
            {"id": "TC-005", "module": "支付", "title": "支付超时30分钟自动取消", "type": "场景",
             "priority": "P1", "preconditions": "存在一笔待支付订单已超过30分钟",
             "steps": ["等待定时任务执行", "查询订单状态"],
             "expected": "订单状态变为已取消，库存已释放"},
            {"id": "TC-006", "module": "订单", "title": "金卡会员享受95折", "type": "场景",
             "priority": "P1", "preconditions": "用户为金卡会员",
             "steps": ["购买原价100元商品", "提交订单查看金额"],
             "expected": "订单实付金额为95元"},
        ]
        for i, c in enumerate(cases):
            c["fr_ids"] = [pool[i % len(pool)]] if pool else []
        for j in range(len(cases), len(pool)):
            tgt = cases[j % len(cases)]["fr_ids"]
            if pool[j] not in tgt:
                tgt.append(pool[j])
        return json.dumps({"testcases": cases}, ensure_ascii=False)

    return "OK"
