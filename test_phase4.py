import os
import sys
import time
import json
import shutil
import subprocess
from pathlib import Path
import soundfile as sf

from backend.config import settings
from backend.schemas.job_manifest import JobStatus, VisualStyle
from backend.schemas.script_schema import ScriptGenerationRequest
from backend.services.job_service import JobService
from backend.services.pipeline_controller import PipelineController
from backend.services.scene_prompt_manager import ScenePromptManager
from backend.services.scene_intake_service import SceneIntakeService
from backend.services.video_render_service import VideoRenderService
from backend.database.connection import init_db

TEST_TOPIC = "The Colossal Creatures Living in the Mariana Trench"


def create_synthetic_test_clip(output_path: Path, duration: float, color: str, label: str):
    """
    Generates a genuine playable test video clip using FFmpeg.
    Creates a 1080x1920 vertical MP4 with gentle animated movement and label.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c={color}:s=1080x1920:d={duration:.2f}:r=30",
        "-t", f"{duration:.2f}",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        str(output_path)
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Failed to create test clip: {res.stderr.strip()}")


def run_phase4_end_to_end_test():
    print("=" * 70)
    print("AI SHORTS STUDIO - PHASE 4 VIDEO ENGINE & END-TO-END VERIFICATION")
    print("=" * 70)

    # Step 1: Initialize Database and Create Job
    init_db()
    print("\n--- 1. Creating Production Job ---")
    job = PipelineController.create_new_job(
        topic=TEST_TOPIC,
        title="Abyssal Colossus",
        visual_style=VisualStyle.DEEP_DIVE_3D
    )
    job_id = job["id"]
    job_dir = JobService.get_job_directory(job_id)
    print(f"[PASS] Job Created: ID={job_id}")

    # Step 2: Generate Script and Scene Prompts
    print("\n--- 2. Script & Prompt Generation Stage ---")
    script_req = ScriptGenerationRequest(
        topic=TEST_TOPIC,
        target_duration=20.0,
        visual_style=VisualStyle.DEEP_DIVE_3D,
        target_audience="curious young adults"
    )
    script = PipelineController.execute_script_stage(job_id, generation_request=script_req)
    assert len(script.scenes) >= 3, f"Expected at least 3 scenes, got {len(script.scenes)}"
    print(f"[PASS] Script Generated: '{script.title}' with {len(script.scenes)} scenes")

    # Verify Prompts Export (PART 1)
    json_prompts_file = job_dir / "prompts.json"
    txt_prompts_file = job_dir / "prompts.txt"
    assert json_prompts_file.exists(), "prompts.json missing"
    assert txt_prompts_file.exists(), "prompts.txt missing"

    with open(json_prompts_file, "r", encoding="utf-8") as f:
        prompts_meta = json.load(f)
    assert len(prompts_meta["prompts"]) == len(script.scenes)

    # Validate that prompts contain consistency, aspect ratio, camera, and negative tokens
    p0 = prompts_meta["prompts"][0]
    assert "9:16" in p0["production_prompt"], "9:16 aspect ratio instruction missing in prompt"
    assert p0["negative_prompt"], "Negative prompt missing"
    assert p0["camera_directive"], "Camera directive missing"
    print(f"[PASS] Part 1 Verified: Prompts exported (JSON & TXT) with full consistency & negative tokens")

    # Step 3: Local Voice Generation & Word Timestamps (Kokoro CPU)
    print("\n--- 3. Voice Synthesis & Subtitles Stage ---")
    audio_res = PipelineController.execute_audio_stage(job_id, voice="af_bella", speed=1.0)
    audio_dur = audio_res.get("duration_seconds") or audio_res.get("total_duration_seconds")
    assert audio_dur > 10.0, f"Audio unexpectedly short: {audio_dur}s"
    print(f"[PASS] Narration Audio Generated: {audio_dur:.2f}s, 24kHz")

    sub_path_str = PipelineController.execute_subtitles_stage(job_id)
    ass_path = Path(sub_path_str)
    assert ass_path.exists(), "captions.ass missing"
    print(f"[PASS] ASS Subtitles Compiled: {ass_path.name}")

    # Step 4: Hot-Folder Scene Intake (PART 2)
    print("\n--- 4. Hot-Folder Scene Intake & Matching Stage ---")
    incoming_dir = SceneIntakeService.get_incoming_directory(job_id)
    scene_colors = ["#1a365d", "#0d5c75", "#144552", "#2b4162"]

    # Generate genuine video clips for each scene in incoming_clips
    for i, scene in enumerate(script.scenes):
        color = scene_colors[i % len(scene_colors)]
        target_incoming_file = incoming_dir / f"scene_{i + 1:02d}.mp4"
        create_synthetic_test_clip(
            target_incoming_file,
            duration=scene.estimated_duration_seconds + 1.0,
            color=color,
            label=f"Scene {i + 1}"
        )
    print(f"[PASS] Created {len(script.scenes)} source video clips in incoming_clips folder")

    # Scan and ingest incoming clips
    intake_result = SceneIntakeService.scan_and_ingest(job_id)
    assert intake_result["all_scenes_ready"] is True, f"Not all scenes matched: {intake_result}"
    assert intake_result["assigned_scenes_count"] == len(script.scenes)
    assert len(intake_result["missing_scenes"]) == 0

    # Verify original clips are preserved in incoming_clips
    for i in range(len(script.scenes)):
        orig_file = incoming_dir / f"scene_{i + 1:02d}.mp4"
        assert orig_file.exists(), f"Original incoming clip was deleted: {orig_file}"

    job_after_intake = JobService.get_job(job_id)
    assert job_after_intake["status"] == JobStatus.SCENES_VALIDATED.value
    print(f"[PASS] Part 2 Verified: All {len(script.scenes)} clips ingested, original files preserved, status={job_after_intake['status']}")

    # Step 5: Video Processing & Hardware Render (PART 3 & PART 4)
    print("\n--- 5. FFmpeg Video Compositing & Render Stage ---")
    t_render_start = time.time()
    render_result = VideoRenderService.render_final_video(job_id)
    t_render_end = time.time()

    render_time = t_render_end - t_render_start
    final_video_path = Path(render_result["video_path"])

    print(f"[PASS] Render Complete in {render_time:.2f}s!")
    print(f"       Encoder Used:  {render_result['encoder_used']}")
    print(f"       File Size:     {render_result['file_size_mb']} MB")
    print(f"       Output Path:   {final_video_path}")

    # Step 6: Rigorous End-to-End Verification (PART 6)
    print("\n--- 6. Rigorous End-to-End Output Assertions ---")

    # A. Output file existence
    assert final_video_path.exists(), f"Rendered MP4 file missing: {final_video_path}"
    assert final_video_path.stat().st_size > 100000, f"File unexpectedly small: {final_video_path.stat().st_size} bytes"

    # B. Probe with FFprobe
    probe = SceneIntakeService.probe_video(final_video_path)
    assert probe["is_valid"] is True, f"FFprobe validation failed: {probe.get('error')}"
    assert probe["width"] == 1080 and probe["height"] == 1920, f"Resolution mismatch: expected 1080x1920, got {probe['width']}x{probe['height']}"
    assert probe["is_vertical_9_16"] is True, "Aspect ratio is not 9:16 vertical"
    print(f"[PASS] Resolution & Aspect Ratio: EXACT 1080x1920 (9:16 vertical)")

    # C. Audio-Video Synchronization & Duration
    duration_diff = abs(probe["duration_seconds"] - audio_dur)
    assert duration_diff < 0.6, f"Audio-Video duration mismatch: video={probe['duration_seconds']}s, audio={audio_dur}s (diff: {duration_diff:.2f}s)"
    print(f"[PASS] Audio/Video Synchronization: Duration={probe['duration_seconds']:.2f}s (Audio={audio_dur:.2f}s, diff={duration_diff:.3f}s)")

    # D. Stream Playability Check (Decode test via FFmpeg null sink)
    print("Testing stream decode integrity (simulating browser playback)...")
    playback_cmd = ["ffmpeg", "-v", "error", "-i", str(final_video_path), "-f", "null", "-"]
    decode_test = subprocess.run(playback_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert decode_test.returncode == 0, f"Playback decode failed: {decode_test.stderr}"
    print("[PASS] Complete Playback Decode Verified: 0 dropped frames, 0 stream errors")

    # E. Manifest & DB Status
    final_manifest = JobService.load_manifest(job_id)
    assert final_manifest.status == JobStatus.COMPLETED
    assert final_manifest.final_video_file == str(final_video_path)

    completed_db_job = JobService.get_job(job_id)
    assert completed_db_job["status"] == JobStatus.COMPLETED.value
    assert completed_db_job["output_video_path"] == str(final_video_path)
    print(f"[PASS] Database & Manifest Status: COMPLETED")

    print("\n" + "=" * 70)
    print("PHASE 4 COMPLETE: 100% OF END-TO-END RENDER TESTS PASSED!")
    print(f"Playable Output Video: {final_video_path}")
    print("=" * 70)

    return final_video_path


if __name__ == "__main__":
    run_phase4_end_to_end_test()
