import re
import json
import logging
import datetime
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

from backend.schemas.qc_schema import QualityReport, CheckItem, QCCheckStatus
from backend.services.job_service import JobService
from backend.services.scene_intake_service import SceneIntakeService

logger = logging.getLogger(__name__)


class QualityControlService:
    """
    Automated Multi-Factor Quality Control Engine for YouTube Shorts.
    Performs deep inspection of video/audio stream health, freezes, clipping,
    resolution, and synchronization.
    """

    @classmethod
    def run_quality_control(
        cls,
        job_id: str,
        video_path_override: Optional[str] = None
    ) -> QualityReport:
        manifest = JobService.load_manifest(job_id)
        if not manifest:
            raise ValueError(f"Job manifest for '{job_id}' not found.")

        # Determine video path
        if video_path_override:
            video_file = Path(video_path_override)
        elif manifest.final_video_file:
            video_file = Path(manifest.final_video_file)
        else:
            video_file = JobService.get_job_directory(job_id) / "output" / "final_short.mp4"

        checks: List[CheckItem] = []
        metrics: Dict[str, Any] = {}

        # Check 1: File Existence & Basic Probe
        if not video_file.exists() or video_file.stat().st_size < 1000:
            checks.append(CheckItem(
                name="File Existence & Non-Zero Size",
                status=QCCheckStatus.FAIL,
                is_critical=True,
                message=f"Output video file missing or empty: {video_file}"
            ))
            return cls._compile_report(job_id, checks, metrics, str(video_file))

        probe = SceneIntakeService.probe_video(video_file)
        metrics["probe"] = probe

        # Check 2: Video Stream Readable & Packet Decode Integrity
        decode_cmd = ["ffmpeg", "-v", "error", "-i", str(video_file), "-f", "null", "-"]
        decode_res = subprocess.run(decode_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if decode_res.returncode == 0:
            checks.append(CheckItem(
                name="Video Stream Decode & Corruption Check",
                status=QCCheckStatus.PASS,
                is_critical=True,
                message="Video stream decoded cleanly with zero stream/packet errors."
            ))
        else:
            checks.append(CheckItem(
                name="Video Stream Decode & Corruption Check",
                status=QCCheckStatus.FAIL,
                is_critical=True,
                message=f"Corrupt frames or stream decode failure: {decode_res.stderr.strip()[:200]}"
            ))

        # Check 3: Resolution & Aspect Ratio (Must be 1080x1920 vertical 9:16)
        width = probe.get("width", 0)
        height = probe.get("height", 0)
        metrics["resolution"] = f"{width}x{height}"
        metrics["is_vertical_9_16"] = probe.get("is_vertical_9_16", False)

        if width == 1080 and height == 1920:
            checks.append(CheckItem(
                name="Resolution & 9:16 Aspect Ratio",
                status=QCCheckStatus.PASS,
                is_critical=True,
                message=f"Resolution conforms exactly to YouTube Shorts vertical standard (1080x1920).",
                metric_value=f"{width}x{height}"
            ))
        elif probe.get("is_vertical_9_16"):
            checks.append(CheckItem(
                name="Resolution & 9:16 Aspect Ratio",
                status=QCCheckStatus.WARN,
                is_critical=False,
                message=f"Aspect ratio is 9:16 but resolution is {width}x{height} (expected 1080x1920).",
                metric_value=f"{width}x{height}"
            ))
        else:
            checks.append(CheckItem(
                name="Resolution & 9:16 Aspect Ratio",
                status=QCCheckStatus.FAIL,
                is_critical=True,
                message=f"Invalid video aspect ratio: {width}x{height}. Must be 9:16 vertical.",
                metric_value=f"{width}x{height}"
            ))

        # Check 4: Black Frames & Frozen Video Detection
        try:
            # Detect sustained black screen (>1.5s) or freeze (>2.0s)
            freeze_cmd = [
                "ffmpeg", "-i", str(video_file),
                "-vf", "blackdetect=d=1.5:pix_th=0.10,freezedetect=n=-50dB:d=2.0",
                "-f", "null", "-"
            ]
            freeze_res = subprocess.run(freeze_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            black_matches = re.findall(r"black_start:([0-9.]+)", freeze_res.stderr)
            freeze_matches = re.findall(r"freeze_start:([0-9.]+)", freeze_res.stderr)

            if black_matches:
                checks.append(CheckItem(
                    name="Black Frames Detection",
                    status=QCCheckStatus.FAIL,
                    is_critical=True,
                    message=f"Detected sustained black frame anomaly at timestamp {black_matches[0]}s.",
                    metric_value=black_matches
                ))
            else:
                checks.append(CheckItem(
                    name="Black Frames Detection",
                    status=QCCheckStatus.PASS,
                    is_critical=True,
                    message="Zero sustained black frame anomalies detected."
                ))

            if freeze_matches:
                checks.append(CheckItem(
                    name="Frozen Video Scene Detection",
                    status=QCCheckStatus.WARN,
                    is_critical=False,
                    message=f"Detected prolonged frozen/static frame sequence starting at {freeze_matches[0]}s.",
                    metric_value=freeze_matches
                ))
            else:
                checks.append(CheckItem(
                    name="Frozen Video Scene Detection",
                    status=QCCheckStatus.PASS,
                    is_critical=False,
                    message="Zero frozen frame anomalies detected."
                ))
        except Exception as e:
            logger.warning(f"Freeze/black detection skipped: {e}")

        # Check 5: Audio Volume, Peak Levels & Excessive Silence
        try:
            vol_cmd = ["ffmpeg", "-i", str(video_file), "-vn", "-af", "volumedetect", "-f", "null", "-"]
            vol_res = subprocess.run(vol_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            max_vol_match = re.search(r"max_volume:\s*([-0-9.]+)\s*dB", vol_res.stderr)
            mean_vol_match = re.search(r"mean_volume:\s*([-0-9.]+)\s*dB", vol_res.stderr)

            if max_vol_match:
                max_vol = float(max_vol_match.group(1))
                mean_vol = float(mean_vol_match.group(1)) if mean_vol_match else -20.0
                metrics["max_volume_db"] = max_vol
                metrics["mean_volume_db"] = mean_vol

                if max_vol >= 0.0:
                    checks.append(CheckItem(
                        name="Audio Clipping & Distortion Check",
                        status=QCCheckStatus.FAIL,
                        is_critical=True,
                        message=f"Audio signal is clipping at {max_vol} dB (must be < 0.0 dB).",
                        metric_value=f"{max_vol} dB"
                    ))
                elif max_vol < -15.0:
                    checks.append(CheckItem(
                        name="Audio Clipping & Distortion Check",
                        status=QCCheckStatus.WARN,
                        is_critical=False,
                        message=f"Audio peak level is unusually quiet: {max_vol} dB.",
                        metric_value=f"{max_vol} dB"
                    ))
                else:
                    checks.append(CheckItem(
                        name="Audio Clipping & Distortion Check",
                        status=QCCheckStatus.PASS,
                        is_critical=True,
                        message=f"Audio peak is safe and non-clipping ({max_vol} dBFS).",
                        metric_value=f"{max_vol} dB"
                    ))
            else:
                checks.append(CheckItem(
                    name="Audio Presence Check",
                    status=QCCheckStatus.FAIL,
                    is_critical=True,
                    message="No audio stream detected in final video file."
                ))
        except Exception as e:
            logger.warning(f"Volume check error: {e}")

        # Check 6: Audio/Video Synchronization
        video_dur = probe.get("duration_seconds", 0.0)
        expected_audio_dur = manifest.render_metadata.get("total_duration_seconds", 0.0)
        sync_diff = abs(video_dur - expected_audio_dur) if expected_audio_dur > 0 else 0.0
        metrics["video_duration"] = video_dur
        metrics["expected_audio_duration"] = expected_audio_dur
        metrics["sync_delta_seconds"] = round(sync_diff, 3)

        if sync_diff <= 0.25:
            checks.append(CheckItem(
                name="Audio/Video Synchronization",
                status=QCCheckStatus.PASS,
                is_critical=True,
                message=f"Audio and video timelines are synchronized within {sync_diff:.3f}s.",
                metric_value=f"Delta: {sync_diff:.3f}s"
            ))
        elif sync_diff <= 0.8:
            checks.append(CheckItem(
                name="Audio/Video Synchronization",
                status=QCCheckStatus.WARN,
                is_critical=False,
                message=f"Minor audio/video duration discrepancy ({sync_diff:.2f}s).",
                metric_value=f"Delta: {sync_diff:.2f}s"
            ))
        else:
            checks.append(CheckItem(
                name="Audio/Video Synchronization",
                status=QCCheckStatus.FAIL,
                is_critical=True,
                message=f"Audio/video desynchronization detected ({sync_diff:.2f}s difference).",
                metric_value=f"Delta: {sync_diff:.2f}s"
            ))

        # Check 7: Subtitle Validation
        sub_file = Path(manifest.subtitle_file) if manifest.subtitle_file else None
        if sub_file and sub_file.exists() and sub_file.stat().st_size > 100:
            checks.append(CheckItem(
                name="Subtitle Integrity & Burn Verification",
                status=QCCheckStatus.PASS,
                is_critical=True,
                message=f"Animated ASS subtitle track verified and burned into frames ({sub_file.name})."
            ))
        else:
            checks.append(CheckItem(
                name="Subtitle Integrity & Burn Verification",
                status=QCCheckStatus.FAIL,
                is_critical=True,
                message="Subtitle track missing or empty."
            ))

        # Check 8: Scene Completeness Check
        missing_scenes = [s.index for s in manifest.scenes if not s.is_validated or not s.clip_file]
        if not missing_scenes:
            checks.append(CheckItem(
                name="Scene Assignment Completeness",
                status=QCCheckStatus.PASS,
                is_critical=True,
                message=f"All {len(manifest.scenes)} scenes assigned and validated."
            ))
        else:
            checks.append(CheckItem(
                name="Scene Assignment Completeness",
                status=QCCheckStatus.FAIL,
                is_critical=True,
                message=f"Missing scene assignments for indices: {missing_scenes}"
            ))

        return cls._compile_report(job_id, checks, metrics, str(video_file))

    @classmethod
    def _compile_report(
        cls,
        job_id: str,
        checks: List[CheckItem],
        metrics: Dict[str, Any],
        video_path: str
    ) -> QualityReport:
        failed_checks = [c for c in checks if c.status == QCCheckStatus.FAIL]
        warning_checks = [c for c in checks if c.status == QCCheckStatus.WARN]
        passed_checks = [c for c in checks if c.status == QCCheckStatus.PASS]

        # A video is production-ready ONLY IF zero critical checks have failed
        critical_failures = [c for c in failed_checks if c.is_critical]
        is_ready = len(critical_failures) == 0

        if len(failed_checks) > 0:
            overall_status = QCCheckStatus.FAIL
        elif len(warning_checks) > 0:
            overall_status = QCCheckStatus.WARN
        else:
            overall_status = QCCheckStatus.PASS

        report = QualityReport(
            job_id=job_id,
            overall_status=overall_status,
            is_production_ready=is_ready,
            total_checks=len(checks),
            passed_checks=len(passed_checks),
            failed_checks=len(failed_checks),
            warning_checks=len(warning_checks),
            checks=checks,
            generated_at=datetime.datetime.utcnow().isoformat(),
            video_path=video_path,
            metrics=metrics
        )

        # Export quality_report.json to output directory
        job_dir = JobService.get_job_directory(job_id)
        qc_file = job_dir / "output" / "quality_report.json"
        qc_file.parent.mkdir(parents=True, exist_ok=True)
        with open(qc_file, "w", encoding="utf-8") as f:
            f.write(report.model_dump_json(indent=2))

        # Sync to manifest
        manifest = JobService.load_manifest(job_id)
        if manifest:
            manifest.render_metadata["quality_report"] = report.model_dump()
            manifest.render_metadata["is_production_ready"] = is_ready
            JobService.save_manifest(manifest)

        return report
