## P0-1 统一状态管理 - 重构完成说明

### 已创建的新文件

1. **core/state_manager.py** - 统一状态管理器
   - `ProjectStatus` 枚举：标准化所有状态
   - `StageType` 枚举：标准化阶段类型
   - `ProjectState` 数据类：完整的状态表示
   - `StateManager` 类：状态转换、验证、持久化的单一入口
   - 有限状态机：VALID_TRANSITIONS 矩阵确保状态转换合法性

2. **services/pipeline_service_v2.py** - 重构后的流水线服务
   - 使用 StateManager 替代直接操作数据库
   - 所有函数返回 (bool, str) 元组，便于错误处理
   - 移除了 _running 集合（由 StateManager 管理）
   - 简化了状态恢复逻辑

3. **pipeline/nodes_v2.py** - 重构后的节点函数
   - 使用 StateManager 进行状态转换
   - 移除直接操作 Project.status 的代码

### 迁移步骤（逐步替换，避免全部推翻）

**第1步：测试新模块（不影响现有系统）**
```python
# 在 Python REPL 中测试
from core.state_manager import get_state_manager, ProjectStatus
from db.models import SessionLocal

mgr = get_state_manager()
with SessionLocal() as session:
    state = mgr.load_state(session, 1)
    print(state)
```

**第2步：逐步替换服务层**
```python
# 在 routes/projects.py 中逐个替换
# 旧代码：
# ok = pipeline_service.start_pipeline(pid)
# if not ok: return jsonify({"error": "流水线已在运行中"}), 409

# 新代码：
success, message = pipeline_service_v2.start_pipeline(pid)
if not success:
    return jsonify({"error": message}), 409
```

**第3步：替换 app.py 的恢复逻辑**
```python
# 旧代码：restore_waiting_states() 复杂逻辑
# 新代码：
from services import pipeline_service_v2
pipeline_service_v2.auto_resume_orphans()
```

### 核心改进点

#### ✅ 解决的问题
1. **单一真相源**：状态只由 StateManager 管理
2. **状态转换验证**：非法转换会抛出异常
3. **清晰的错误处理**：所有操作返回 (success, message)
4. **简化的恢复逻辑**：自动检测 checkpoint 状态

#### 📊 代码对比

**旧代码（多重真相源）**：
```python
# 状态散落在3处
Project.status = "running"  # DB
_running.add(project_id)    # 内存
# LangGraph checkpoint       # 文件
```

**新代码（单一真相源）**：
```python
# 统一入口
state_mgr.transition(session, project_id, ProjectStatus.RUNNING)
```

### 接下来的步骤

我将继续进行 P0-2：数据库规范化重构。是否继续？
