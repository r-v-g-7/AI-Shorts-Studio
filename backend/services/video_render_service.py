import os
import re
import time
import shutil
import logging
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from backend.config import settings
from backend.services.job_service import JobService
from backend.services.scene_intake_service import SceneIntakeService
from backend.services.quality_control_service import QualityControlService
from backend.database.repository import JobRepository
from backend.schemas.job_manifest import JobStatus, SceneSpec
from backend.schemas.preset_schema import QualityMode

logger = logging.getLogger(__name__)


class VideoRenderService:
    """
    Hardware-accelerated (NVENC / MediaFoundation / CPU) 9:16 vertical video compositor.
    Handles scaling, cropping, scene concatenation, audio mixing, loudness normalization,
    ASS subtitle burning, and automated post-render Quality Control.
    """

    _cached_encoder_config: Optional[Tuple[str, List[str]]] = None

    @classmethod
    def test_and_select_encoder(
        cls,
        quality_mode: QualityMode = QualityMode.BALANCED
    ) -> Tuple[str, List[str]]:
        """
        Dynamically tests NVENC, MediaFoundation, and CPU libx264 encoders
        and returns the best available encoder configured for the requested quality mode.
        """
        bitrate_map = {
            QualityMode.DRAFT: "3M",
            QualityMode.BALANCED: "6M",
            QualityMode.HIGH_QUALITY: "10M"
        }
        crf_map = {
            QualityMode.DRAFT: ("ultrafast", "26"),
            QualityMode.BALANCED: ("veryfast", "22"),
            QualityMode.HIGH_QUALITY: ("medium", "18")
        }
        target_bitrate = bitrate_map.get(quality_mode, "6M")
        preset, crf = crf_map.get(quality_mode, ("veryfast", "22"))

        # Candidate 1: NVIDIA NVENC (Fastest on RTX 3050 if driver >= 610)
        try:
            cmd = [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
                "-c:v", "h264_nvenc", "-f", "null", "-"
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
            if res.returncode == 0:
                logger.info("Hardware Encoder Verified: NVIDIA NVENC (h264_nvenc)")
                return ("h264_nvenc", [
                    "-c:v", "h264_nvenc",
                    "-preset", "p4",
                    "-cq", crf,
                    "-b:v", target_bitrate,
                    "-maxrate", "12M",
                    "-bufsize", "14M",
                    "-pix_fmt", "yuv420p"
                ])
        except Exception as e:
            logger.debug(f"NVENC test failed: {e}")

        # Candidate 2: Windows Media Foundation (Hardware acceleration via DirectX / GPU)
        try:
            cmd = [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
                "-c:v", "h264_mf", "-f", "null", "-"
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
            if res.returncode == 0:
                logger.info("Hardware Encoder Verified: Windows Media Foundation (h264_mf)")
                return ("h264_mf", [
                    "-c:v", "h264_mf",
                    "-b:v", target_bitrate,
                    "-pix_fmt", "yuv420p"
                ])
        except Exception as e:
            logger.debug(f"MediaFoundation test failed: {e}")

        # Candidate 3: Universal CPU Fallback (libx264)
        logger.info(f"Using Reliable CPU Encoder: libx264 ({preset})")
        return ("libx264", [
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", crf,
            "-pix_fmt", "yuv420p"
        ])

    @classmethod
    def calculate_scene_durations(
        cls,
        scenes: List[SceneSpec],
        total_audio_duration: float,
        word_timestamps: List[Any]
    ) -> List[float]:
        """
        Calculates exact target durations for each scene clip to ensure 100% audio-video sync.
        Uses word timestamps boundaries when available, or proportional scene weight.
        """
        scene_count = len(scenes)
        if scene_count == 0:
            return []

        # If word timestamps exist, map them to scenes by word count
        if word_timestamps and len(word_timestamps) > 0:
            total_words = len(word_timestamps)
            durations: List[float] = []
            words_per_scene = max(1, total_words // scene_count)
            prev_time = 0.0

            for i in range(scene_count):
                if i == scene_count - 1:
                    dur = max(0.5, total_audio_duration - prev_time)
                else:
                    word_idx = min(len(word_timestamps) - 1, (i + 1) * words_per_scene - 1)
                    boundary = word_timestamps[word_idx].end_time if hasattr(word_timestamps[word_idx], "end_time") else word_timestamps[word_idx].get("end_time", 0.0)
                    dur = max(0.5, boundary - prev_time)
                    prev_time = boundary
                durations.append(round(dur, 3))

            # Ensure sum matches total_audio_duration exactly
            total_calc = sum(durations)
            diff = total_audio_duration - total_calc
            durations[-1] = round(max(0.5, durations[-1] + diff), 3)
            return durations

        # Fallback: Proportional allocation by estimated scene duration
        sum_est = sum(s.duration_seconds for s in scenes)
        if sum_est <= 0:
            even_dur = round(total_audio_duration / scene_count, 3)
            durations = [even_dur] * scene_count
            durations[-1] = round(total_audio_duration - even_dur * (scene_count - 1), 3)
            return durations

        durations = []
        for i, s in enumerate(scenes):
            if i == scene_count - 1:
                dur = max(0.5, total_audio_duration - sum(durations))
            else:
                dur = max(0.5, total_audio_duration * (s.duration_seconds / sum_est))
            durations.append(round(dur, 3))

        return durations

    @classmethod
    def render_final_video(
        cls,
        job_id: str,
        bgm_path: Optional[str] = None,
        bgm_volume: float = 0.18,
        quality_mode: QualityMode = QualityMode.BALANCED
    ) -> Dict[str, Any]:
        """
        Orchestrates the entire video rendering stage:
        conforms all scene clips to 1080x1920 9:16 vertical resolution,
        synchronizes cuts to narration audio, hard-burns ASS animated subtitles,
        applies EBU R128 audio normalization, and produces the final MP4.
        Then runs automated Quality Control.
        """
        manifest = JobService.load_manifest(job_id)
        if not manifest:
            raise ValueError(f"Job manifest for '{job_id}' not found.")

        # Validation: Check required assets
        if not manifest.audio_file or not Path(manifest.audio_file).exists():
            raise FileNotFoundError(f"Job audio file missing: {manifest.audio_file}")

        if not manifest.subtitle_file or not Path(manifest.subtitle_file).exists():
            raise FileNotFoundError(f"Job subtitle file missing: {manifest.subtitle_file}")

        if not manifest.scenes:
            raise ValueError("No scenes defined in manifest.")

        # Ensure all scenes have clips assigned
        missing_clips = [s.index for s in manifest.scenes if not s.clip_file or not Path(s.clip_file).exists()]
        if missing_clips:
            raise ValueError(f"Missing video clips for scenes: {missing_clips}. Ingest clips first.")

        # Setup paths
        job_dir = JobService.get_job_directory(job_id)
        output_dir = job_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        final_video_path = output_dir / "final_short.mp4"
        temp_dir = job_dir / "temp_render"
        temp_dir.mkdir(parents=True, exist_ok=True)

        JobService.update_job_status(job_id, JobStatus.RENDERING)
        JobRepository.add_log(job_id, "INFO", "RENDER_ENGINE", "Initiating final 9:16 Shorts video render...")

        start_time = time.time()

        try:
            # 1. Detect and configure encoder
            encoder_name, encoder_args = cls.test_and_select_encoder(quality_mode=quality_mode)
            logger.info(f"Rendering job '{job_id}' using encoder '{encoder_name}' ({quality_mode.value})")

            # 2. Get total audio duration
            audio_path = Path(manifest.audio_file)
            probe_audio_cmd = [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)
            ]
            res = subprocess.run(probe_audio_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode != 0:
                raise RuntimeError(f"FFprobe failed on audio file: {res.stderr.strip()}")
            total_audio_dur = float(res.stdout.strip())
            logger.info(f"Master narration audio duration: {total_audio_dur:.3f}s")

            # 3. Calculate target durations for each scene
            scene_durations = cls.calculate_scene_durations(
                scenes=manifest.scenes,
                total_audio_duration=total_audio_dur,
                word_timestamps=manifest.word_timestamps
            )
            logger.info(f"Target scene durations: {scene_durations}")

            # 4. Conform each scene clip into standardized 1080x1920 30fps vertical MP4
            conformed_clip_files: List[Path] = []
            for idx, scene in enumerate(manifest.scenes):
                target_dur = scene_durations[idx]
                source_clip = Path(scene.clip_file)
                conformed_clip = temp_dir / f"conformed_scene_{idx + 1:02d}.mp4"

                # Filter: scale to fill 1080x1920 (crop excess), set sar=1, fps=30
                # If clip is shorter than target_dur, loop it with -stream_loop -1
                conform_cmd = [
                    "ffmpeg", "-y",
                    "-stream_loop", "-1",
                    "-i", str(source_clip),
                    "-t", f"{target_dur:.3f}",
                    "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920:(in_w-1080)/2:(in_h-1920)/2,setsar=1,fps=30",
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-crf", "18",
                    "-an",
                    str(conformed_clip)
                ]

                sub_res = subprocess.run(conform_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if sub_res.returncode != 0:
                    raise RuntimeError(f"Failed to conform scene {idx + 1} ({source_clip.name}): {sub_res.stderr.strip()[-300:]}")

                conformed_clip_files.append(conformed_clip)

            # 5. Build concat demuxer text file
            concat_list_file = temp_dir / "concat_list.txt"
            with open(concat_list_file, "w", encoding="utf-8") as f:
                for clip in conformed_clip_files:
                    safe_path = str(clip.resolve()).replace("\\", "/")
                    f.write(f"file '{safe_path}'\n")

            # 6. Assemble complete composite command
            ass_escaped = str(Path(manifest.subtitle_file).resolve()).replace("\\", "/").replace(":", "\\:")

            final_render_cmd = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_list_file.resolve()),
                "-i", str(Path(manifest.audio_file).resolve()),
            ]

            # Optional BGM input with audio ducking and loudness normalization
            if bgm_path and Path(bgm_path).exists():
                final_render_cmd.extend(["-stream_loop", "-1", "-i", str(Path(bgm_path).resolve())])
                # Filter graph: Burn ASS + Duck BGM and mix + EBU R128 loudness normalization
                filter_complex = (
                    f"[0:v]ass='{ass_escaped}'[vfinal];"
                    f"[2:a]volume={bgm_volume:.2f}[bgm];"
                    f"[1:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[amixed];"
                    f"[amixed]loudnorm=I=-14:TP=-1.5:LRA=7[afinal]"
                )
                final_render_cmd.extend([
                    "-filter_complex", filter_complex,
                    "-map", "[vfinal]",
                    "-map", "[afinal]"
                ])
            else:
                # Direct narration mix with ASS burn + EBU R128 loudness normalization
                filter_complex = (
                    f"[0:v]ass='{ass_escaped}'[vfinal];"
                    f"[1:a]loudnorm=I=-14:TP=-1.5:LRA=7[afinal]"
                )
                final_render_cmd.extend([
                    "-filter_complex", filter_complex,
                    "-map", "[vfinal]",
                    "-map", "[afinal]"
                ])

            # Apply selected video encoder args
            final_render_cmd.extend(encoder_args)

            # Audio settings & web optimization
            final_render_cmd.extend([
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", "48000",
                "-shortest",
                "-movflags", "+faststart",
                str(final_video_path)
            ])

            # Execute final render
            logger.info(f"Running final render command for job '{job_id}'...")
            render_proc = subprocess.run(
                final_render_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            if render_proc.returncode != 0:
                err_excerpt = render_proc.stderr.strip()[-600:]
                raise RuntimeError(f"FFmpeg composite render failed: {err_excerpt}")

            render_duration = time.time() - start_time

            # 7. Verify rendered video file with FFprobe
            probe_final = SceneIntakeService.probe_video(final_video_path)
            if not probe_final["is_valid"]:
                raise RuntimeError(f"Rendered video failed verification: {probe_final.get('error')}")

            # 8. Update manifest & SQLite records
            manifest.final_video_file = str(final_video_path)
            manifest.render_metadata["render_duration_seconds"] = round(render_duration, 2)
            manifest.render_metadata["output_file_size_bytes"] = probe_final["size_bytes"]
            manifest.render_metadata["actual_duration_seconds"] = probe_final["duration_seconds"]
            manifest.render_metadata["encoder_used"] = encoder_name
            manifest.render_metadata["quality_mode"] = quality_mode.value

            JobService.save_manifest(manifest)

            # 9. Automated Multi-Factor Quality Control Check
            logger.info(f"Running automated Quality Control for job '{job_id}'...")
            qc_report = QualityControlService.run_quality_control(job_id)

            if qc_report.is_production_ready:
                manifest.status = JobStatus.COMPLETED
                JobService.save_manifest(manifest)
                JobRepository.update_job_status(
                    job_id,
                    JobStatus.COMPLETED.value,
                    output_video_path=str(final_video_path)
                )
                JobRepository.add_log(
                    job_id,
                    "INFO",
                    "QUALITY_CONTROL",
                    f"QC Passed ({qc_report.passed_checks}/{qc_report.total_checks} checks). Video marked PRODUCTION READY."
                )
            else:
                critical_count = qc_report.failed_checks
                JobRepository.add_log(
                    job_id,
                    "WARN",
                    "QUALITY_CONTROL",
                    f"QC Flagged {critical_count} critical failure(s). Video rendered but NOT production-ready."
                )

            return {
                "status": "success",
                "job_id": job_id,
                "video_path": str(final_video_path),
                "duration_seconds": probe_final["duration_seconds"],
                "resolution": f"{probe_final['width']}x{probe_final['height']}",
                "encoder_used": encoder_name,
                "quality_mode": quality_mode.value,
                "file_size_mb": round(probe_final["size_bytes"] / (1024 * 1024), 2),
                "render_time_seconds": round(render_duration, 2),
                "is_production_ready": qc_report.is_production_ready,
                "qc_status": qc_report.overall_status.value
            }

        except Exception as e:
            err_msg = f"Video render failed: {str(e)}"
            logger.error(err_msg, exc_info=True)
            JobService.update_job_status(job_id, JobStatus.FAILED, error_message=err_msg)
            JobRepository.add_log(job_id, "ERROR", "RENDER_ENGINE", err_msg)
            raise

        finally:
            # Temporary file cleanup
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
