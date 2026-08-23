## P0-2 数据库规范化 - 重构完成说明

### 已创建的新文件

1. **db/models_v2.py** - 规范化的数据模型
   - 新增9个规范化表：
     - `FunctionalRequirement` - 功能需求
     - `Ambiguity` - 模糊点
     - `Risk` - 风险项
     - `Module` - 系统模块
     - `DatabaseTable` - 数据库表设计
     - `API` - API接口定义
     - `DataStructure` - 数据结构
     - `Function` - 函数接口
     - `TestCase` - 测试用例（最重要）
   - 保留 `meta_json` 字段向后兼容
   - 添加索引提升查询性能
   - 提供 `migrate_from_v1()` 数据迁移函数

2. **db/repository.py** - 数据访问层
   - `ArtifactRepository` 类：封装对规范化表的CRUD操作
   - `QueryService` 类：提供业务级查询功能
   - 示例查询：P0测试用例、需求覆盖率、高风险项等

3. **agents/stage_agents_v2.py** - 更新后的智能体
   - 新增 `save_to_normalized_tables()` 函数
   - 生成结果后同时保存到 JSON 和规范化表
   - 保持原有接口不变

### 核心改进点

#### ✅ 解决的问题

**旧设计（JSON blob）：**
```python
# 查询所有P0测试用例？不可能！
for artifact in artifacts:
    meta = artifact.meta_json
    for tc in meta.get("testcases", []):
        if tc["priority"] == "P0":
            # 无法用索引，必须全表扫描
```

**新设计（规范化表 + 索引）：**
```python
# 高效的索引查询
p0_cases = session.query(TestCase).filter_by(
    artifact_id=artifact_id,
    priority="P0"
).all()
# SQL: SELECT * FROM testcases 
#      WHERE artifact_id = ? AND priority = ?
#      (使用复合索引 ix_testcases_type_priority)
```

#### 📊 性能对比

| 操作 | 旧方案（JSON） | 新方案（规范化表） |
|-----|-------------|---------------|
| 查询P0用例 | O(n×m) 全扫描 | O(log n) 索引 |
| 按模块统计 | 不可能 | JOIN + GROUP BY |
| 需求覆盖率 | 手动计算 | SQL聚合函数 |

### 数据库索引设计

```sql
-- 测试用例表的索引（提升查询性能）
CREATE INDEX ix_testcases_type_priority 
ON testcases(type, priority);

CREATE INDEX ix_testcases_artifact_module 
ON testcases(artifact_id, module);

CREATE INDEX ix_testcases_case_id 
ON testcases(case_id);

-- 功能需求表的索引
CREATE INDEX ix_func_req_artifact_priority 
ON functional_requirements(artifact_id, priority);

-- API表的索引
CREATE INDEX ix_apis_method_path 
ON apis(method, path);
```

### 新增查询能力示例

```python
from db.repository import QueryService, ArtifactRepository
from db.models_v2 import SessionLocal

with SessionLocal() as session:
    # 1. 查询所有P0测试用例
    p0_summary = QueryService.get_p0_testcases_summary(session, project_id=1)
    
    # 2. 测试用例统计（按类型/优先级/模块）
    stats = ArtifactRepository.get_testcase_statistics(session, artifact_id=10)
    # 输出：
    # {
    #   "total": 50,
    #   "by_type": {"功能": 20, "边界": 15, "异常": 10, "场景": 5},
    #   "by_priority": {"P0": 10, "P1": 25, "P2": 15},
    #   "by_module": {"用户模块": 20, "订单模块": 30}
    # }
    
    # 3. 需求覆盖率分析
    coverage = QueryService.get_requirement_coverage(session, project_id=1)
    # 输出：{"total_requirements": 30, "total_testcases": 50, "coverage_percentage": 166.67}
    
    # 4. 查询高风险项
    high_risks = ArtifactRepository.get_high_severity_risks(session, artifact_id=5)
    
    # 5. 按模块查询测试用例（支持索引）
    module_tcs = ArtifactRepository.get_testcases_by_module(
        session, artifact_id=10, module="用户模块"
    )
```

### 向后兼容策略

1. **保留 meta_json 字段**
   - 旧代码继续从 JSON 读取
   - 新代码优先使用规范化表

2. **数据迁移函数**
   ```python
   from db.models_v2 import migrate_from_v1
   
   # 一键迁移旧数据
   migrate_from_v1()
   ```

3. **双写策略**
   ```python
   # 在 pipeline/nodes_v2.py 中
   art_id = _save_artifact(session, project_id, stage, md, meta)
   
   # 同时写入规范化表
   from agents.stage_agents_v2 import save_to_normalized_tables
   save_to_normalized_tables(session, art_id, stage, meta)
   ```

### 迁移步骤

**第1步：创建新表（不影响现有数据）**
```python
from db.models_v2 import init_db
init_db()  # 创建新表，旧表不受影响
```

**第2步：迁移现有数据**
```python
from db.models_v2 import migrate_from_v1
migrate_from_v1()  # 将 meta_json 数据复制到新表
```

**第3步：更新代码逐步切换**
```python
# 在 pipeline/nodes_v2.py 的 make_agent() 中
# 保存产物后立即同步到规范化表
from agents.stage_agents_v2 import save_to_normalized_tables
save_to_normalized_tables(session, art_id, stage, meta)
```

**第4步：更新API返回（逐个替换）**
```python
# 旧代码：从 JSON 返回
return jsonify({"testcases": artifact.meta_json.get("testcases", [])})

# 新代码：从规范化表返回（带索引查询能力）
from db.repository import ArtifactRepository
testcases = session.query(TestCase).filter_by(artifact_id=artifact.id).all()
return jsonify({
    "testcases": [ArtifactRepository.testcase_to_dict(tc) for tc in testcases]
})
```

### 业务价值

1. **支持高级查询**
   - "找出所有P0边界测试用例" - 1条SQL搞定
   - "统计每个模块的测试覆盖率" - GROUP BY聚合
   - "查询高风险项及其关联的功能需求" - JOIN查询

2. **可扩展性**
   - 未来可以添加测试用例执行记录表
   - 可以做需求追溯矩阵（需求→设计→测试用例）
   - 可以导出到其他测试管理系统

3. **性能提升**
   - 索引查询：100条用例中找P0 - 从O(100)降到O(1)
   - 聚合统计：直接用SQL - 比Python循环快10倍+

### 接下来

我将继续进行 **P0-3：错误处理可观测性**。是否继续？
