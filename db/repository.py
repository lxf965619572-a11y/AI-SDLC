"""配套的数据访问层：简化规范化表的查询和操作。

提供高层API，隐藏底层SQL细节。
"""
from typing import List, Optional

from sqlalchemy.orm import Session

from db.models_v2 import (API, Ambiguity, DataStructure, DatabaseTable,
                          FunctionalRequirement, Function, Module, Risk,
                          StageArtifact, TestCase)


class ArtifactRepository:
    """产物仓储：封装对规范化表的操作"""

    @staticmethod
    def save_functional_requirements(session: Session, artifact_id: int, requirements: List[dict]):
        """批量保存功能需求"""
        for req in requirements:
            session.add(FunctionalRequirement(
                artifact_id=artifact_id,
                req_id=req.get("id", ""),
                description=req.get("desc", ""),
                priority=req.get("priority", "P2")
            ))
        session.commit()

    @staticmethod
    def save_testcases(session: Session, artifact_id: int, testcases: List[dict]):
        """批量保存测试用例"""
        import json
        for tc in testcases:
            session.add(TestCase(
                artifact_id=artifact_id,
                case_id=tc.get("id", ""),
                module=tc.get("module"),
                title=tc.get("title", ""),
                type=tc.get("type", "功能"),
                priority=tc.get("priority", "P2"),
                preconditions=tc.get("preconditions", ""),
                steps=json.dumps(tc.get("steps", []), ensure_ascii=False),
                expected=tc.get("expected", "")
            ))
        session.commit()

    @staticmethod
    def save_apis(session: Session, artifact_id: int, apis: List[dict]):
        """批量保存API定义"""
        for api in apis:
            session.add(API(
                artifact_id=artifact_id,
                method=api.get("method", "GET"),
                path=api.get("path", ""),
                description=api.get("desc", "")
            ))
        session.commit()

    @staticmethod
    def get_testcases_by_priority(session: Session, artifact_id: int, priority: str) -> List[TestCase]:
        """按优先级查询测试用例（高效索引查询）"""
        return session.query(TestCase).filter_by(
            artifact_id=artifact_id,
            priority=priority
        ).all()

    @staticmethod
    def get_testcases_by_type(session: Session, artifact_id: int, type: str) -> List[TestCase]:
        """按类型查询测试用例"""
        return session.query(TestCase).filter_by(
            artifact_id=artifact_id,
            type=type
        ).all()

    @staticmethod
    def get_testcases_by_module(session: Session, artifact_id: int, module: str) -> List[TestCase]:
        """按模块查询测试用例"""
        return session.query(TestCase).filter_by(
            artifact_id=artifact_id,
            module=module
        ).all()

    @staticmethod
    def get_functional_requirements_by_priority(
        session: Session, artifact_id: int, priority: str
    ) -> List[FunctionalRequirement]:
        """按优先级查询功能需求"""
        return session.query(FunctionalRequirement).filter_by(
            artifact_id=artifact_id,
            priority=priority
        ).all()

    @staticmethod
    def get_high_severity_risks(session: Session, artifact_id: int) -> List[Risk]:
        """查询高风险项"""
        return session.query(Risk).filter_by(
            artifact_id=artifact_id,
            severity="高"
        ).all()

    @staticmethod
    def testcase_to_dict(tc: TestCase) -> dict:
        """将测试用例ORM对象转为字典（用于API返回）"""
        import json
        return {
            "id": tc.case_id,
            "module": tc.module,
            "title": tc.title,
            "type": tc.type,
            "priority": tc.priority,
            "preconditions": tc.preconditions,
            "steps": json.loads(tc.steps) if tc.steps else [],
            "expected": tc.expected
        }

    @staticmethod
    def get_testcase_statistics(session: Session, artifact_id: int) -> dict:
        """测试用例统计（利用规范化表）"""
        testcases = session.query(TestCase).filter_by(artifact_id=artifact_id).all()

        stats = {
            "total": len(testcases),
            "by_type": {},
            "by_priority": {},
            "by_module": {}
        }

        for tc in testcases:
            # 按类型统计
            stats["by_type"][tc.type] = stats["by_type"].get(tc.type, 0) + 1
            # 按优先级统计
            stats["by_priority"][tc.priority] = stats["by_priority"].get(tc.priority, 0) + 1
            # 按模块统计
            if tc.module:
                stats["by_module"][tc.module] = stats["by_module"].get(tc.module, 0) + 1

        return stats


class QueryService:
    """查询服务：提供业务级别的查询功能"""

    @staticmethod
    def get_p0_testcases_summary(session: Session, project_id: int) -> dict:
        """获取项目所有P0测试用例摘要（跨产物聚合）"""
        from db.models_v2 import StageArtifact

        # 查找测试用例产物
        artifact = session.query(StageArtifact).filter_by(
            project_id=project_id,
            stage="testcase"
        ).order_by(StageArtifact.version.desc()).first()

        if not artifact:
            return {"total": 0, "testcases": []}

        p0_cases = ArtifactRepository.get_testcases_by_priority(
            session, artifact.id, "P0"
        )

        return {
            "total": len(p0_cases),
            "testcases": [ArtifactRepository.testcase_to_dict(tc) for tc in p0_cases]
        }

    @staticmethod
    def get_requirement_coverage(session: Session, project_id: int) -> dict:
        """需求覆盖率分析（功能需求 vs 测试用例）"""
        from db.models_v2 import StageArtifact

        # 获取需求产物
        req_artifact = session.query(StageArtifact).filter_by(
            project_id=project_id,
            stage="requirement"
        ).order_by(StageArtifact.version.desc()).first()

        # 获取测试用例产物
        tc_artifact = session.query(StageArtifact).filter_by(
            project_id=project_id,
            stage="testcase"
        ).order_by(StageArtifact.version.desc()).first()

        if not req_artifact or not tc_artifact:
            return {"coverage": 0, "details": []}

        # 统计功能需求
        total_reqs = session.query(FunctionalRequirement).filter_by(
            artifact_id=req_artifact.id
        ).count()

        # 统计测试用例
        total_tcs = session.query(TestCase).filter_by(
            artifact_id=tc_artifact.id
        ).count()

        # 简单的覆盖率估算（实际应该做需求ID与测试用例的关联）
        coverage = min(100, (total_tcs / max(total_reqs, 1)) * 100)

        return {
            "total_requirements": total_reqs,
            "total_testcases": total_tcs,
            "coverage_percentage": round(coverage, 2)
        }


# 使用示例
def example_usage():
    """使用示例（仅供参考，不执行）"""
    from db.models_v2 import SessionLocal

    with SessionLocal() as session:
        # 示例1：查询所有P0测试用例
        p0_summary = QueryService.get_p0_testcases_summary(session, project_id=1)
        print(f"P0测试用例总数: {p0_summary['total']}")

        # 示例2：查询某个产物的测试用例统计
        stats = ArtifactRepository.get_testcase_statistics(session, artifact_id=10)
        print(f"测试用例统计: {stats}")

        # 示例3：查询高风险项
        high_risks = ArtifactRepository.get_high_severity_risks(session, artifact_id=5)
        print(f"高风险项数量: {len(high_risks)}")

        # 示例4：需求覆盖率
        coverage = QueryService.get_requirement_coverage(session, project_id=1)
        print(f"需求覆盖率: {coverage['coverage_percentage']}%")
