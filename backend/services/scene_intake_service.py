import os
import re
import json
import shutil
import logging
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from backend.config import settings
from backend.services.job_service import JobService
from backend.database.repository import JobRepository
from backend.schemas.job_manifest import JobStatus, SceneSpec

logger = logging.getLogger(__name__)

SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}


class SceneIntakeService:
    """
    Manages detection, FFprobe inspection, automated matching, and manual assignment
    of incoming video clips downloaded from Google Flow / Veo.
    """

    @classmethod
    def get_incoming_directory(cls, job_id: str) -> Path:
        incoming_dir = JobService.get_job_directory(job_id) / "incoming_clips"
        incoming_dir.mkdir(parents=True, exist_ok=True)
        return incoming_dir

    @classmethod
    def get_scenes_directory(cls, job_id: str) -> Path:
        scenes_dir = JobService.get_job_directory(job_id) / "scenes"
        scenes_dir.mkdir(parents=True, exist_ok=True)
        return scenes_dir

    @classmethod
    def probe_video(cls, video_path: Path) -> Dict[str, Any]:
        """
        Uses FFprobe to inspect a video file and extract technical metadata.
        """
        if not video_path.exists():
            return {"is_valid": False, "error": f"File not found: {video_path}"}

        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration,r_frame_rate,codec_name",
            "-show_entries", "format=duration,size,format_name",
            "-of", "json",
            str(video_path)
        ]

        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            if res.returncode != 0:
                return {"is_valid": False, "error": f"FFprobe error: {res.stderr.strip()}"}

            data = json.loads(res.stdout)
            streams = data.get("streams", [])
            format_info = data.get("format", {})

            if not streams:
                return {"is_valid": False, "error": "No video streams detected"}

            stream = streams[0]
            width = int(stream.get("width", 0))
            height = int(stream.get("height", 0))
            codec = stream.get("codec_name", "unknown")

            # Duration calculation
            dur_str = stream.get("duration") or format_info.get("duration") or "0"
            duration = float(dur_str)

            # Frame rate calculation
            fps_str = stream.get("r_frame_rate", "30/1")
            if "/" in fps_str:
                num, den = fps_str.split("/")
                fps = round(float(num) / float(den), 2) if float(den) > 0 else 30.0
            else:
                fps = float(fps_str)

            file_size = int(format_info.get("size") or video_path.stat().st_size)

            # Aspect ratio check
            aspect_ratio = width / height if height > 0 else 0.0
            is_vertical_9_16 = abs(aspect_ratio - (9 / 16)) < 0.05 or (width == 1080 and height == 1920)

            return {
                "is_valid": width > 0 and height > 0 and duration > 0,
                "file_path": str(video_path),
                "filename": video_path.name,
                "width": width,
                "height": height,
                "aspect_ratio": round(aspect_ratio, 4),
                "is_vertical_9_16": is_vertical_9_16,
                "duration_seconds": round(duration, 3),
                "fps": fps,
                "codec": codec,
                "size_bytes": file_size,
                "error": None
            }

        except Exception as e:
            return {"is_valid": False, "error": f"Failed to probe video: {str(e)}"}

    @classmethod
    def match_filename_to_scene_index(cls, filename: str, scene_count: int) -> Optional[int]:
        """
        Determines target scene index from filenames using strict pattern matching.
        Never guesses if there is any ambiguity.
        Examples:
          - scene_01.mp4 -> 0
          - scene1.mp4 -> 0
          - clip_02.mp4 -> 1
          - 3.mp4 -> 2
        """
        stem = Path(filename).stem.lower().strip()

        # Pattern 1: scene_01, scene1, scene-2
        m1 = re.search(r"(?:scene|clip)[_\s-]*0*(\d+)", stem)
        if m1:
            idx = int(m1.group(1)) - 1
            if 0 <= idx < scene_count:
                return idx

        # Pattern 2: standalone number, e.g. "1.mp4" or "02.mp4"
        m2 = re.match(r"^0*(\d+)$", stem)
        if m2:
            idx = int(m2.group(1)) - 1
            if 0 <= idx < scene_count:
                return idx

        return None

    @classmethod
    def scan_and_ingest(cls, job_id: str) -> Dict[str, Any]:
        """
        Scans incoming_clips/ for the job, validates video integrity via FFprobe,
        matches files to scenes, assigns them safely, and preserves originals.
        """
        manifest = JobService.load_manifest(job_id)
        if not manifest:
            raise ValueError(f"Job manifest for '{job_id}' not found.")

        scene_count = len(manifest.scenes)
        if scene_count == 0:
            raise ValueError("Job has no scenes defined. Generate script first.")

        incoming_dir = cls.get_incoming_directory(job_id)
        scenes_dir = cls.get_scenes_directory(job_id)

        # 1. Discover all video files in incoming directory
        incoming_files = [
            f for f in incoming_dir.iterdir()
            if f.is_file() and f.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
        ]

        # 2. Probe and validate all incoming files
        probed_files: List[Dict[str, Any]] = []
        for f in incoming_files:
            meta = cls.probe_video(f)
            probed_files.append(meta)

        # 3. Match candidate files to scene indices
        matches_per_scene: Dict[int, List[Dict[str, Any]]] = {i: [] for i in range(scene_count)}
        unmatched_files: List[Dict[str, Any]] = []

        for p in probed_files:
            if not p["is_valid"]:
                continue
            matched_idx = cls.match_filename_to_scene_index(p["filename"], scene_count)
            if matched_idx is not None:
                matches_per_scene[matched_idx].append(p)
            else:
                unmatched_files.append(p)

        # 4. Safe assignment: only assign when exactly ONE candidate matches
        assigned_scenes: Dict[int, Dict[str, Any]] = {}
        duplicates_detected: Dict[int, List[str]] = {}

        for scene_idx, candidates in matches_per_scene.items():
            if len(candidates) == 1:
                cand = candidates[0]
                target_filename = f"scene_{scene_idx + 1:02d}{Path(cand['filename']).suffix.lower()}"
                target_path = scenes_dir / target_filename

                # Copy file into scenes/ to preserve original in incoming_clips/
                source_path = Path(cand["file_path"])
                if not target_path.exists() or source_path.stat().st_mtime > target_path.stat().st_mtime:
                    shutil.copy2(str(source_path), str(target_path))

                # Update SceneSpec
                manifest.scenes[scene_idx].clip_file = str(target_path)
                manifest.scenes[scene_idx].is_validated = True

                assigned_scenes[scene_idx] = {
                    "scene_index": scene_idx,
                    "clip_file": str(target_path),
                    "original_filename": cand["filename"],
                    "probe_metadata": cand
                }
            elif len(candidates) > 1:
                # Ambiguous collision: do NOT auto-assign
                duplicates_detected[scene_idx] = [c["filename"] for c in candidates]
                logger.warning(
                    f"Job '{job_id}' Scene {scene_idx + 1} has multiple candidates: {duplicates_detected[scene_idx]}"
                )

        # Check existing assigned scenes in scenes_dir
        for idx in range(scene_count):
            if idx not in assigned_scenes:
                expected_scene_file = scenes_dir / f"scene_{idx + 1:02d}.mp4"
                if expected_scene_file.exists():
                    probe = cls.probe_video(expected_scene_file)
                    if probe["is_valid"]:
                        manifest.scenes[idx].clip_file = str(expected_scene_file)
                        manifest.scenes[idx].is_validated = True
                        assigned_scenes[idx] = {
                            "scene_index": idx,
                            "clip_file": str(expected_scene_file),
                            "original_filename": expected_scene_file.name,
                            "probe_metadata": probe
                        }

        # 5. Persist updates to manifest & SQLite
        JobService.save_manifest(manifest)
        JobService.set_scenes(job_id, manifest.scenes)

        missing_scenes = [i for i in range(scene_count) if i not in assigned_scenes]
        all_ready = len(missing_scenes) == 0

        # Update job status if all scenes are ready
        if all_ready and manifest.status == JobStatus.VISUALS_PENDING:
            JobService.update_job_status(job_id, JobStatus.SCENES_VALIDATED)
            JobRepository.add_log(
                job_id,
                "INFO",
                "SCENE_INTAKE",
                f"All {scene_count} scene clips validated and ready for render."
            )

        return {
            "job_id": job_id,
            "total_scenes": scene_count,
            "assigned_scenes_count": len(assigned_scenes),
            "missing_scenes": missing_scenes,
            "all_scenes_ready": all_ready,
            "assigned_scenes": assigned_scenes,
            "unmatched_files": [f["filename"] for f in unmatched_files],
            "duplicates": duplicates_detected,
            "incoming_folder": str(incoming_dir)
        }

    @classmethod
    def assign_clip_manually(cls, job_id: str, scene_index: int, filename: str) -> Dict[str, Any]:
        """
        Manually assigns an incoming clip to a specific scene index.
        """
        manifest = JobService.load_manifest(job_id)
        if not manifest:
            raise ValueError(f"Job '{job_id}' not found.")

        if not (0 <= scene_index < len(manifest.scenes)):
            raise ValueError(f"Invalid scene index {scene_index}. Must be 0 to {len(manifest.scenes) - 1}.")

        incoming_dir = cls.get_incoming_directory(job_id)
        source_file = incoming_dir / filename
        if not source_file.exists():
            raise FileNotFoundError(f"File '{filename}' not found in incoming_clips folder: {incoming_dir}")

        probe = cls.probe_video(source_file)
        if not probe["is_valid"]:
            raise ValueError(f"File '{filename}' failed video validation: {probe.get('error')}")

        scenes_dir = cls.get_scenes_directory(job_id)
        target_path = scenes_dir / f"scene_{scene_index + 1:02d}{source_file.suffix.lower()}"
        shutil.copy2(str(source_file), str(target_path))

        manifest.scenes[scene_index].clip_file = str(target_path)
        manifest.scenes[scene_index].is_validated = True
        JobService.save_manifest(manifest)
        JobService.set_scenes(job_id, manifest.scenes)

        JobRepository.add_log(
            job_id,
            "INFO",
            "SCENE_INTAKE",
            f"Manually assigned '{filename}' to Scene {scene_index + 1}."
        )

        return {
            "status": "success",
            "scene_index": scene_index,
            "assigned_file": str(target_path),
            "probe": probe
        }
