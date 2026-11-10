import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Query, status, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.config import settings
from backend.database.connection import init_db
from backend.schemas.job_manifest import (
    JobCreateRequest,
    JobManifest,
    JobStatus,
    VisualStyle,
    DirectShortCreateRequest,
    DirectSceneItem
)
from backend.schemas.preset_schema import (
    PRODUCTION_PRESETS,
    ProductionPreset,
    QualityMode
)
from backend.schemas.consistency_schema import (
    VisualReferenceSpec,
    CharacterReference,
    EnvironmentReference
)
from backend.services.job_service import JobService
from backend.services.audio_service import AudioService
from backend.services.subtitle_service import SubtitleService
from backend.services.script_service import ScriptService
from backend.services.pipeline_controller import PipelineController
from backend.services.scene_prompt_manager import ScenePromptManager
from backend.services.scene_intake_service import SceneIntakeService
from backend.services.video_render_service import VideoRenderService
from backend.services.quality_control_service import QualityControlService
from backend.services.queue_manager import ProductionQueueManager
from backend.services.downloads_watcher import DownloadsWatcher
from backend.schemas.script_schema import ScriptGenerationRequest


class AudioSynthesisRequest(BaseModel):
    voice: Optional[str] = None
    speed: Optional[float] = 1.0
    text: Optional[str] = None


class ClipAssignRequest(BaseModel):
    scene_index: int
    filename: str


class RenderRequest(BaseModel):
    bgm_path: Optional[str] = None
    bgm_volume: Optional[float] = 0.18
    quality_mode: Optional[str] = "BALANCED"


class QueueEnqueueRequest(BaseModel):
    job_id: str
    action: Optional[str] = "UP_TO_VISUALS"
    options: Optional[Dict[str, Any]] = None


