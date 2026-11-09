import os
import sys
import time
import json
from pathlib import Path
import psutil
import soundfile as sf

from backend.config import settings
from backend.schemas.job_manifest import JobStatus, VisualStyle
from backend.schemas.script_schema import (
    ShortsScript,
    SceneScript,
    ScriptGenerationRequest
)
from backend.services.job_service import JobService
from backend.services.script_service import ScriptService
from backend.services.pipeline_controller import PipelineController
from backend.database.connection import init_db, get_db

SAMPLE_TOPIC = "The Secrets of Deep Sea Bioluminescence"


def run_phase3_test():
    print("=" * 65)
    print("AI SHORTS STUDIO - PHASE 3 CENTRAL CONTROLLER & SCRIPT TEST")
    print("=" * 65)

    # 1. Test Script JSON Schema Validation
    print("\n--- 1. Testing Script JSON Schema & Validation ---")
    valid_request = ScriptGenerationRequest(
        topic=SAMPLE_TOPIC,
        target_duration=22.0,
        visual_style=VisualStyle.DEEP_DIVE_3D,
        target_audience="curious young adults",
        tone="eerie, cinematic, captivating"
    )
    generated_script = ScriptService.generate_script(valid_request)
    assert generated_script.title, "Title is empty"
    assert generated_script.hook, "Hook is empty"
    assert len(generated_script.scenes) >= 3, "Insufficient scenes"
    for s in generated_script.scenes:
        assert s.narration, f"Scene {s.index} missing narration"
        assert s.visual_description, f"Scene {s.index} missing visual description"
        assert s.camera_movement, f"Scene {s.index} missing camera instruction"
        assert s.estimated_duration_seconds > 0, f"Scene {s.index} invalid duration"
    print(f"[PASS] Script Schema Validated: '{generated_script.title}' ({len(generated_script.scenes)} scenes)")

    # 2. Test Job Creation
    print("\n--- 2. Testing Job Creation via Controller ---")
    init_db()
    job = PipelineController.create_new_job(
        topic=SAMPLE_TOPIC,
        title="Deep Sea Glowing Secrets",
        visual_style=VisualStyle.DEEP_DIVE_3D
    )
    job_id = job["id"]
    assert job["status"] == JobStatus.DRAFT.value
    job_dir = JobService.get_job_directory(job_id)
    manifest = JobService.load_manifest(job_id)
    assert manifest is not None
    assert manifest.status == JobStatus.DRAFT
    print(f"[PASS] Job Created: ID={job_id}, Workspace={job_dir.name}")

    # 3. Test Scene & Script Generation Stage
    print("\n--- 3. Testing Script & Scene Generation Stage ---")
    script = PipelineController.execute_script_stage(job_id, generation_request=valid_request)
    script_file = job_dir / "script.json"
    assert script_file.exists(), "script.json was not saved to job folder"

    # Verify SQLite scenes records
    updated_job = JobService.get_job(job_id)
    assert updated_job["status"] == JobStatus.SCRIPT_READY.value
    assert len(updated_job["scenes"]) == len(script.scenes)
    print(f"[PASS] Script Stage Complete: status={updated_job['status']}, scenes={len(updated_job['scenes'])}")

    # 4. Test Audio Stage via Controller (Kokoro ONNX)
    print("\n--- 4. Testing Kokoro Audio Stage via Controller ---")
    t0 = time.time()
    audio_res = PipelineController.execute_audio_stage(job_id, voice="af_bella", speed=1.0)
    t_audio = time.time() - t0

    wav_file = job_dir / "audio" / "narration.wav"
    assert wav_file.exists(), "narration.wav missing"
    wav_info = sf.info(str(wav_file))
    assert wav_info.samplerate == 24000
    duration = audio_res.get("duration_seconds") or audio_res.get("total_duration_seconds")
    assert duration > 10.0, f"Unexpected short duration: {duration}s"

    job_after_audio = JobService.get_job(job_id)
    assert job_after_audio["status"] == JobStatus.AUDIO_READY.value
    print(f"[PASS] Audio Stage Complete in {t_audio:.2f}s: duration={duration:.2f}s, status={job_after_audio['status']}")

    # 5. Test Word Timestamps Verification
    print("\n--- 5. Testing Word Timestamps Extraction ---")
    words_file = job_dir / "audio" / "words.json"
    assert words_file.exists(), "words.json missing"
    with open(words_file, "r", encoding="utf-8") as f:
        words_data = json.load(f)
    timestamps = words_data["timestamps"]
    assert len(timestamps) > 20, "Insufficient word timestamps"

    for t in timestamps:
        assert 0.0 <= t["start_time"] <= t["end_time"] <= duration + 0.1
    print(f"[PASS] Timestamps Verified: {len(timestamps)} words, all bounded within {duration:.2f}s")

    # 6. Test Subtitle Stage via Controller (ASS Generation)
    print("\n--- 6. Testing ASS Subtitle Generation Stage ---")
    sub_file_path = PipelineController.execute_subtitles_stage(job_id)
    ass_file = Path(sub_file_path)
    assert ass_file.exists(), "captions.ass missing"
    with open(ass_file, "r", encoding="utf-8") as f:
        ass_content = f.read()
    assert "[V4+ Styles]" in ass_content
    assert "&H0000FFFF&" in ass_content  # Active yellow highlight tag
    assert "Dialogue:" in ass_content

    job_after_sub = JobService.get_job(job_id)
    assert job_after_sub["status"] == JobStatus.VISUALS_PENDING.value
    print(f"[PASS] Subtitle Stage Complete: status={job_after_sub['status']}, file={ass_file.name}")

    # 7. Test Manifest Synchronization
    print("\n--- 7. Testing Manifest Updates ---")
    final_manifest = JobService.load_manifest(job_id)
    assert final_manifest.status == JobStatus.VISUALS_PENDING
    assert final_manifest.audio_file == str(wav_file)
    assert final_manifest.subtitle_file == str(ass_file)
    assert len(final_manifest.word_timestamps) == len(timestamps)
    assert len(final_manifest.scenes) == len(script.scenes)
    print(f"[PASS] Manifest Synchronized: Status={final_manifest.status.value}, Scenes={len(final_manifest.scenes)}")

    # 8. Test Retry and Resumption Behaviour
    print("\n--- 8. Testing Resumption and Retry Behaviour ---")
    # Record modification times of existing assets
    mtime_script_orig = script_file.stat().st_mtime_ns
    mtime_wav_orig = wav_file.stat().st_mtime_ns
    mtime_ass_orig = ass_file.stat().st_mtime_ns

    # Simulate an error by marking job as FAILED
    PipelineController._mark_job_failed(job_id, "SIMULATED_TEST", "Simulated transient failure for testing")
    failed_job = JobService.get_job(job_id)
    assert failed_job["status"] == JobStatus.FAILED.value
    print(f"[PASS] Artificially marked job as FAILED: error={failed_job['error_message']}")

    # Now call PipelineController.retry_job(job_id)
    t_retry_start = time.time()
    retry_result = PipelineController.retry_job(job_id)
    t_retry_end = time.time()

    retried_job = JobService.get_job(job_id)
    assert retried_job["status"] == JobStatus.VISUALS_PENDING.value
    assert retried_job["error_message"] is None, "Error message should be cleared upon recovery"

    # Verify that existing assets were REUSED without costly re-generation
    mtime_script_after = script_file.stat().st_mtime_ns
    mtime_wav_after = wav_file.stat().st_mtime_ns
    mtime_ass_after = ass_file.stat().st_mtime_ns

    assert mtime_script_orig == mtime_script_after, "Script was unnecessarily re-generated!"
    assert mtime_wav_orig == mtime_wav_after, "Audio WAV was unnecessarily re-synthesized!"
    assert mtime_ass_orig == mtime_ass_after, "Subtitles were unnecessarily re-compiled!"

    print(f"[PASS] Pipeline Resumed & Recovered in {t_retry_end - t_retry_start:.3f}s!")
    print(f"       Assets preserved: script (OK), audio (OK), subtitles (OK).")
    print(f"       Final Job Status: {retried_job['status']}")

    print("\n" + "=" * 65)
    print("PHASE 3 ALL INTEGRATION & CONTROLLER TESTS: 100% PASSED")
    print("=" * 65)


if __name__ == "__main__":
    run_phase3_test()
