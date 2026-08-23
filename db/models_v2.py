"""规范化的数据模型：将 JSON blob 拆分为关系表。

主要改进：
1. 测试用例独立建表，支持高效查询和过滤
2. 功能需求、风险项、API 定义等独立建表
3. 保留原 meta_json 字段用于向后兼容
4. 增加索引提升查询性能
"""
from datetime import datetime

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Index, Integer,
                        String, Text, create_engine)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column,
                           relationship, sessionmaker)

import config


class Base(DeclarativeBase):
    pass


def now():
    return datetime.now()


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="created", index=True)
    current_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    documents = relationship("Document", back_populates="project", cascade="all, delete-orphan")
    artifacts = relationship("StageArtifact", back_populates="project", cascade="all, delete-orphan")
    reviews = relationship("Review", back_populates="project", cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    filename: Mapped[str] = mapped_column(String(300))
    stored_path: Mapped[str] = mapped_column(String(500))
    file_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), default="uploaded")
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    project = relationship("Project", back_populates="documents")
    chunks = relationship("Chunk", back_populates="document", cascade="all, delete-orphan")


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str | None] = mapped_column(String(300), nullable=True)
    content: Mapped[str] = mapped_column(Text)

    document = relationship("Document", back_populates="chunks")


class StageArtifact(Base):
    """各阶段产物：markdown 文档 + 结构化元数据（向后兼容保留 meta_json）"""
    __tablename__ = "stage_artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    stage: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(200))
    markdown: Mapped[str] = mapped_column(Text)
    meta_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending_review")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    project = relationship("Project", back_populates="artifacts")
    reviews = relationship("Review", back_populates="artifact", cascade="all, delete-orphan")

    functional_requirements = relationship("FunctionalRequirement", back_populates="artifact", cascade="all, delete-orphan")
    ambiguities = relationship("Ambiguity", back_populates="artifact", cascade="all, delete-orphan")
    risks = relationship("Risk", back_populates="artifact", cascade="all, delete-orphan")
    modules = relationship("Module", back_populates="artifact", cascade="all, delete-orphan")
    database_tables = relationship("DatabaseTable", back_populates="artifact", cascade="all, delete-orphan")
    apis = relationship("API", back_populates="artifact", cascade="all, delete-orphan")
    data_structures = relationship("DataStructure", back_populates="artifact", cascade="all, delete-orphan")
    functions = relationship("Function", back_populates="artifact", cascade="all, delete-orphan")
    testcases = relationship("TestCase", back_populates="artifact", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_stage_artifacts_project_stage", "project_id", "stage"),
    )


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("stage_artifacts.id"), index=True)
    approved: Mapped[bool] = mapped_column(default=False)
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    project = relationship("Project", back_populates="reviews")
    artifact = relationship("StageArtifact", back_populates="reviews")


class PipelineLog(Base):
    __tablename__ = "pipeline_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    level: Mapped[str] = mapped_column(String(16), default="INFO")
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class FunctionalRequirement(Base):
    """功能需求（需求分析阶段）"""
    __tablename__ = "functional_requirements"

    id: Mapped[int] = mapped_column(primary_key=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("stage_artifacts.id"), index=True)
    req_id: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    artifact = relationship("StageArtifact", back_populates="functional_requirements")

    __table_args__ = (
        Index("ix_func_req_artifact_priority", "artifact_id", "priority"),
    )


class Ambiguity(Base):
    """模糊点（需求分析阶段）"""
    __tablename__ = "ambiguities"

    id: Mapped[int] = mapped_column(primary_key=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("stage_artifacts.id"), index=True)
    amb_id: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    artifact = relationship("StageArtifact", back_populates="ambiguities")


class Risk(Base):
    """风险项（需求分析阶段）"""
    __tablename__ = "risks"

    id: Mapped[int] = mapped_column(primary_key=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("stage_artifacts.id"), index=True)
    risk_id: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    artifact = relationship("StageArtifact", back_populates="risks")


