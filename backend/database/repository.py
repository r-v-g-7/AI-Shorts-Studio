import datetime
from typing import List, Optional, Dict, Any
from backend.database.connection import get_db


class JobRepository:
    @staticmethod
    def create_job(
        job_id: str,
        title: str,
        topic: str,
        visual_style: str,
        status: str,
        manifest_path: str
    ) -> Dict[str, Any]:
        now = datetime.datetime.utcnow().isoformat()
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO project_jobs (id, title, topic, visual_style, status, manifest_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, title, topic, visual_style, status, manifest_path, now, now)
            )
        return JobRepository.get_job(job_id)

    @staticmethod
    def get_job(job_id: str) -> Optional[Dict[str, Any]]:
        with get_db() as conn:
            row = conn.execute("SELECT * FROM project_jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                return None
            job_dict = dict(row)

            # Fetch scenes
            scenes = conn.execute(
                "SELECT * FROM scenes WHERE job_id = ? ORDER BY scene_index ASC",
                (job_id,)
            ).fetchall()
            job_dict["scenes"] = [dict(s) for s in scenes]
            return job_dict

    @staticmethod
    def list_jobs(skip: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM project_jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, skip)
            ).fetchall()
            jobs = []
            for r in rows:
                job_dict = dict(r)
                scenes = conn.execute(
                    "SELECT * FROM scenes WHERE job_id = ? ORDER BY scene_index ASC",
                    (job_dict["id"],)
                ).fetchall()
                job_dict["scenes"] = [dict(s) for s in scenes]
                jobs.append(job_dict)
            return jobs

    @staticmethod
    def update_job_status(job_id: str, status: str, error_message: Optional[str] = None, output_video_path: Optional[str] = None):
        now = datetime.datetime.utcnow().isoformat()
        with get_db() as conn:
            if output_video_path:
                conn.execute(
                    """
                    UPDATE project_jobs
                    SET status = ?, error_message = ?, output_video_path = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, error_message, output_video_path, now, job_id)
                )
            else:
                conn.execute(
                    """
                    UPDATE project_jobs
                    SET status = ?, error_message = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, error_message, now, job_id)
                )
        return JobRepository.get_job(job_id)

    @staticmethod
    def set_scenes(job_id: str, scenes: List[Dict[str, Any]]):
        with get_db() as conn:
            conn.execute("DELETE FROM scenes WHERE job_id = ?", (job_id,))
            for s in scenes:
                conn.execute(
                    """
                    INSERT INTO scenes (job_id, scene_index, narration_text, visual_prompt, duration_seconds, clip_path, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        s.get("index", 0),
                        s.get("narration", ""),
                        s.get("visual_prompt", ""),
                        s.get("duration_seconds", 5.0),
                        s.get("clip_file"),
                        "READY" if s.get("is_validated") else "PENDING"
                    )
                )
        return JobRepository.get_job(job_id)

    @staticmethod
    def add_log(job_id: str, level: str, stage: str, message: str):
        now = datetime.datetime.utcnow().isoformat()
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO job_logs (job_id, level, stage, message, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, level.upper(), stage.upper(), message, now)
            )

    @staticmethod
    def get_logs(job_id: str) -> List[Dict[str, Any]]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM job_logs WHERE job_id = ? ORDER BY timestamp ASC",
                (job_id,)
            ).fetchall()
            return [dict(r) for r in rows]
