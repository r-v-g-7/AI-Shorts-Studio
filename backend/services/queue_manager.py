import time
import logging
import datetime
import threading
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field, asdict

from backend.config import settings
from backend.schemas.job_manifest import JobStatus
from backend.services.job_service import JobService
from backend.database.repository import JobRepository
from backend.services.pipeline_controller import PipelineController

logger = logging.getLogger(__name__)


@dataclass
class QueueItem:
    job_id: str
    action: str = "UP_TO_VISUALS"  # UP_TO_VISUALS, RENDER, FULL_PIPELINE
    status: str = "QUEUED"          # QUEUED, RUNNING, COMPLETED, FAILED, CANCELLED
    current_stage: str = "PENDING"
    progress_percent: int = 0
    enqueued_at: str = field(default_factory=lambda: datetime.datetime.utcnow().isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    options: Dict[str, Any] = field(default_factory=dict)


class ProductionQueueManager:
    """
    Automated Sequential Production Queue for YouTube Shorts Studio.
    Enforces strict sequential single-job execution to protect HP Victus 16GB RAM
    and RTX 3050 VRAM resources. Supports queuing, cancellation, idempotency,
    and asset-preserving retries.
    """

    _instance: Optional["ProductionQueueManager"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._queue: List[QueueItem] = []
        self._history: List[QueueItem] = []
        self._current_job: Optional[QueueItem] = None
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._queue_lock = threading.Lock()
        self._wake_event = threading.Event()
        self._start_worker()

    @classmethod
    def get_instance(cls) -> "ProductionQueueManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _start_worker(self):
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._stop_event.clear()
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="ProductionQueueWorker",
                daemon=True
            )
            self._worker_thread.start()
            logger.info("ProductionQueueWorker thread started.")

    def enqueue_job(
        self,
        job_id: str,
        action: str = "UP_TO_VISUALS",
        options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Enqueues a job for sequential processing.
        Prevents duplicate execution of an already queued or actively running job.
        """
        with self._queue_lock:
            # 1. Prevent duplicate active execution
            if self._current_job and self._current_job.job_id == job_id and self._current_job.status == "RUNNING":
                return {
                    "status": "error",
                    "message": f"Job '{job_id}' is already actively running.",
                    "queue_item": asdict(self._current_job)
                }

            for item in self._queue:
                if item.job_id == job_id:
                    return {
                        "status": "error",
                        "message": f"Job '{job_id}' is already in the queue waiting to be processed.",
                        "queue_item": asdict(item)
                    }

            # 2. Create and add QueueItem
            item = QueueItem(
                job_id=job_id,
                action=action,
                status="QUEUED",
                current_stage="ENQUEUED",
                progress_percent=0,
                options=options or {}
            )
            self._queue.append(item)
            logger.info(f"Enqueued job '{job_id}' (action={action}). Queue length: {len(self._queue)}")

            # Update job manifest / SQLite status if DRAFT
            job = JobService.get_job(job_id)
            if job and job.get("status") in ("DRAFT", "FAILED"):
                JobRepository.add_log(job_id, "INFO", "QUEUE", f"Job added to sequential production queue (action={action}).")

        # Wake worker
        self._wake_event.set()
        return {
            "status": "success",
            "message": f"Job '{job_id}' enqueued successfully.",
            "queue_position": len(self._queue),
            "queue_item": asdict(item)
        }

    def cancel_job(self, job_id: str) -> bool:
        """
        Cancels a job in the queue or flags the currently running job for cancellation.
        """
        with self._queue_lock:
            # If in queue, mark CANCELLED and move to history
            for idx, item in enumerate(self._queue):
                if item.job_id == job_id:
                    item.status = "CANCELLED"
                    item.completed_at = datetime.datetime.utcnow().isoformat()
                    item.error_message = "Cancelled by user before execution."
                    self._history.append(self._queue.pop(idx))
                    JobRepository.add_log(job_id, "WARN", "QUEUE", "Job cancelled by user while in queue.")
                    JobService.update_job_status(job_id, JobStatus.DRAFT, error_message="Cancelled by user.")
                    return True

            # If actively running
            if self._current_job and self._current_job.job_id == job_id:
                self._current_job.status = "CANCELLED"
                self._current_job.error_message = "Cancelled by user during execution."
                JobRepository.add_log(job_id, "WARN", "QUEUE", "Running job flagged for cancellation.")
                return True

        return False

    def retry_job(self, job_id: str, action: str = "UP_TO_VISUALS") -> Dict[str, Any]:
        """
        Retries a failed job by clearing error state and re-enqueueing it.
        Preserves all completed intermediate assets on disk.
        """
        with self._queue_lock:
            # Remove any previous failed/cancelled entry from history
            self._history = [h for h in self._history if h.job_id != job_id]

        JobRepository.update_job_status(job_id, JobStatus.DRAFT.value, error_message=None)
        JobRepository.add_log(job_id, "INFO", "QUEUE", "Retrying failed job. Preserving intermediate assets...")
        return self.enqueue_job(job_id, action=action)

    def get_status(self) -> Dict[str, Any]:
        """Returns the current queue status, actively executing job, and queue length."""
        with self._queue_lock:
            return {
                "active_job": asdict(self._current_job) if self._current_job else None,
                "queued_count": len(self._queue),
                "queued_jobs": [asdict(item) for item in self._queue],
                "recent_history": [asdict(item) for item in self._history[-10:]]
            }

    def _worker_loop(self):
        """Sequential single-threaded worker loop."""
        while not self._stop_event.is_set():
            job_to_run: Optional[QueueItem] = None

            with self._queue_lock:
                if self._queue:
                    job_to_run = self._queue.pop(0)
                    job_to_run.status = "RUNNING"
                    job_to_run.started_at = datetime.datetime.utcnow().isoformat()
                    self._current_job = job_to_run

            if not job_to_run:
                # Wait for wake event or timeout
                self._wake_event.wait(timeout=1.0)
                self._wake_event.clear()
                continue

            # Process job
            try:
                self._execute_queue_item(job_to_run)
            except Exception as e:
                logger.error(f"Worker exception processing job '{job_to_run.job_id}': {e}", exc_info=True)
                job_to_run.status = "FAILED"
                job_to_run.error_message = str(e)
                job_to_run.completed_at = datetime.datetime.utcnow().isoformat()
                try:
                    JobRepository.update_job_status(job_to_run.job_id, JobStatus.FAILED.value, error_message=str(e))
                except Exception:
                    logger.exception("Could not persist FAILED status for job '%s'", job_to_run.job_id)
            finally:
                with self._queue_lock:
                    if job_to_run.status != "CANCELLED":
                        if job_to_run.status != "FAILED":
                            job_to_run.status = "COMPLETED"
                            job_to_run.progress_percent = 100
                        job_to_run.completed_at = datetime.datetime.utcnow().isoformat()
                    self._history.append(job_to_run)
                    self._current_job = None

    def resume_job(self, job_id: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Resumes a job from its current stage:
        - Scans storage/jobs/<job_id>/incoming_clips/ for existing scene_*.mp4 files.
        - Enqueues GENERATE_FULL_VIDEO so only missing scenes are generated, or jumps
          straight to COMPOSITING if all 4 scene clips already exist.
        """
        with self._queue_lock:
            self._history = [h for h in self._history if h.job_id != job_id]
        JobRepository.add_log(job_id, "INFO", "QUEUE", "Resuming job pipeline (preserving existing clips in incoming_clips/)...")
        return self.enqueue_job(job_id, action="GENERATE_FULL_VIDEO", options=options)

    def process_job(self, item: QueueItem):
        """Alias for _execute_queue_item."""
        return self._execute_queue_item(item)

    def _execute_queue_item(self, item: QueueItem):
        job_id = item.job_id
        action = item.action
        options = item.options or {}

        logger.info(f"ProductionQueueWorker: Starting job '{job_id}' (action={action})")

        # Action: GENERATE_FULL_VIDEO (1-Click Complete Pipeline & Auto-Resume)
        if action == "GENERATE_FULL_VIDEO":
            if item.status == "CANCELLED":
                return

            item.current_stage = "SCRIPT"
            item.progress_percent = 15
            script_req = options.get("script_request")
            PipelineController.execute_script_stage(job_id, generation_request=script_req)

            if item.status == "CANCELLED":
                return

            item.current_stage = "AUDIO"
            item.progress_percent = 30
            PipelineController.execute_audio_stage(
                job_id,
                voice=options.get("voice"),
                speed=options.get("speed")
            )

            if item.status == "CANCELLED":
                return

            item.current_stage = "SUBTITLES"
            item.progress_percent = 45
            PipelineController.execute_subtitles_stage(job_id)

            if item.status == "CANCELLED":
                return

            # Scan incoming_clips/ for existing scene_*.mp4 files before starting visuals
            from backend.services.scene_intake_service import SceneIntakeService
            manifest = JobService.load_manifest(job_id)
            total_scenes = len(manifest.scenes) if (manifest and manifest.scenes) else 4
            existing_indices, missing_indices = SceneIntakeService.get_existing_and_missing_scenes(
                job_id, total_scenes=total_scenes
            )

            if len(missing_indices) == 0:
                # All 4 scenes already exist in incoming_clips/ -> jump straight to COMPOSITING
                SceneIntakeService.scan_and_ingest(job_id)
                item.current_stage = "COMPOSITING"
                item.progress_percent = 80
                JobService.update_job_status(job_id, JobStatus.COMPOSITING)
                JobRepository.add_log(
                    job_id,
                    "INFO",
                    "QUEUE",
                    f"All {total_scenes} clips found in incoming_clips/; jumping straight to COMPOSITING."
                )
            else:
                first_missing = missing_indices[0] + 1
                item.current_stage = f"GENERATING_FLOW_VISUALS (Scene {first_missing}/{total_scenes})"
                item.progress_percent = 50 + int((len(existing_indices) / max(1, total_scenes)) * 30)
                provider_override = options.get("provider_override")

                def _on_scene_progress(stage_label: str, scene_num: int, total_cnt: int):
                    item.current_stage = stage_label
                    item.progress_percent = 50 + int((scene_num / max(1, total_cnt)) * 30)

                generated_clips = PipelineController.execute_video_generation_stage(
                    job_id,
                    provider_override=provider_override,
                    stage_callback=_on_scene_progress
                )

                if item.status == "CANCELLED":
                    return

                manifest = JobService.load_manifest(job_id)
                if not manifest or len(generated_clips) < len(manifest.scenes):
                    item.current_stage = "AWAITING_CLIPS"
                    item.progress_percent = 70
                    JobService.update_job_status(job_id, JobStatus.AWAITING_CLIPS)
                    logger.info(
                        f"ProductionQueueWorker: Job '{job_id}' paused at AWAITING_CLIPS "
                        f"({len(generated_clips)}/{len(manifest.scenes) if manifest else 4} scenes ready). "
                        "DownloadsWatcher remains active to auto-ingest remaining clips."
                    )
                    return

            item.current_stage = "COMPOSITING"
            item.progress_percent = 85
            JobService.update_job_status(job_id, JobStatus.COMPOSITING)
            bgm_path = options.get("bgm_path") or (str(settings.DEFAULT_BGM_PATH) if settings.DEFAULT_BGM_PATH.exists() else None)
            quality_mode_val = options.get("quality_mode", "BALANCED")
            from backend.schemas.preset_schema import QualityMode
            q_mode = QualityMode(quality_mode_val) if quality_mode_val in QualityMode.__members__ else QualityMode.BALANCED

            from backend.services.video_render_service import VideoRenderService
            render_res = VideoRenderService.render_final_video(
                job_id=job_id,
                bgm_path=bgm_path,
                quality_mode=q_mode
            )
            if render_res.get("status") == "awaiting_clips":
                item.current_stage = "AWAITING_CLIPS"
                item.progress_percent = 70
                JobService.update_job_status(job_id, JobStatus.AWAITING_CLIPS)
                logger.info(f"ProductionQueueWorker: Job '{job_id}' held at AWAITING_CLIPS by pre-render sanity check.")
                return

            item.current_stage = "QC_VERIFIED"
            item.progress_percent = 100
            logger.info(f"ProductionQueueWorker: 1-Click job '{job_id}' successfully finished.")
            return

        # Action: UP_TO_VISUALS
        if action in ("UP_TO_VISUALS", "FULL_PIPELINE"):
            if item.status == "CANCELLED":
                return

            item.current_stage = "SCRIPT"
            item.progress_percent = 15
            script_req = options.get("script_request")
            PipelineController.execute_script_stage(job_id, generation_request=script_req)

            if item.status == "CANCELLED":
                return

            item.current_stage = "AUDIO"
            item.progress_percent = 40
            PipelineController.execute_audio_stage(
                job_id,
                voice=options.get("voice"),
                speed=options.get("speed")
            )

            if item.status == "CANCELLED":
                return

            item.current_stage = "SUBTITLES"
            item.progress_percent = 65
            PipelineController.execute_subtitles_stage(job_id)

            item.current_stage = "VISUALS_READY"
            item.progress_percent = 70

        # Action: RENDER (or second half of FULL_PIPELINE)
        if action in ("RENDER", "FULL_PIPELINE"):
            if item.status == "CANCELLED":
                return

            # Check if scenes have clips
            manifest = JobService.load_manifest(job_id)
            if not manifest:
                raise ValueError(f"Job manifest for '{job_id}' not found.")

            # Ingest any matching clips from incoming folder first
            item.current_stage = "INGESTING_CLIPS"
            item.progress_percent = 75
            PipelineController.scan_job_clips(job_id)

            manifest = JobService.load_manifest(job_id)
            unassigned = [s.index for s in manifest.scenes if not s.clip_file]
            if unassigned and action == "FULL_PIPELINE":
                # Automatically generate missing clips using video generator
                item.current_stage = "GENERATING_VIDEO"
                item.progress_percent = 80
                PipelineController.execute_video_generation_stage(
                    job_id,
                    provider_override=options.get("provider_override")
                )

            item.current_stage = "RENDERING"
            item.progress_percent = 85
            bgm_path = options.get("bgm_path") or (str(settings.DEFAULT_BGM_PATH) if settings.DEFAULT_BGM_PATH.exists() else None)
            quality_mode_val = options.get("quality_mode", "BALANCED")
            from backend.schemas.preset_schema import QualityMode
            q_mode = QualityMode(quality_mode_val) if quality_mode_val in QualityMode.__members__ else QualityMode.BALANCED

            from backend.services.video_render_service import VideoRenderService
            render_res = VideoRenderService.render_final_video(
                job_id=job_id,
                bgm_path=bgm_path,
                quality_mode=q_mode
            )
            if render_res.get("status") == "awaiting_clips":
                item.current_stage = "AWAITING_CLIPS"
                item.progress_percent = 70
                JobService.update_job_status(job_id, JobStatus.AWAITING_CLIPS)
                logger.info(f"ProductionQueueWorker: Job '{job_id}' held at AWAITING_CLIPS by pre-render sanity check.")
                return

            item.current_stage = "QC_VERIFIED"
            item.progress_percent = 100
            logger.info(f"ProductionQueueWorker: Job '{job_id}' successfully finished.")
