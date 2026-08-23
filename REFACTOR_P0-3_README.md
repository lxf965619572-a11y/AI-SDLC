## P0-3 错误处理可观测性 - 重构完成说明

### 已创建的新文件

1. **core/errors.py** - 统一的错误处理和追踪系统
   - `ErrorCategory` 枚举：错误分类（LLM调用、解析、验证等）
   - `ErrorSeverity` 枚举：严重级别（DEBUG → CRITICAL）
   - `ErrorContext` 数据类：记录错误发生时的上下文
   - `ErrorRecord` 数据类：结构化的错误记录
   - `ErrorTracker` 类：错误收集和统计分析
   - 业务特定错误类：`LLMCallError`, `ValidationError`, `ParsingError`, `StateTransitionError`

2. **parsing/extractor_v2.py** - 增强错误处理的抽取器
   - `ExtractionResult` 数据类：明确标记数据来源（llm/cache/local_fallback）
   - 所有降级策略都显式记录到 ErrorTracker
   - 区分"缓存命中"和"降级到本地合并"

### 核心改进点

#### 旧代码问题：静默吞掉错误
```python
try:
    raw = llm_client.chat(...)
    merged = extract_json(raw)
    if merged:
        return merged
except Exception:
    pass  # 用户完全不知道发生了什么
return _local_merge(batch)
```

#### 新代码：可观测
```python
try:
    raw = llm_client.chat(...)
    merged = extract_json(raw)
    if merged:
        return ExtractionResult(data=merged, source="llm")
    else:
        record_error(
            category=ErrorCategory.LLM_CALL,
            severity=ErrorSeverity.WARNING,
            message="LLM返回无效JSON，降级到本地合并",
            context=context,
            fallback_used=True,
            fallback_reason="JSON解析失败"
        )
        return ExtractionResult(
            data=_local_merge(batch),
            source="local_fallback",
            fallback_reason="JSON解析失败"
        )
except Exception as e:
    record_error(
        category=ErrorCategory.LLM_CALL,
        severity=ErrorSeverity.WARNING,
        message=f"LLM调用失败: {e}",
        context=context,
        exception=e,
        fallback_used=True
    )
    return ExtractionResult(
        data=_local_merge(batch),
        source="local_fallback",
        error=e
    )
```

### 错误追踪能力

**查询错误统计**
```python
from core.errors import get_error_tracker

tracker = get_error_tracker()
stats = tracker.get_statistics(project_id=1)
# {
#   "total": 15,
#   "by_category": {"llm_call": 10, "parsing": 3, "validation": 2},
#   "by_severity": {"warning": 10, "error": 5},
#   "fallback_count": 8
# }
```

**查询特定错误**
```python
llm_errors = tracker.get_errors(
    project_id=1,
    category=ErrorCategory.LLM_CALL
)
for error in llm_errors:
    print(error.format_for_user())
```

### 迁移步骤

1. 引入错误追踪（不影响现有逻辑）
2. 替换关键模块（extractor_v2）
3. 添加错误查询API

### 业务价值

1. 问题快速定位
2. 系统健康度量（降级率）
3. 用户透明度

---

## 📊 P0重构总结

已完成三个最高优先级的重构：

### P0-1: 统一状态管理 ✅
- **文件**: `core/state_manager.py`, `services/pipeline_service_v2.py`, `pipeline/nodes_v2.py`
- **改进**: 单一真相源、有限状态机、清晰的错误处理

### P0-2: 数据库规范化 ✅
- **文件**: `db/models_v2.py`, `db/repository.py`, `agents/stage_agents_v2.py`
- **改进**: JSON blob → 关系表、支持高效查询、索引优化

### P0-3: 错误处理可观测性 ✅
- **文件**: `core/errors.py`, `parsing/extractor_v2.py`
- **改进**: 结构化错误、降级策略显式标记、错误统计

### 接下来的重构优先级

**P1: 消除代码重复** (技术债)
- 抽象智能体执行逻辑
- 统一配置管理

**P2: 优化增强** (性能和体验)
- WebSocket替换轮询
- 局部修订支持

是否继续P1级重构？
