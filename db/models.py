"""SQLAlchemy 数据模型：项目、文档、分块、阶段产物、评审记录、日志。"""
from datetime import datetime

from sqlalchemy import (JSON, DateTime, Enum as SAEnum, ForeignKey, Integer,
                        String, Text, create_engine)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

import config


class Base(DeclarativeBase):
    pass


def now():
    return datetime.now()


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="created")
    # created / parsing / waiting_review / running / completed / failed
    current_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    documents = relationship("Document", back_populates="project", cascade="all, delete-orphan")
    artifacts = relationship("StageArtifact", back_populates="project", cascade="all, delete-orphan")
    reviews = relationship("Review", back_populates="project", cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    filename: Mapped[str] = mapped_column(String(300))
    stored_path: Mapped[str] = mapped_column(String(500))
    file_type: Mapped[str] = mapped_column(String(16))  # docx/pdf/md/txt
    status: Mapped[str] = mapped_column(String(32), default="uploaded")  # uploaded/parsed/failed
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    project = relationship("Project", back_populates="documents")
    chunks = relationship("Chunk", back_populates="document", cascade="all, delete-orphan")


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    seq: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str | None] = mapped_column(String(300), nullable=True)
    content: Mapped[str] = mapped_column(Text)

    document = relationship("Document", back_populates="chunks")


class StageArtifact(Base):
    """各阶段产物：markdown 文档 + 结构化元数据。"""
    __tablename__ = "stage_artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    stage: Mapped[str] = mapped_column(String(32))  # parse/requirement/hld/lld/testcase
    version: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(200))
    markdown: Mapped[str] = mapped_column(Text)
    meta_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending_review")
    # pending_review / approved / rejected
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    project = relationship("Project", back_populates="artifacts")
    reviews = relationship("Review", back_populates="artifact", cascade="all, delete-orphan")


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    artifact_id: Mapped[int] = mapped_column(ForeignKey("stage_artifacts.id"))
    approved: Mapped[bool] = mapped_column(default=False)
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    project = relationship("Project", back_populates="reviews")
    artifact = relationship("StageArtifact", back_populates="reviews")


class PipelineLog(Base):
    __tablename__ = "pipeline_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    level: Mapped[str] = mapped_column(String(16), default="INFO")
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


engine = create_engine(f"sqlite:///{config.APP_DB}", future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db():
    Base.metadata.create_all(engine)