class Module(Base):
    """系统模块（概要设计阶段）"""
    __tablename__ = "modules"

    id: Mapped[int] = mapped_column(primary_key=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("stage_artifacts.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    responsibility: Mapped[str | None] = mapped_column(Text, nullable=True)
    dependencies: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    artifact = relationship("StageArtifact", back_populates="modules")


class DatabaseTable(Base):
    """数据库表设计（概要设计阶段）"""
    __tablename__ = "database_tables"

    id: Mapped[int] = mapped_column(primary_key=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("stage_artifacts.id"), index=True)
    table_name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    ddl: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    artifact = relationship("StageArtifact", back_populates="database_tables")


class API(Base):
    """API 接口定义（概要设计阶段）"""
    __tablename__ = "apis"

    id: Mapped[int] = mapped_column(primary_key=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("stage_artifacts.id"), index=True)
    method: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    artifact = relationship("StageArtifact", back_populates="apis")

    __table_args__ = (
        Index("ix_apis_method_path", "method", "path"),
    )


class DataStructure(Base):
    """数据结构（详细设计阶段）"""
    __tablename__ = "data_structures"

    id: Mapped[int] = mapped_column(primary_key=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("stage_artifacts.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    type: Mapped[str] = mapped_column(String(32))
    definition: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    artifact = relationship("StageArtifact", back_populates="data_structures")


class Function(Base):
    """函数接口（详细设计阶段）"""
    __tablename__ = "functions"

    id: Mapped[int] = mapped_column(primary_key=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("stage_artifacts.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    signature: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    module: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    artifact = relationship("StageArtifact", back_populates="functions")


class TestCase(Base):
    """测试用例（最重要的规范化表）"""
    __tablename__ = "testcases"

    id: Mapped[int] = mapped_column(primary_key=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("stage_artifacts.id"), index=True)
    case_id: Mapped[str] = mapped_column(String(32), index=True)
    module: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(16), index=True)
    priority: Mapped[str] = mapped_column(String(8), index=True)
    preconditions: Mapped[str] = mapped_column(Text)
    steps: Mapped[str] = mapped_column(Text)
    expected: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    artifact = relationship("StageArtifact", back_populates="testcases")

    __table_args__ = (
        Index("ix_testcases_type_priority", "type", "priority"),
        Index("ix_testcases_artifact_module", "artifact_id", "module"),
    )


engine = create_engine(f"sqlite:///{config.APP_DB}", future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db():
    """初始化数据库（创建所有表）"""
    Base.metadata.create_all(engine)


def migrate_from_v1():
    """从旧版本迁移数据：将 meta_json 中的数据迁移到规范化表"""
    import json

    with SessionLocal() as session:
        artifacts = session.query(StageArtifact).filter(
            StageArtifact.meta_json.isnot(None)
        ).all()

        for art in artifacts:
            meta = art.meta_json
            if not meta:
                continue

            if art.stage == "requirement":
                for fr in meta.get("functional_requirements", []):
                    if not session.query(FunctionalRequirement).filter_by(
                        artifact_id=art.id, req_id=fr.get("id")
                    ).first():
                        session.add(FunctionalRequirement(
                            artifact_id=art.id,
                            req_id=fr.get("id", ""),
                            description=fr.get("desc", ""),
                            priority=fr.get("priority", "P2")
                        ))

                for amb in meta.get("ambiguities", []):
                    if not session.query(Ambiguity).filter_by(
                        artifact_id=art.id, amb_id=amb.get("id")
                    ).first():
                        session.add(Ambiguity(
                            artifact_id=art.id,
                            amb_id=amb.get("id", ""),
                            description=amb.get("desc", ""),
                            severity=amb.get("severity", "中")
                        ))

                for risk in meta.get("risks", []):
                    if not session.query(Risk).filter_by(
                        artifact_id=art.id, risk_id=risk.get("id")
                    ).first():
                        session.add(Risk(
                            artifact_id=art.id,
                            risk_id=risk.get("id", ""),
                            description=risk.get("desc", ""),
                            severity=risk.get("severity", "中")
                        ))

            elif art.stage == "hld":
                for mod_name in meta.get("modules", []):
                    if not session.query(Module).filter_by(
                        artifact_id=art.id, name=mod_name
                    ).first():
                        session.add(Module(artifact_id=art.id, name=mod_name))

                for tbl_name in meta.get("tables", []):
                    if not session.query(DatabaseTable).filter_by(
                        artifact_id=art.id, table_name=tbl_name
                    ).first():
                        session.add(DatabaseTable(artifact_id=art.id, table_name=tbl_name))

                for api in meta.get("apis", []):
                    if not session.query(API).filter_by(
                        artifact_id=art.id, method=api.get("method"), path=api.get("path")
                    ).first():
                        session.add(API(
                            artifact_id=art.id,
                            method=api.get("method", "GET"),
                            path=api.get("path", ""),
                            description=api.get("desc", "")
                        ))

            elif art.stage == "lld":
                for ds_name in meta.get("data_structures", []):
                    if not session.query(DataStructure).filter_by(
                        artifact_id=art.id, name=ds_name
                    ).first():
                        session.add(DataStructure(artifact_id=art.id, name=ds_name, type="struct"))

                for func in meta.get("functions", []):
                    if not session.query(Function).filter_by(
                        artifact_id=art.id, name=func.get("name")
                    ).first():
                        session.add(Function(
                            artifact_id=art.id,
                            name=func.get("name", ""),
                            signature=func.get("sig", ""),
                            description=func.get("desc", "")
                        ))

            elif art.stage == "testcase":
                for tc in meta.get("testcases", []):
                    if not session.query(TestCase).filter_by(
                        artifact_id=art.id, case_id=tc.get("id")
                    ).first():
                        session.add(TestCase(
                            artifact_id=art.id,
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
        print("数据迁移完成")