# Initialize SQLite database on startup
init_db()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Starts the non-blocking Downloads folder watcher with the API and stops it on shutdown."""
    watcher = DownloadsWatcher.get_instance()
    if settings.DOWNLOADS_WATCH_ENABLED:
        if settings.DOWNLOADS_DIR.exists():
            watcher.start()
        else:
            logging.getLogger(__name__).warning(
                f"Downloads folder '{settings.DOWNLOADS_DIR}' not found; watcher disabled."
            )
    try:
        yield
    finally:
        watcher.stop()


app = FastAPI(
    title="AI Faceless Shorts Studio API",
    description="Automated YouTube Shorts Production Studio Backend",
    version="1.0.0",
    lifespan=lifespan,
)

# Enable CORS for frontend development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health", tags=["System"])
def health_check():
    """Health check endpoint to verify backend operational status and config."""
    return {
        "status": "healthy",
        "environment": settings.ENVIRONMENT,
        "database": settings.DATABASE_URL,
        "storage_dir": str(settings.STORAGE_DIR),
        "hardware": {
            "acceleration": settings.HARDWARE_ACCELERATION,
            "max_vram_gb": settings.MAX_VRAM_GB,
            "max_ram_gb": settings.MAX_RAM_GB,
        }
    }


# ==========================================
# PRESETS API
# ==========================================

@app.get("/api/presets", tags=["Presets"])
def list_production_presets():
    """Returns available video duration and quality production presets."""
    return {
        "presets": list(PRODUCTION_PRESETS.values())
    }


# ==========================================
# QUEUE API
# ==========================================

@app.get("/api/queue/status", tags=["Queue"])
def get_queue_status():
    """Returns the current status of the sequential production queue."""
    qm = ProductionQueueManager.get_instance()
    return qm.get_status()


@app.get("/api/downloads-watcher/status", tags=["Queue"])
def get_downloads_watcher_status():
    """Live status of the Downloads folder watcher (target job, per-scene clip arrival, recent events)."""
    return DownloadsWatcher.get_instance().status()


@app.post("/api/queue/enqueue", tags=["Queue"])
def enqueue_job_to_queue(request: QueueEnqueueRequest):
    """Enqueues a job into the sequential queue worker."""
    qm = ProductionQueueManager.get_instance()
    res = qm.enqueue_job(
        job_id=request.job_id,
        action=request.action or "UP_TO_VISUALS",
        options=request.options
    )
    if res.get("status") == "error":
        raise HTTPException(status_code=400, detail=res.get("message"))
    return res


@app.post("/api/queue/cancel/{job_id}", tags=["Queue"])
def cancel_queued_job(job_id: str):
    """Cancels a job in the queue or flags active job for termination."""
    qm = ProductionQueueManager.get_instance()
    success = qm.cancel_job(job_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found in queue or running.")
    return {"status": "success", "message": f"Job '{job_id}' cancelled."}


@app.post("/api/queue/retry/{job_id}", tags=["Queue"])
def retry_failed_job(job_id: str):
    """Retries a failed job, preserving existing intermediate on-disk assets."""
    qm = ProductionQueueManager.get_instance()
    return qm.retry_job(job_id)


# ==========================================
# JOBS API
# ==========================================

@app.post("/api/jobs", status_code=status.HTTP_201_CREATED, tags=["Jobs"])
def create_new_job(request: JobCreateRequest):
    """Create a new Shorts production job with initialized state manifest."""
    return JobService.create_job(request)


class CreateAndGenerateRequest(BaseModel):
    title: str = Field(..., min_length=2, max_length=200)
    topic: str = Field(..., min_length=5, max_length=10000)
    visual_style: VisualStyle = VisualStyle.DEEP_DIVE_3D
    quality_mode: Optional[str] = "BALANCED"
    provider_override: Optional[str] = None


@app.post("/api/jobs/create-and-generate", status_code=status.HTTP_201_CREATED, tags=["Jobs"])
@app.post("/api/short/create", status_code=status.HTTP_201_CREATED, tags=["Jobs"])
def create_and_generate_job(request: CreateAndGenerateRequest):
    """
    1-Click Entry Point: Creates the project, resets/synchronizes watcher disk state
    to 0/4 so UI counters never start desynced, and immediately kicks off
    the full automated production pipeline (Script -> Audio -> Subtitles -> AI Video -> FFmpeg 9:16 Render -> QC).
    """
    job_create = JobCreateRequest(
        title=request.title,
        topic=request.topic,
        visual_style=request.visual_style
    )
    job = JobService.create_job(job_create)
    job_id = job["id"]

    # Ensure watcher state and disk state start cleanly at Scene 1 (0/4)
    DownloadsWatcher.get_instance().sync_job_disk_state(job_id, reset_clips=True)

    qm = ProductionQueueManager.get_instance()
    queue_res = qm.enqueue_job(
        job_id=job_id,
        action="GENERATE_FULL_VIDEO",
        options={
            "quality_mode": request.quality_mode or "BALANCED",
            "provider_override": request.provider_override
        }
    )

    return {
        "job": job,
        "queue": queue_res
    }


@app.post("/api/jobs/create-direct", status_code=status.HTTP_201_CREATED, tags=["Jobs"])
@app.post("/api/short/create_direct", status_code=status.HTTP_201_CREATED, tags=["Jobs"])
@app.post("/api/short/create-direct", status_code=status.HTTP_201_CREATED, tags=["Jobs"])
def create_direct_short_endpoint(request: DirectShortCreateRequest):
    """
    Direct Script & Visual Prompt Input Entry Point:
    Bypasses LLM rewriting, ingesting verbatim voiceover words and exact visual prompts.
    Supports Multilingual Neural Voices (English, Hindi, Bengali, Punjabi, Marathi, Haryanvi, Rajasthani)
    and 60s timeline budgeting.
    """
    job = PipelineController.create_direct_job(request)
    job_id = job["id"]

    # Ensure watcher state starts cleanly
    DownloadsWatcher.get_instance().sync_job_disk_state(job_id, reset_clips=True)

    qm = ProductionQueueManager.get_instance()
    queue_res = qm.enqueue_job(
        job_id=job_id,
        action="GENERATE_FULL_VIDEO",
        options={
            "quality_mode": request.quality_mode or "BALANCED",
            "provider_override": request.provider_override
        }
    )

    return {
        "job": job,
        "queue": queue_res
    }


@app.post("/api/jobs/{job_id}/generate-full-video", tags=["Pipeline"])
@app.post("/api/short/resume/{job_id}", tags=["Pipeline"])
def generate_full_video_endpoint(job_id: str, request: Optional[Dict[str, Any]] = None):
    """
    1-Click Resume / Re-run trigger for existing job:
    Validates on-disk scene clips first so UI counters never get desynced, then executes
    Script -> Humanized Hindi Voice -> ASS Subtitles -> Missing AI Video Clips -> FFmpeg 9:16 Render -> QC.
    """
    job = JobService.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    opts = request or {}
    reset_flag = bool(opts.get("reset_clips", False))
    sync_state = DownloadsWatcher.get_instance().sync_job_disk_state(job_id, reset_clips=reset_flag)

    qm = ProductionQueueManager.get_instance()
    res = qm.resume_job(job_id=job_id, options=opts)
    if res.get("status") == "error":
        raise HTTPException(status_code=400, detail=res.get("message"))
    return {**res, "disk_sync": sync_state}


class ShortResumeRequest(BaseModel):
    job_id: str
    reset_clips: Optional[bool] = False
    options: Optional[Dict[str, Any]] = None


@app.post("/api/short/resume", tags=["Pipeline"])
def resume_short_body_endpoint(request: ShortResumeRequest):
    """Body-based `/api/short/resume` endpoint that validates or resets disk state and resumes the job."""
    job = JobService.get_job(request.job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{request.job_id}' not found")

    sync_state = DownloadsWatcher.get_instance().sync_job_disk_state(
        request.job_id, reset_clips=bool(request.reset_clips)
    )
    qm = ProductionQueueManager.get_instance()
    res = qm.resume_job(job_id=request.job_id, options=request.options or {})
    if res.get("status") == "error":
        raise HTTPException(status_code=400, detail=res.get("message"))
    return {**res, "disk_sync": sync_state}


@app.post("/api/jobs/{job_id}/open-folder", tags=["Jobs"])
def open_job_folder(job_id: str):
    """Opens the job output folder in Windows File Explorer."""
    job_dir = JobService.get_job_directory(job_id)
    output_dir = job_dir / "output"
    target = output_dir if output_dir.exists() else job_dir
    try:
        import subprocess
        subprocess.Popen(["explorer.exe", str(target.resolve())])
        return {"status": "success", "path": str(target.resolve())}
    except Exception as e:
        return {"status": "error", "message": str(e), "path": str(target.resolve())}


@app.get("/api/jobs", tags=["Jobs"])
def list_all_jobs(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100)
):
    """List all production jobs ordered by creation timestamp."""
    skip_val = getattr(skip, "default", skip) if not isinstance(skip, int) else skip
    limit_val = getattr(limit, "default", limit) if not isinstance(limit, int) else limit
    return JobService.list_jobs(skip=skip_val, limit=limit_val)


@app.get("/api/jobs/{job_id}", tags=["Jobs"])
def get_job_by_id(job_id: str):
    """Retrieve full database record and scenes for a specific job."""
    job = JobService.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job with ID '{job_id}' not found")
    return job


@app.get("/api/jobs/{job_id}/manifest", tags=["Jobs"])
def get_job_manifest(job_id: str):
    """Retrieve full on-disk JSON manifest for a job."""
    manifest = JobService.load_manifest(job_id)
    if not manifest:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return manifest


@app.get("/api/jobs/{job_id}/logs", tags=["Jobs"])
def get_job_logs(job_id: str):
    """Retrieve all execution and audit logs for a job."""
    from backend.database.repository import JobRepository
    return JobRepository.get_logs(job_id)


# ==========================================
# SCRIPT & REFERENCE SHEET API
# ==========================================

@app.post("/api/jobs/{job_id}/generate-script", tags=["Script"])
def generate_job_script(job_id: str, request: Optional[ScriptGenerationRequest] = None):
    """Generates structured script, exports scene prompts and visual reference sheets."""
    try:
        script = PipelineController.execute_script_stage(job_id, generation_request=request)
        return {
            "status": "success",
            "job_id": job_id,
            "script": script.model_dump()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/jobs/{job_id}/reference-sheet", tags=["Consistency"])
def get_job_reference_sheet(job_id: str):
    """Retrieve the visual and character reference sheet for Google Flow / Veo."""
    job_dir = JobService.get_job_directory(job_id)
    ref_file = job_dir / "reference_sheet.json"
    if not ref_file.exists():
        manifest = JobService.load_manifest(job_id)
        if not manifest or not manifest.script_data:
            return {"status": "pending", "ready": False, "reference_sheet": None}
        from backend.schemas.script_schema import ShortsScript
        try:
            script = ShortsScript.model_validate(manifest.script_data)
            ScenePromptManager.export_prompts(job_id, script)
        except Exception:
            return {"status": "pending", "ready": False, "reference_sheet": None}

    if ref_file.exists():
        with open(ref_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {"status": "completed", "ready": True, "reference_sheet": data, **data}
    return {"status": "pending", "ready": False, "reference_sheet": None}


@app.get("/api/jobs/{job_id}/prompts", tags=["Scenes"])
def get_job_prompts(job_id: str):
    """Retrieve formatted production prompts for Google Flow / Veo."""
    job_dir = JobService.get_job_directory(job_id)
    prompts_file = job_dir / "prompts.json"
    if not prompts_file.exists():
        raise HTTPException(status_code=404, detail="Prompts not yet generated for this job.")

    with open(prompts_file, "r", encoding="utf-8") as f:
        return json.load(f)


# ==========================================
# SCENE INTAKE API
# ==========================================

@app.post("/api/jobs/{job_id}/scan-clips", tags=["Scenes"])
def scan_incoming_clips(job_id: str):
    """Scans the job's incoming_clips folder and maps valid videos to scenes."""
    try:
        return SceneIntakeService.scan_and_ingest(job_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/jobs/{job_id}/assign-clip", tags=["Scenes"])
def assign_scene_clip(job_id: str, request: ClipAssignRequest):
    """Manually assign an incoming video clip to a specific scene index."""
    try:
        return SceneIntakeService.assign_clip_to_scene(
            job_id=job_id,
            scene_index=request.scene_index,
            source_filename=request.filename
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==========================================
# RENDER & QC API
# ==========================================

@app.post("/api/jobs/{job_id}/render", tags=["Render"])
def render_job_video(job_id: str, request: Optional[RenderRequest] = None):
    """Render the final 9:16 Shorts video using FFmpeg and burning ASS subtitles."""
    try:
        bgm = request.bgm_path if request else None
        vol = request.bgm_volume if request and request.bgm_volume is not None else 0.18
        mode_str = request.quality_mode if request and request.quality_mode else "BALANCED"
        q_mode = QualityMode(mode_str) if mode_str in QualityMode.__members__ else QualityMode.BALANCED
        return VideoRenderService.render_final_video(
            job_id,
            bgm_path=bgm,
            bgm_volume=vol,
            quality_mode=q_mode
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/jobs/{job_id}/qc", tags=["QualityControl"])
def get_job_quality_control(job_id: str):
    """Retrieve the multi-factor Quality Control report for the rendered video."""
    job_dir = JobService.get_job_directory(job_id)
    qc_file = job_dir / "output" / "quality_report.json"
    if not qc_file.exists():
        manifest = JobService.load_manifest(job_id)
        if manifest and manifest.final_video_file and Path(manifest.final_video_file).exists():
            report = QualityControlService.run_quality_control(job_id)
            data = report.model_dump()
            return {"status": "completed", "qc_ready": True, "report": data, **data}
        return {"status": "pending", "qc_ready": False, "report": None}

    with open(qc_file, "r", encoding="utf-8") as f:
        data = json.load(f)
        return {"status": "completed", "qc_ready": True, "report": data, **data}


@app.post("/api/jobs/{job_id}/qc", tags=["QualityControl"])
def run_job_quality_control(job_id: str):
    """Explicitly trigger Quality Control verification on the rendered video."""
    try:
        report = QualityControlService.run_quality_control(job_id)
        return report.model_dump()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/jobs/{job_id}/video", tags=["Render"])
def get_final_rendered_video(job_id: str, download: bool = False):
    """Serves the final rendered MP4 video file for preview or download."""
    job = JobService.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    output_file = Path(job.get("output_video_path") or "")
    if not output_file.exists():
        output_file = JobService.get_job_directory(job_id) / "output" / "final_short.mp4"

    if not output_file.exists():
        raise HTTPException(status_code=404, detail="Final video has not been rendered yet.")

    safe_title = "".join(c for c in job.get("title", "Short") if c.isalnum() or c in (" ", "_", "-")).strip()
    safe_filename = f"{safe_title.replace(' ', '_')}_{job_id[:8]}.mp4"

    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{safe_filename}"'

    return FileResponse(
        str(output_file),
        media_type="video/mp4",
        filename=safe_filename if download else None,
        headers=headers
    )


# ==========================================
# AUDIO & SUBTITLES API
# ==========================================

@app.get("/api/audio/voices", tags=["Audio"])
def get_voices():
    """List all available Kokoro voices."""
    return {
        "voices": AudioService.get_available_voices(),
        "default": settings.KOKORO_VOICE
    }


@app.post("/api/jobs/{job_id}/generate-audio", tags=["Audio"])
def generate_job_audio(job_id: str, request: Optional[AudioSynthesisRequest] = None):
    """Synthesize narration WAV, generate word timestamps, and compile ASS subtitles."""
    job = JobService.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    manifest = JobService.load_manifest(job_id)
    if not manifest:
        raise HTTPException(status_code=404, detail=f"Manifest for job '{job_id}' not found")

    narration_text = ""
    if request and request.text and request.text.strip():
        narration_text = request.text.strip()
    elif manifest.scenes:
        narration_text = " ".join(s.narration for s in manifest.scenes)
    else:
        narration_text = job["topic"]

    voice = request.voice if request else None
    speed = request.speed if request else None

    audio_res = AudioService.synthesize_speech(
        text=narration_text,
        voice=voice,
        speed=speed,
        job_id=job_id
    )

    sub_path = SubtitleService.generate_ass_subtitles(
        word_timestamps=audio_res["timestamps"],
        job_id=job_id
    )

    return {
        "status": "success",
        "job_id": job_id,
        "audio_file": audio_res["audio_path"],
        "subtitle_file": sub_path,
        "duration_seconds": audio_res["duration_seconds"],
        "sample_rate": audio_res["sample_rate"],
        "word_count": audio_res["word_count"],
        "inference_time_seconds": audio_res["inference_time_seconds"],
        "timestamps": audio_res["timestamps"]
    }


# Mount static frontend
frontend_dir = settings.BASE_DIR / "frontend"
if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="frontend_static")
    src_dir = frontend_dir / "src"
    if src_dir.exists():
        app.mount("/src", StaticFiles(directory=str(src_dir)), name="frontend_src")

    @app.get("/", include_in_schema=False)
    def serve_frontend():
        return FileResponse(frontend_dir / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host=settings.BACKEND_HOST,
        port=settings.BACKEND_PORT,
        reload=settings.DEBUG
    )
