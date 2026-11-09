import os
import sys
import time
import json
import shutil
import subprocess
from pathlib import Path

from backend.config import settings
from backend.database.connection import init_db
from backend.schemas.job_manifest import JobStatus, VisualStyle
from backend.schemas.script_schema import ScriptGenerationRequest
from backend.schemas.preset_schema import PRODUCTION_PRESETS, QualityMode
from backend.schemas.qc_schema import QCCheckStatus
from backend.schemas.consistency_schema import VisualReferenceSpec
from backend.services.job_service import JobService
from backend.services.pipeline_controller import PipelineController
from backend.services.scene_prompt_manager import ScenePromptManager
from backend.services.video_render_service import VideoRenderService
from backend.services.quality_control_service import QualityControlService
from backend.services.queue_manager import ProductionQueueManager

TEST_TOPIC = "The Hidden Bioluminescent Creatures of Mariana Trench"


def create_synthetic_test_clip(output_path: Path, duration: float, color: str):
    """Generates a genuine test clip using FFmpeg."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c={color}:s=1080x1920:d={duration:.2f}:r=30",
        "-t", f"{duration:.2f}",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "22",
        "-pix_fmt", "yuv420p",
        str(output_path)
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Failed to create test clip: {res.stderr.strip()}")


def run_phase5_verification():
    print("=" * 75)
    print("AI SHORTS STUDIO - PHASE 5 AUTOMATION, QC & OPTIMIZATION VERIFICATION")
    print("=" * 75)

    init_db()

    # =========================================================
    # PART 1: Production Presets Verification
    # =========================================================
    print("\n--- 1. Testing Production Presets ---")
    assert len(PRODUCTION_PRESETS) >= 4, "Must have at least 4 presets defined"
    preset_30 = PRODUCTION_PRESETS.get("preset_30s")
    preset_60 = PRODUCTION_PRESETS.get("preset_60s")
    assert preset_30 is not None and preset_30.target_duration_seconds == 30
    assert preset_60 is not None and preset_60.target_duration_seconds == 60
    assert preset_60.quality_mode == QualityMode.HIGH_QUALITY
    print(f"[PASS] Verified Presets: 30s ({preset_30.estimated_word_count} words), 60s ({preset_60.estimated_word_count} words)")

    # =========================================================
    # PART 2: Visual Reference & Character Continuity
    # =========================================================
    print("\n--- 2. Testing Character & Visual Reference Continuity ---")
    job = PipelineController.create_new_job(
        topic=TEST_TOPIC,
        title="Abyssal Bioluminescence",
        visual_style=VisualStyle.DEEP_DIVE_3D
    )
    job_id = job["id"]
    job_dir = JobService.get_job_directory(job_id)

    script_req = ScriptGenerationRequest(
        topic=TEST_TOPIC,
        target_duration=15.0,
        visual_style=VisualStyle.DEEP_DIVE_3D
    )
    script = PipelineController.execute_script_stage(job_id, generation_request=script_req)
    
    # Export prompts and reference sheet
    exported = ScenePromptManager.export_prompts(job_id, script)
    assert Path(exported["ref_json_path"]).exists(), "reference_sheet.json must exist"
    assert Path(exported["ref_txt_path"]).exists(), "reference_sheet.txt must exist"

    with open(exported["ref_json_path"], "r", encoding="utf-8") as f:
        ref_data = json.load(f)
    assert "primary_character" in ref_data
    assert "environment" in ref_data
    assert "color_palette" in ref_data["environment"]
    char_name = ref_data["primary_character"]["name"]
    print(f"[PASS] Reference Sheet Created: Character='{char_name}', Setting='{ref_data['environment']['setting_name']}'")

    # Verify scene prompts include consistency anchors
    with open(exported["json_path"], "r", encoding="utf-8") as f:
        prompts_data = json.load(f)
    first_prompt = prompts_data["prompts"][0]["production_prompt"]
    assert char_name in first_prompt or "Subject:" in first_prompt, "Scene prompt must include character anchor"
    assert "Setting:" in first_prompt or "Environment:" in first_prompt, "Scene prompt must include setting anchor"
    print(f"[PASS] Cross-Scene Prompt Anchoring Verified. Sample length: {len(first_prompt)} chars")

    # =========================================================
    # PART 3: Audio Polish with EBU R128 Normalization & Render
    # =========================================================
    print("\n--- 3. Audio Generation & EBU R128 Loudness Rendering ---")
    PipelineController.execute_audio_stage(job_id)
    PipelineController.execute_subtitles_stage(job_id)

    # Ingest synthetic video clips for each scene
    manifest = JobService.load_manifest(job_id)
    incoming_dir = job_dir / "incoming_clips"
    incoming_dir.mkdir(parents=True, exist_ok=True)
    colors = ["#1a365d", "#0077b6", "#023e8a", "#2b6cb0", "#0d5c75"]
    for i, s in enumerate(manifest.scenes):
        color = colors[i % len(colors)]
        clip_file = incoming_dir / f"scene_{i + 1:02d}.mp4"
        create_synthetic_test_clip(clip_file, duration=4.0, color=color)

    # Scan clips
    scan_res = PipelineController.scan_job_clips(job_id)
    assert scan_res["assigned_scenes_count"] == len(manifest.scenes)

    # Render video with EBU R128 loudness normalization
    t0 = time.time()
    render_res = VideoRenderService.render_final_video(job_id, quality_mode=QualityMode.BALANCED)
    t_render = time.time() - t0
    final_video = Path(render_res["video_path"])
    assert final_video.exists() and final_video.stat().st_size > 1000
    print(f"[PASS] Video Rendered in {t_render:.2f}s ({final_video.stat().st_size / (1024*1024):.2f} MB)")

    # =========================================================
    # PART 4: Automated Quality Control Gate
    # =========================================================
    print("\n--- 4. Automated Multi-Factor Quality Control (QC) ---")
    qc_report = QualityControlService.run_quality_control(job_id)
    print(f"[QC] Total Checks: {qc_report.total_checks}, Passed: {qc_report.passed_checks}, Status: {qc_report.overall_status.value}")
    for c in qc_report.checks:
        status_tag = f"[{c.status.value}]"
        print(f"     {status_tag:8} {c.name}: {c.message[:65]}...")

    assert qc_report.is_production_ready, "Compliant video must pass QC and be marked production ready"
    assert (job_dir / "output" / "quality_report.json").exists(), "quality_report.json must be saved in output"
    print("[PASS] Quality Control Gate Passed: Video is PRODUCTION READY.")

    # =========================================================
    # PART 5: Deliberate QC Failure Injection Test
    # =========================================================
    print("\n--- 5. Testing Deliberate QC Failure Injection ---")
    # Generate an invalid HORIZONTAL video (1920x1080 instead of 1080x1920)
    bad_video_file = job_dir / "output" / "bad_horizontal_video.mp4"
    cmd_bad = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "color=c=red:s=1920x1080:d=2:r=30",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        str(bad_video_file)
    ]
    subprocess.run(cmd_bad, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

    bad_report = QualityControlService.run_quality_control(job_id, video_path_override=str(bad_video_file))
    print(f"[QC Test] Deliberate Bad Video Result: Ready={bad_report.is_production_ready}, Status={bad_report.overall_status.value}")
    assert bad_report.is_production_ready is False, "Deliberate failure must NOT be production ready"
    assert bad_report.overall_status == QCCheckStatus.FAIL, "Deliberate failure status must be FAIL"
    failed_names = [c.name for c in bad_report.checks if c.status == QCCheckStatus.FAIL]
    assert any("Resolution" in name for name in failed_names), "Resolution check must fail for horizontal video"
    print(f"[PASS] QC Gate Correctly Blocked Defective Video. Failed checks: {failed_names}")

    # =========================================================
    # PART 6: Production Queue & Resource Protection
    # =========================================================
    print("\n--- 6. Testing Sequential Production Queue ---")
    qm = ProductionQueueManager.get_instance()

    # Create dummy jobs for queue testing
    job_q1 = PipelineController.create_new_job(topic="Queue Test Job 1", title="Queue Test 1")
    job_q2 = PipelineController.create_new_job(topic="Queue Test Job 2", title="Queue Test 2")

    # Enqueue Job 1
    enq1 = qm.enqueue_job(job_q1["id"], action="UP_TO_VISUALS")
    assert enq1["status"] == "success"
    print(f"[PASS] Job 1 Enqueued: {job_q1['id'][:8]}")

    # Duplicate Prevention Check: Try enqueueing Job 1 again
    enq_dup = qm.enqueue_job(job_q1["id"], action="UP_TO_VISUALS")
    assert enq_dup["status"] == "error", "Duplicate enqueue must be rejected"
    print(f"[PASS] Duplicate Enqueue Rejected: '{enq_dup['message']}'")

    # Enqueue Job 2
    enq2 = qm.enqueue_job(job_q2["id"], action="UP_TO_VISUALS")
    assert enq2["status"] == "success"
    print(f"[PASS] Job 2 Enqueued: {job_q2['id'][:8]}")

    # Test Cancellation of Job 2 while in queue
    cancelled = qm.cancel_job(job_q2["id"])
    assert cancelled is True, "Job 2 cancellation must succeed"
    print(f"[PASS] Job 2 Successfully Cancelled in Queue")

    # Let Queue Worker finish Job 1
    print("Waiting for Queue Worker to complete Job 1...")
    timeout = 30
    start_wait = time.time()
    while time.time() - start_wait < timeout:
        q_stat = qm.get_status()
        if q_stat["active_job"] is None and q_stat["queued_count"] == 0:
            break
        time.sleep(1)

    # Verify Job 1 completed via Queue Worker
    manifest_q1 = JobService.load_manifest(job_q1["id"])
    assert manifest_q1.audio_file is not None and Path(manifest_q1.audio_file).exists()
    assert manifest_q1.subtitle_file is not None and Path(manifest_q1.subtitle_file).exists()
    print(f"[PASS] Queue Worker Executed Job 1 Sequentially: Audio and Subtitles Verified!")

    print("\n" + "=" * 75)
    print("ALL PHASE 5 AUTOMATION, QC, CONTINUITY & QUEUE TESTS PASSED (100%)")
    print("=" * 75)


if __name__ == "__main__":
    run_phase5_verification()
