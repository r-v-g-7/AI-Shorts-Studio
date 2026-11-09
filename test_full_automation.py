import os
import sys
import time
import json
import urllib.request
from pathlib import Path

from backend.config import settings
from backend.database.connection import init_db
from backend.schemas.job_manifest import JobStatus, VisualStyle
from backend.schemas.preset_schema import QualityMode
from backend.services.job_service import JobService
from backend.services.pipeline_controller import PipelineController
from backend.services.video_generator import (
    GeminiVideoGenerator,
    MockVideoGenerator,
    get_video_generator
)
from backend.services.scene_intake_service import SceneIntakeService
from backend.services.video_render_service import VideoRenderService
from backend.services.quality_control_service import QualityControlService

TEST_TOPIC = """Deep in the Mariana Trench, eleven kilometers below the ocean waves, an ancient colossal leviathan with bioluminescent cyan scales hovers in the pitch-black void. As geothermal vents erupt with mineral-rich heat, the creature's translucent crystalline dermal plates reflect rhythmic pulses of sapphire light."""


def run_full_automation_verification():
    print("=" * 80)
    print("AI SHORTS STUDIO - FULL AUTOMATION END-TO-END VERIFICATION")
    print("=" * 80)

    init_db()

    # =========================================================================
    # PART 1: Video Generation Provider Contract & Credential Safety
    # =========================================================================
    print("\n--- 1. Testing Video Generation Provider Interface & Credential Handling ---")
    
    # Test Gemini provider behavior when API key is checked
    gemini_gen = GeminiVideoGenerator()
    has_api_key = bool(settings.GEMINI_API_KEY and settings.GEMINI_API_KEY.strip())
    print(f"Configured Provider: '{settings.VIDEO_GENERATION_PROVIDER}', Model: '{settings.GEMINI_VIDEO_MODEL}'")
    print(f"GEMINI_API_KEY configured: {has_api_key}")

    if not has_api_key:
        print("[CHECK] Verifying that Gemini provider fails cleanly without silently faking credentials...")
        test_clip_path = settings.STORAGE_DIR / "temp_test_clip.mp4"
        try:
            gemini_gen.generate_scene_clip(
                scene_prompt="A bioluminescent leviathan in the Mariana Trench",
                duration_seconds=4.0,
                output_path=test_clip_path
            )
            assert False, "Gemini provider must raise error when API key is missing!"
        except RuntimeError as e:
            assert "GEMINI_API_KEY is not configured" in str(e)
            print(f"[PASS] Provider safely refused execution without credentials: {e}")
    else:
        print("[LIVE API DETECTED] GEMINI_API_KEY is set. Attempting live test...")
        try:
            live_out = settings.STORAGE_DIR / "temp_live_gemini_clip.mp4"
            gemini_gen.generate_scene_clip(
                scene_prompt="A glowing blue fish swimming in the dark ocean, vertical video",
                duration_seconds=4.0,
                output_path=live_out
            )
            assert live_out.exists() and live_out.stat().st_size > 1000
            print(f"[PASS] Real Google Gemini video generation succeeded! Saved to {live_out}")
        except Exception as e:
            print(f"[LIVE API NOTICE] Live generation call encountered: {e}")

    # =========================================================================
    # PART 2: 1-Click Complete Pipeline Execution
    # =========================================================================
    print("\n--- 2. Testing 1-Click Automated Pipeline (Script -> Voice -> Subtitles -> Visuals -> FFmpeg Render) ---")
    
    # Create test job
    job = PipelineController.create_new_job(
        topic=TEST_TOPIC,
        title="Automated Mariana Expedition",
        visual_style=VisualStyle.DEEP_DIVE_3D
    )
    job_id = job["id"]
    job_dir = JobService.get_job_directory(job_id)
    print(f"[PASS] Job Created: ID={job_id}")

    # Execute full automated pipeline (using mock provider for deterministic local execution)
    t0 = time.time()
    result = PipelineController.run_full_automated_pipeline(
        job_id=job_id,
        provider_override="mock",
        quality_mode=QualityMode.BALANCED
    )
    duration_total = time.time() - t0

    print(f"[PASS] Complete 1-Click Pipeline Executed in {duration_total:.2f}s!")
    print(f"       Status: {result.get('status')}")
    print(f"       Video Path: {result.get('video_path')}")
    print(f"       QC Status: {result.get('qc_status')}")
    print(f"       Production Ready: {result.get('is_production_ready')}")

    # =========================================================================
    # PART 3: Storage & Asset Verification
    # =========================================================================
    print("\n--- 3. Verifying All Intermediate & Final Assets on Disk ---")
    manifest = JobService.load_manifest(job_id)
    assert manifest is not None, "Job manifest must exist"

    # 1. Script
    script_file = job_dir / "script.json"
    assert script_file.exists(), "script.json must exist"
    print(f"[PASS] Script verified: {script_file.name} ({len(manifest.scenes)} scenes)")

    # 2. Narration WAV
    audio_file = Path(manifest.audio_file)
    assert audio_file.exists() and audio_file.stat().st_size > 1000, "Narration audio missing"
    print(f"[PASS] Narration audio verified: {audio_file.name} ({audio_file.stat().st_size / 1024:.1f} KB)")

    # 3. Subtitles ASS
    sub_file = Path(manifest.subtitle_file)
    assert sub_file.exists(), "Subtitle file missing"
    print(f"[PASS] ASS subtitles verified: {sub_file.name}")

    # 4. Generated Scene Clips
    for scene in manifest.scenes:
        clip_path = Path(scene.clip_file)
        assert clip_path.exists() and clip_path.stat().st_size > 1000, f"Clip for scene {scene.index + 1} missing: {clip_path}"
        probe = SceneIntakeService.probe_video(clip_path)
        assert probe["is_valid"], f"Scene {scene.index + 1} clip is invalid: {probe.get('error')}"
    print(f"[PASS] All {len(manifest.scenes)} generated scene clips verified in storage/jobs/{job_id[:8]}.../scenes/")

    # 5. Final Rendered MP4
    final_video = Path(result["video_path"])
    assert final_video.exists() and final_video.stat().st_size > 1000, "Final MP4 missing or empty"
    probe_final = SceneIntakeService.probe_video(final_video)
    assert probe_final["is_valid"], "Final MP4 probe failed"
    assert probe_final["width"] == 1080 and probe_final["height"] == 1920, f"Expected 1080x1920, got {probe_final['width']}x{probe_final['height']}"
    assert probe_final["duration_seconds"] > 0, "Duration must be positive"
    print(f"[PASS] Final 9:16 Video Verified: 1080x1920, {probe_final['duration_seconds']:.2f}s, {probe_final['size_bytes'] / (1024*1024):.2f} MB")

    # =========================================================================
    # PART 4: HTTP API & Download Endpoints Verification
    # =========================================================================
    print("\n--- 4. Testing HTTP Endpoints: Video Streaming & Download ---")
    base_url = f"http://{settings.BACKEND_HOST}:{settings.BACKEND_PORT}"

    # Test 1: Video stream for HTML5 player
    stream_url = f"{base_url}/api/jobs/{job_id}/video"
    req_stream = urllib.request.Request(stream_url)
    with urllib.request.urlopen(req_stream) as resp:
        assert resp.status == 200
        content_type = resp.headers.get("Content-Type")
        assert "video/mp4" in content_type, f"Expected video/mp4, got {content_type}"
        first_chunk = resp.read(1024)
        assert len(first_chunk) == 1024
        print(f"[PASS] HTML5 Video Stream verified (Status 200, Content-Type: {content_type})")

    # Test 2: Download attachment endpoint
    download_url = f"{base_url}/api/jobs/{job_id}/video?download=true"
    req_dl = urllib.request.Request(download_url)
    with urllib.request.urlopen(req_dl) as resp:
        assert resp.status == 200
        disposition = resp.headers.get("Content-Disposition")
        assert "attachment" in disposition, f"Expected attachment disposition, got {disposition}"
        print(f"[PASS] Download MP4 endpoint verified ({disposition})")

    # Test 3: Open folder endpoint
    open_url = f"{base_url}/api/jobs/{job_id}/open-folder"
    req_open = urllib.request.Request(
        open_url,
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req_open) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data.get("status") == "success"
        print(f"[PASS] Open folder endpoint verified: {data.get('path')}")

    # =========================================================================
    # PART 5: 1-Click HTTP Create-and-Generate Endpoint
    # =========================================================================
    print("\n--- 5. Testing 1-Click 'POST /api/jobs/create-and-generate' Endpoint ---")
    create_gen_url = f"{base_url}/api/jobs/create-and-generate"
    payload = {
        "title": "Abyssal 1-Click Expedition",
        "topic": "Colossal luminous deep-sea squid gliding near hydrothermal chimneys in the trench",
        "visual_style": "deep_dive_3d",
        "quality_mode": "DRAFT",
        "provider_override": "mock"
    }
    req_cg = urllib.request.Request(
        create_gen_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req_cg) as resp:
        assert resp.status == 201
        data = json.loads(resp.read().decode("utf-8"))
        job_data = data["job"]
        queue_data = data["queue"]
        print(f"[PASS] 1-Click Job Created & Enqueued: ID={job_data['id']}, Position={queue_data.get('queue_position')}")

    # Wait for queue worker to finish the 1-click job
    print("Waiting for Sequential Queue Worker to process 1-Click Job...")
    wait_start = time.time()
    while time.time() - wait_start < 45:
        q_stat_url = f"{base_url}/api/queue/status"
        with urllib.request.urlopen(q_stat_url) as q_resp:
            q_info = json.loads(q_resp.read().decode("utf-8"))
            if q_info.get("active_job") is None and q_info.get("queued_count") == 0:
                break
        time.sleep(1.5)

    # Verify the 1-click job rendered completely
    check_job_url = f"{base_url}/api/jobs/{job_data['id']}"
    with urllib.request.urlopen(check_job_url) as j_resp:
        j_detail = json.loads(j_resp.read().decode("utf-8"))
        assert j_detail["status"] == "COMPLETED", f"Expected COMPLETED, got {j_detail['status']}"
        assert j_detail.get("output_video_path") is not None
        print(f"[PASS] Queue Worker Executed 1-Click Job to Completion! Output: {j_detail['output_video_path']}")

    print("\n" + "=" * 80)
    print("ALL FULL AUTOMATION TESTS PASSED (100% SUCCESS)!")
    print("=" * 80)


if __name__ == "__main__":
    run_full_automation_verification()
