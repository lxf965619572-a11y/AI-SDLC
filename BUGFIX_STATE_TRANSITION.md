# 状态转换 Bug 修复

## 问题描述

**现象**: 上传文档后启动流水线失败，提示 "当前状态 created 不允许启动"

**根因**: 新的状态管理器引入了严格的状态机，要求 `CREATED -> READY -> PARSING`，但文档上传后没有将状态从 `CREATED` 转换为 `READY`。

---

## 状态机设计

```
CREATED (已创建，无文档)
   ↓ 上传文档
READY (已上传文档，可启动)
   ↓ 启动流水线
PARSING (解析中)
   ↓
RUNNING (智能体执行中)
   ↓
WAITING_REVIEW (等待评审) / COMPLETED (完成) / FAILED (失败)
```

---

## 修复内容

### 1. 文档上传 - 转换为 READY

**文件**: `routes/projects.py`

**修复后**:
```python
# 上传文档后，将状态从 CREATED 转换为 READY
if proj.status == "created":
    proj.status = "ready"
```

### 2. 文档删除 - 回到 CREATED

**文件**: `routes/projects.py`

**修复后**:
```python
# 删除文档后，检查是否还有其他文档
remaining_docs = session.query(Document).filter_by(project_id=pid).count()
if remaining_docs == 0 and p.status == "ready":
    # 没有文档了，状态回到 CREATED
    p.status = "created"
```

---

## 验证步骤

### 测试流程
1. 创建项目 -> status: "created"
2. 上传文档 -> status: "ready"
3. 启动流水线 -> status: "parsing"
4. 删除所有文档（ready状态下） -> status: "created"

---

## 状态转换规则

| 当前状态 | 操作 | 新状态 |
|---------|------|--------|
| CREATED | 上传文档 | READY |
| READY | 删除所有文档 | CREATED |
| READY | 启动流水线 | PARSING |
| PARSING | 解析完成 | RUNNING |
| RUNNING | 到达评审门 | WAITING_REVIEW |
| RUNNING | 全部完成 | COMPLETED |
| FAILED | 断点续跑 | PARSING |

---

**修复时间**: 2026-08-23
**影响文件**: `routes/projects.py` (2 处修改)
