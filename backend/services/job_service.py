import json
import uuid
import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from backend.config import settings
from backend.database.repository import JobRepository
from backend.schemas.job_manifest import (
    JobManifest,
    JobStatus,
    VisualStyle,
    SceneSpec,
    JobCreateRequest
)


class JobService:
    @staticmethod
    def get_job_directory(job_id: str) -> Path:
        """Returns the dedicated filesystem folder for a specific job."""
        job_dir = settings.JOBS_STORAGE_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        # Create standard subfolders
        (job_dir / "audio").mkdir(exist_ok=True)
        (job_dir / "subtitles").mkdir(exist_ok=True)
        (job_dir / "scenes").mkdir(exist_ok=True)
        (job_dir / "output").mkdir(exist_ok=True)
        return job_dir

    @staticmethod
    def get_manifest_path(job_id: str) -> Path:
        return JobService.get_job_directory(job_id) / "manifest.json"

    @classmethod
    def save_manifest(cls, manifest: JobManifest) -> Path:
        manifest.updated_at = datetime.datetime.utcnow().isoformat()
        manifest_path = cls.get_manifest_path(manifest.job_id)
        with open(manifest_path, "w", encoding="utf-8") as f:
            f.write(manifest.model_dump_json(indent=2))
        return manifest_path

    @classmethod
    def load_manifest(cls, job_id: str) -> Optional[JobManifest]:
        manifest_path = cls.get_manifest_path(job_id)
        if not manifest_path.exists():
            return None
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return JobManifest.model_validate(data)

    @classmethod
    def create_job(cls, request: JobCreateRequest) -> Dict[str, Any]:
        job_id = str(uuid.uuid4())
        cls.get_job_directory(job_id)
        manifest_path = cls.get_manifest_path(job_id)

        # 1. Initialize Pydantic Manifest
        manifest = JobManifest(
            job_id=job_id,
            title=request.title,
            topic=request.topic,
            visual_style=request.visual_style,
            status=JobStatus.DRAFT
        )
        cls.save_manifest(manifest)

        # 2. Persist in SQLite
        db_job = JobRepository.create_job(
            job_id=job_id,
            title=request.title,
            topic=request.topic,
            visual_style=request.visual_style.value,
            status=JobStatus.DRAFT.value,
            manifest_path=str(manifest_path)
        )

        JobRepository.add_log(job_id, "INFO", "INITIALIZATION", f"Job created: '{request.title}' [{job_id}]")
        return db_job

    @classmethod
    def get_job(cls, job_id: str) -> Optional[Dict[str, Any]]:
        return JobRepository.get_job(job_id)

    @classmethod
    def list_jobs(cls, skip: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
        return JobRepository.list_jobs(skip=skip, limit=limit)

    @classmethod
    def update_job_status(
        cls,
        job_id: str,
        status: JobStatus,
        error_message: Optional[str] = None,
        output_video_path: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        job = JobRepository.update_job_status(
            job_id=job_id,
            status=status.value,
            error_message=error_message,
            output_video_path=output_video_path
        )
        if not job:
            return None

        # Update disk manifest
        manifest = cls.load_manifest(job_id)
        if manifest:
            manifest.status = status
            manifest.error = error_message
            if output_video_path:
                manifest.final_video_file = output_video_path
            cls.save_manifest(manifest)

        level = "ERROR" if status == JobStatus.FAILED else "INFO"
        JobRepository.add_log(
            job_id,
            level,
            "STATUS_CHANGE",
            f"Status updated to {status.value}" + (f": {error_message}" if error_message else "")
        )
        return job

    @classmethod
    def set_scenes(cls, job_id: str, scenes: List[SceneSpec]) -> Optional[Dict[str, Any]]:
        scenes_dicts = [s.model_dump() for s in scenes]
        job = JobRepository.set_scenes(job_id, scenes_dicts)

        manifest = cls.load_manifest(job_id)
        if manifest:
            manifest.scenes = scenes
            cls.save_manifest(manifest)

        JobRepository.add_log(job_id, "INFO", "SCENE_UPDATE", f"Registered {len(scenes)} scenes in job manifest.")
        return job

    @classmethod
    def get_logs(cls, job_id: str) -> List[Dict[str, Any]]:
        return JobRepository.get_logs(job_id)
