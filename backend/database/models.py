import datetime
from sqlalchemy import Column, String, Integer, Float, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from backend.database.connection import Base


class ProjectJob(Base):
    __tablename__ = "project_jobs"

    id = Column(String(36), primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    topic = Column(Text, nullable=False)
    visual_style = Column(String(50), nullable=False, default="tall_tale_3d")
    status = Column(String(50), nullable=False, default="DRAFT", index=True)
    error_message = Column(Text, nullable=True)
    manifest_path = Column(String(500), nullable=False)
    output_video_path = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)

    scenes = relationship("SceneRecord", back_populates="job", cascade="all, delete-orphan", order_by="SceneRecord.scene_index")
    logs = relationship("JobLogRecord", back_populates="job", cascade="all, delete-orphan", order_by="JobLogRecord.timestamp")


class SceneRecord(Base):
    __tablename__ = "scenes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(36), ForeignKey("project_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    scene_index = Column(Integer, nullable=False)
    narration_text = Column(Text, nullable=False)
    visual_prompt = Column(Text, nullable=False)
    duration_seconds = Column(Float, nullable=False, default=5.0)
    clip_path = Column(String(500), nullable=True)
    status = Column(String(50), nullable=False, default="PENDING")

    job = relationship("ProjectJob", back_populates="scenes")


class JobLogRecord(Base):
    __tablename__ = "job_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(36), ForeignKey("project_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    level = Column(String(20), nullable=False, default="INFO")
    stage = Column(String(50), nullable=False, default="GENERAL")
    message = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

    job = relationship("ProjectJob", back_populates="logs")
