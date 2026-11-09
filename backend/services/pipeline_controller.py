import json
import logging
import traceback
from pathlib import Path
from typing import Optional, Dict, Any, List

from backend.config import settings
from backend.schemas.job_manifest import (
    JobStatus,
    JobManifest,
    SceneSpec,
    VisualStyle,
    JobCreateRequest
)
from backend.schemas.script_schema import (
    ScriptGenerationRequest,
    ShortsScript
)
from backend.services.job_service import JobService
from backend.services.script_service import ScriptService
from backend.services.audio_service import AudioService
from backend.services.subtitle_service import SubtitleService
from backend.services.scene_prompt_manager import ScenePromptManager
from backend.services.scene_intake_service import SceneIntakeService
from backend.services.video_render_service import VideoRenderService
from backend.database.repository import JobRepository

logger = logging.getLogger(__name__)


class PipelineController:
    """
    Central production controller for the AI Faceless Shorts Studio.
    Coordinates job lifecycle, validates intermediate assets, handles failure recovery,
    and supports idempotent resumption of production pipelines.
    """

    @classmethod
    def create_new_job(
        cls,
        topic: str,
        title: Optional[str] = None,
        visual_style: VisualStyle = VisualStyle.DEEP_DIVE_3D
    ) -> Dict[str, Any]:
        """Creates a new job and initializes its manifest and storage workspace."""
        clean_title = title.strip() if title and title.strip() else f"Short: {topic[:40].strip()}..."
        request = JobCreateRequest(
            title=clean_title,
            topic=topic.strip(),
            visual_style=visual_style
        )
        job = JobService.create_job(request)
        return job

    @classmethod
    def execute_script_stage(
        cls,
        job_id: str,
        generation_request: Optional[ScriptGenerationRequest] = None,
        force_regenerate: bool = False
    ) -> ShortsScript:
        """
        Stage 1: Generates or ingests the structured Shorts script.
        Populates scenes in SQLite and on-disk manifest.
        """
        job = JobService.get_job(job_id)
        if not job:
            raise ValueError(f"Job '{job_id}' not found.")

        job_dir = JobService.get_job_directory(job_id)
        script_file = job_dir / "script.json"

        # Check for completed asset (Resumption Check: reuse existing valid script file)
        if script_file.exists() and not force_regenerate:
            try:
                with open(script_file, "r", encoding="utf-8") as f:
                    cached_script = ShortsScript.model_validate(json.load(f))
                logger.info(f"Reusing existing script for job '{job_id}' from {script_file.name}")
                JobService.update_job_status(job_id, JobStatus.SCRIPT_READY)
                return cached_script
            except Exception as e:
                logger.warning(f"Existing script file {script_file.name} invalid ({e}); will regenerate.")

        try:
            # Build request if not provided or if topic is omitted
            if not generation_request or not generation_request.topic:
                req = ScriptGenerationRequest(
                    topic=job["topic"],
                    visual_style=generation_request.visual_style if generation_request else VisualStyle(job["visual_style"]),
                    target_duration=generation_request.target_duration if generation_request else 25.0
                )
            else:
                req = generation_request

            # Generate and validate
            script = ScriptService.generate_script(req)

            # Save script.json in job workspace
            with open(script_file, "w", encoding="utf-8") as f:
                f.write(script.model_dump_json(indent=2))

            # Export production prompts (JSON and human-readable TXT for Flow/Veo)
            ScenePromptManager.export_prompts(job_id, script)

            # Convert to SceneSpecs and update DB & manifest
            scene_specs: List[SceneSpec] = []
            for s in script.scenes:
                full_prompt = f"{s.visual_description}. Camera: {s.camera_movement}."
                scene_specs.append(
                    SceneSpec(
                        index=s.index,
                        narration=s.narration,
                        visual_prompt=full_prompt,
                        duration_seconds=s.estimated_duration_seconds,
                        camera_movement=s.camera_movement,
                        sound_effects=s.sound_effects,
                        is_validated=False
                    )
                )

            # Register scenes in DB and update manifest
            JobService.set_scenes(job_id, scene_specs)

            manifest = JobService.load_manifest(job_id)
            if manifest:
                manifest.title = script.title
                manifest.script_data = script.model_dump()
                manifest.status = JobStatus.SCRIPT_READY
                manifest.error = None
                JobService.save_manifest(manifest)

            JobRepository.update_job_status(job_id, JobStatus.SCRIPT_READY.value, error_message=None)
            JobRepository.add_log(
                job_id,
                "INFO",
                "SCRIPT_ENGINE",
                f"Generated and validated script '{script.title}' with {len(script.scenes)} scenes."
            )
            return script

        except Exception as e:
            err_msg = f"Script stage failed: {str(e)}"
            logger.error(err_msg, exc_info=True)
            cls._mark_job_failed(job_id, "SCRIPT_ENGINE", err_msg)
            raise

    @classmethod
    def execute_audio_stage(
        cls,
        job_id: str,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        force_regenerate: bool = False
    ) -> Dict[str, Any]:
        """
        Stage 2: Synthesizes narration audio and extracts word-level timestamps.
        Runs locally on CPU with zero VRAM.
        """
        job = JobService.get_job(job_id)
        if not job:
            raise ValueError(f"Job '{job_id}' not found.")

        job_dir = JobService.get_job_directory(job_id)
        wav_path = job_dir / "audio" / "narration.wav"
        words_path = job_dir / "audio" / "words.json"

        # Resumption Check: if WAV & words.json already exist and are valid, reuse them
        if wav_path.exists() and words_path.exists() and not force_regenerate:
            try:
                with open(words_path, "r", encoding="utf-8") as f:
                    words_data = json.load(f)
                if wav_path.stat().st_size > 1000 and words_data.get("timestamps"):
                    logger.info(f"Reusing existing narration audio for job '{job_id}' from {wav_path.name}")
                    JobService.update_job_status(job_id, JobStatus.AUDIO_READY)
                    return words_data
            except Exception as e:
                logger.warning(f"Existing audio files invalid ({e}); will regenerate.")

        try:
            # Set status to AUDIO_PROCESSING
            JobService.update_job_status(job_id, JobStatus.AUDIO_PROCESSING)

            # Determine complete narration text from script or scenes
            manifest = JobService.load_manifest(job_id)
            if not manifest or not manifest.scenes:
                raise ValueError("Cannot synthesize audio: No scenes found in manifest. Generate script first.")

            full_narration = " ".join(s.narration.strip() for s in manifest.scenes)

            # Call AudioService
            audio_result = AudioService.synthesize_speech(
                text=full_narration,
                voice=voice,
                speed=speed,
                job_id=job_id
            )

            # Update status to AUDIO_READY
            JobService.update_job_status(job_id, JobStatus.AUDIO_READY)
            return audio_result

        except Exception as e:
            err_msg = f"Audio stage failed: {str(e)}"
            logger.error(err_msg, exc_info=True)
            cls._mark_job_failed(job_id, "AUDIO_SYNTHESIS", err_msg)
            raise

    @classmethod
    def execute_subtitles_stage(
        cls,
        job_id: str,
        force_regenerate: bool = False
    ) -> str:
        """
        Stage 3: Compiles ASS subtitles with active yellow highlights from word timestamps.
        Advances status to VISUALS_PENDING (ready for scene clips ingest).
        """
        job = JobService.get_job(job_id)
        if not job:
            raise ValueError(f"Job '{job_id}' not found.")

        job_dir = JobService.get_job_directory(job_id)
        ass_path = job_dir / "subtitles" / "captions.ass"

        # Resumption Check: reuse existing valid ASS subtitles
        if ass_path.exists() and not force_regenerate and ass_path.stat().st_size > 100:
            logger.info(f"Reusing existing ASS subtitles for job '{job_id}' from {ass_path.name}")
            JobService.update_job_status(job_id, JobStatus.VISUALS_PENDING)
            return str(ass_path)

        try:
            manifest = JobService.load_manifest(job_id)
            if not manifest or not manifest.word_timestamps:
                raise ValueError("Cannot compile subtitles: No word timestamps found. Complete audio stage first.")

            # Compile ASS
            sub_file = SubtitleService.generate_ass_subtitles(
                word_timestamps=manifest.word_timestamps,
                job_id=job_id
            )

            # Advance to VISUALS_PENDING
            JobService.update_job_status(job_id, JobStatus.VISUALS_PENDING)
            return sub_file

        except Exception as e:
            err_msg = f"Subtitle stage failed: {str(e)}"
            logger.error(err_msg, exc_info=True)
            cls._mark_job_failed(job_id, "SUBTITLE_ENGINE", err_msg)
            raise

    @classmethod
    def run_pipeline_up_to_visuals(
        cls,
        job_id: str,
        script_request: Optional[ScriptGenerationRequest] = None,
        voice: Optional[str] = None,
        speed: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Executes all pipeline stages up to VISUALS_PENDING.
        Automatically resumes from the latest completed stage.
        """
        job = JobService.get_job(job_id)
        if not job:
            raise ValueError(f"Job '{job_id}' not found.")

        logger.info(f"Running pipeline for job '{job_id}', current status: {job['status']}")

        # Stage 1: Script
        script = cls.execute_script_stage(job_id, generation_request=script_request)

        # Stage 2: Audio
        audio = cls.execute_audio_stage(job_id, voice=voice, speed=speed)

        # Stage 3: Subtitles
        subtitles_file = cls.execute_subtitles_stage(job_id)

        updated_manifest = JobService.load_manifest(job_id)
        return {
            "status": "success",
            "job_id": job_id,
            "current_status": updated_manifest.status.value if updated_manifest else JobStatus.VISUALS_PENDING.value,
            "script_title": script.title,
            "scenes_count": len(script.scenes),
            "audio_duration_seconds": audio.get("total_duration_seconds") or audio.get("duration_seconds"),
            "subtitle_file": subtitles_file
        }

    @classmethod
    def scan_job_clips(cls, job_id: str) -> Dict[str, Any]:
        """Scans incoming clips, validates them, and matches to scenes."""
        return SceneIntakeService.scan_and_ingest(job_id)

    @classmethod
    def execute_render_stage(
        cls,
        job_id: str,
        bgm_path: Optional[str] = None,
        force_render: bool = False
    ) -> Dict[str, Any]:
        """
        Stage 4: Assembles all validated video scenes, syncs with narration audio,
        burns ASS animated subtitles, and outputs the final 1080x1920 MP4.
        """
        job = JobService.get_job(job_id)
        if not job:
            raise ValueError(f"Job '{job_id}' not found.")

        final_mp4 = JobService.get_job_directory(job_id) / "output" / "final_short.mp4"
        if final_mp4.exists() and not force_render and job["status"] == JobStatus.COMPLETED.value:
            logger.info(f"Reusing existing final video for job '{job_id}'")
            probe = SceneIntakeService.probe_video(final_mp4)
            return {
                "status": "success",
                "job_id": job_id,
                "video_path": str(final_mp4),
                "duration_seconds": probe.get("duration_seconds", 0.0),
                "reused_existing": True
            }

        return VideoRenderService.render_final_video(job_id, bgm_path=bgm_path)

    @classmethod
    def execute_video_generation_stage(
        cls,
        job_id: str,
        force_regenerate: bool = False,
        provider_override: Optional[str] = None,
        stage_callback: Optional[ Any] = None
    ) -> List[str]:
        """
        Stage 3.5: Automatically generates visual video clips for each scene
        using the configured video generation provider (Flow Browser CDP, Gemini / Veo, or Mock).
        Saves each generated clip directly to storage/jobs/{job_id}/scenes/scene_{index:02d}.mp4.
        If flow_browser is selected and Chrome CDP on port 9222 is unreachable, logs instructions
        and places the job cleanly in VISUALS_PENDING.
        """
        job = JobService.get_job(job_id)
        if not job:
            raise ValueError(f"Job '{job_id}' not found.")

        manifest = JobService.load_manifest(job_id)
        if not manifest or not manifest.scenes:
            raise ValueError(f"Job '{job_id}' has no scenes. Generate script first.")

        job_dir = JobService.get_job_directory(job_id)
        scenes_dir = job_dir / "scenes"
        scenes_dir.mkdir(parents=True, exist_ok=True)
        incoming_dir = job_dir / "incoming_clips"
        incoming_dir.mkdir(parents=True, exist_ok=True)

        from backend.services.video_generator import get_video_generator, FlowBrowserVideoGenerator
        from backend.services.flow_automation_service import (
            FlowAutomationService,
            CDPConnectionError,
            CHROME_CDP_LAUNCH_INSTRUCTIONS
        )

        generator = get_video_generator(provider_override=provider_override)
        total_scenes = len(manifest.scenes)

        if isinstance(generator, FlowBrowserVideoGenerator):
            if not FlowAutomationService.is_cdp_reachable():
                warn_msg = CHROME_CDP_LAUNCH_INSTRUCTIONS.format(cdp_url=settings.CDP_DEBUGGING_URL)
                logger.warning(warn_msg)
                JobRepository.add_log(job_id, "WARN", "FLOW_AUTOMATION", warn_msg)
                JobService.update_job_status(job_id, JobStatus.VISUALS_PENDING)
                return []

        JobRepository.add_log(
            job_id,
            "INFO",
            "VIDEO_GENERATOR",
            f"Starting automated video generation for {total_scenes} scenes using {generator.__class__.__name__}..."
        )

        generated_clips: List[str] = []
        for scene in manifest.scenes:
            scene_num = scene.index + 1
            clip_path = scenes_dir / f"scene_{scene_num:02d}.mp4"

            # Resumption check: reuse valid existing clip if not force_regenerate
            if clip_path.exists() and clip_path.stat().st_size > 1000 and not force_regenerate:
                probe = SceneIntakeService.probe_video(clip_path)
                if probe["is_valid"]:
                    logger.info(f"Reusing existing clip for scene {scene_num}: {clip_path.name}")
                    scene.clip_file = str(clip_path)
                    scene.is_validated = True
                    generated_clips.append(str(clip_path))
                    continue

            stage_label = (
                f"GENERATING_FLOW_VISUALS (Scene {scene_num}/{total_scenes})"
                if isinstance(generator, FlowBrowserVideoGenerator)
                else f"GENERATING_VIDEO (Scene {scene_num}/{total_scenes})"
            )
            if stage_callback:
                try:
                    stage_callback(stage_label, scene_num, total_scenes)
                except Exception:
                    pass

            JobRepository.add_log(job_id, "INFO", "VIDEO_GENERATOR", f"{stage_label}: Generating video footage...")
            prompt = scene.visual_prompt

            try:
                generator.generate_scene_clip(
                    scene_prompt=prompt,
                    duration_seconds=scene.duration_seconds,
                    output_path=clip_path,
                    aspect_ratio=settings.VIDEO_ASPECT_RATIO,
                    job_id=job_id,
                    scene_index=scene.index
                )
            except CDPConnectionError as cdp_err:
                logger.warning(f"CDP connection failed during scene {scene_num}: {cdp_err}")
                JobRepository.add_log(job_id, "WARN", "FLOW_AUTOMATION", str(cdp_err))
                JobService.update_job_status(job_id, JobStatus.VISUALS_PENDING)
                return generated_clips
            except Exception as gen_err:
                if isinstance(generator, FlowBrowserVideoGenerator):
                    warn_msg = (
                        f"Google Flow browser automation encountered an issue on Scene {scene_num}: {gen_err}. "
                        "Job placed in VISUALS_PENDING so manual downloads into your Downloads folder are auto-ingested."
                    )
                    logger.warning(warn_msg)
                    JobRepository.add_log(job_id, "WARN", "FLOW_AUTOMATION", warn_msg)
                    JobService.update_job_status(job_id, JobStatus.VISUALS_PENDING)
                    return generated_clips
                raise

            # Validate generated video with FFprobe
            probe = SceneIntakeService.probe_video(clip_path)
            if not probe["is_valid"]:
                raise RuntimeError(f"Generated clip for scene {scene_num} failed FFprobe validation: {probe.get('error')}")

            scene.clip_file = str(clip_path)
            scene.is_validated = True
            generated_clips.append(str(clip_path))

        # Save updated scenes to manifest & DB
        JobService.save_manifest(manifest)
        JobService.set_scenes(job_id, manifest.scenes)
        JobService.update_job_status(job_id, JobStatus.SCENES_VALIDATED)
        JobRepository.add_log(job_id, "INFO", "VIDEO_GENERATOR", f"All {len(generated_clips)} scene clips generated and validated.")
        return generated_clips

    @classmethod
    def run_full_automated_pipeline(
        cls,
        job_id: str,
        script_request: Optional[ScriptGenerationRequest] = None,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        bgm_path: Optional[str] = None,
        quality_mode: Optional[Any] = None,
        provider_override: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Complete 1-Click End-to-End Automated Pipeline:
        Script -> Voice -> Subtitles -> AI Video Generation -> FFmpeg Conforming -> Quality Control.
        """
        logger.info(f"Initiating 1-Click Automated Pipeline for job '{job_id}'")

        # Step 1: Script
        cls.execute_script_stage(job_id, generation_request=script_request)

        # Step 2: Audio
        cls.execute_audio_stage(job_id, voice=voice, speed=speed)

        # Step 3: Subtitles
        cls.execute_subtitles_stage(job_id)

        # Step 4: Automated Video Generation
        generated_clips = cls.execute_video_generation_stage(job_id, provider_override=provider_override)
        manifest = JobService.load_manifest(job_id)
        if not manifest or len(generated_clips) < len(manifest.scenes):
            JobService.update_job_status(job_id, JobStatus.VISUALS_PENDING)
            return {
                "status": "visuals_pending",
                "job_id": job_id,
                "current_status": JobStatus.VISUALS_PENDING.value,
                "message": "Job is in VISUALS_PENDING awaiting Google Flow clips (via CDP port 9222 or Downloads folder)."
            }

        # Step 5: Render with BGM & EBU R128 Loudness Normalization
        from backend.schemas.preset_schema import QualityMode
        q_mode = quality_mode if isinstance(quality_mode, QualityMode) else QualityMode.BALANCED
        active_bgm = bgm_path
        if not active_bgm and settings.DEFAULT_BGM_PATH.exists():
            active_bgm = str(settings.DEFAULT_BGM_PATH)

        return VideoRenderService.render_final_video(
            job_id=job_id,
            bgm_path=active_bgm,
            quality_mode=q_mode
        )

    @classmethod
    def retry_job(cls, job_id: str) -> Dict[str, Any]:
        """
        Retries a failed job by clearing error state and resuming execution from
        whichever stage was incomplete.
        """
        job = JobService.get_job(job_id)
        if not job:
            raise ValueError(f"Job '{job_id}' not found.")

        JobRepository.add_log(job_id, "INFO", "RETRY", "Retrying failed pipeline stage...")
        JobRepository.update_job_status(job_id, job["status"], error_message=None)

        # Resume execution
        return cls.run_pipeline_up_to_visuals(job_id)

    @classmethod
    def _mark_job_failed(cls, job_id: str, stage: str, error_message: str):
        """Helper to mark job as FAILED and record error in SQLite and manifest."""
        JobService.update_job_status(job_id, JobStatus.FAILED, error_message=error_message)
        JobRepository.add_log(job_id, "ERROR", stage, error_message)
