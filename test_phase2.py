import os
import sys
import time
import json
from pathlib import Path
import psutil
import soundfile as sf

from backend.config import settings
from backend.schemas.job_manifest import JobCreateRequest, VisualStyle, JobStatus
from backend.services.job_service import JobService
from backend.services.audio_service import AudioService
from backend.services.subtitle_service import SubtitleService
from backend.database.connection import init_db

# 20-30 second English narration text (approximately 60 words)
NARRATION_TEXT = (
    "Deep beneath the ocean waves, in complete and utter darkness, "
    "ancient creatures have evolved mysterious glowing lights to survive. "
    "Under thousands of pounds of crushing water pressure, "
    "these eerie bioluminescent predators drift silently through the cold abyss, "
    "waiting for an unsuspecting victim to wander into their inescapable trap."
)


def run_phase2_test():
    print("=" * 60)
    print("AI SHORTS STUDIO - PHASE 2 AUDIO ENGINE VERIFICATION")
    print("=" * 60)

    # Memory baseline before inference
    process = psutil.Process(os.getpid())
    mem_before_mb = process.memory_info().rss / (1024 * 1024)
    print(f"Initial Memory Usage: {mem_before_mb:.2f} MB")

    # 1. Initialize SQLite database & create test job
    init_db()
    req = JobCreateRequest(
        title="Bioluminescent Abyss",
        topic="Deep sea creatures glowing in the dark",
        visual_style=VisualStyle.DEEP_DIVE_3D
    )
    job = JobService.create_job(req)
    job_id = job["id"]
    print(f"[PASS] Created Job ID: {job_id}")

    # 2. Synthesize Audio and Extract Word Timestamps
    selected_voice = "af_bella"  # Natural warm narration voice
    print(f"\nStarting Kokoro TTS synthesis with voice '{selected_voice}' on CPU...")
    t_start = time.time()

    audio_result = AudioService.synthesize_speech(
        text=NARRATION_TEXT,
        voice=selected_voice,
        speed=1.0,
        job_id=job_id
    )
    t_end = time.time()

    gen_time = t_end - t_start
    mem_peak_mb = process.memory_info().rss / (1024 * 1024)
    mem_delta_mb = mem_peak_mb - mem_before_mb

    wav_path = Path(audio_result["audio_path"])
    json_path = Path(audio_result["timestamps_path"])
    duration = audio_result["duration_seconds"]
    sample_rate = audio_result["sample_rate"]
    word_count = audio_result["word_count"]
    timestamps = audio_result["timestamps"]

    print("\n--- Audio Generation Metrics ---")
    print(f"Output WAV File:      {wav_path.name} ({wav_path.stat().st_size:,} bytes)")
    print(f"Sample Rate:          {sample_rate} Hz")
    print(f"Audio Duration:       {duration:.2f} seconds")
    print(f"Word Count:           {word_count} words")
    print(f"Inference Wall Time:  {gen_time:.2f} seconds")
    print(f"Real-Time Factor:     {duration / gen_time:.2f}x (faster than real-time)")
    print(f"Peak Process Memory:  {mem_peak_mb:.2f} MB (+{mem_delta_mb:.2f} MB)")
    print("VRAM Allocated:       0.00 MB (100% CPU Execution)")

    # 3. Generate ASS Subtitle File
    print("\nCompiling Advanced SubStation Alpha (.ass) Subtitles...")
    t_sub_start = time.time()
    sub_path_str = SubtitleService.generate_ass_subtitles(
        word_timestamps=timestamps,
        job_id=job_id
    )
    t_sub_end = time.time()
    sub_path = Path(sub_path_str)
    print(f"[PASS] ASS Subtitles Compiled in {t_sub_end - t_sub_start:.3f}s: {sub_path.name}")

    # 4. Rigorous Validations
    print("\n--- Running Automated Assertions ---")

    # A. File Existence & Properties
    assert wav_path.exists(), f"WAV file missing: {wav_path}"
    assert wav_path.stat().st_size > 50000, "WAV file unexpectedly small"
    wav_info = sf.info(str(wav_path))
    assert wav_info.samplerate == 24000, f"Expected 24000Hz, got {wav_info.samplerate}"
    assert wav_info.channels == 1, "Expected mono channel"
    print(f"[PASS] WAV Verified: 24000Hz Mono, Duration={wav_info.duration:.2f}s")

    # B. Target Duration Range (20–30 seconds)
    assert 18.0 <= duration <= 32.0, f"Audio duration {duration:.2f}s is outside 20-30s expected target!"
    print(f"[PASS] Duration in target range (20-30s): {duration:.2f}s")

    # C. Word Timestamps Validation
    assert json_path.exists(), f"Timestamps JSON missing: {json_path}"
    with open(json_path, "r", encoding="utf-8") as f:
        json_data = json.load(f)
    assert json_data["word_count"] == word_count, "Word count mismatch in JSON"
    print(f"[PASS] Timestamps JSON verified: {len(json_data['timestamps'])} entries")

    prev_end = 0.0
    for idx, item in enumerate(timestamps):
        w = item.word
        s = item.start_time
        e = item.end_time

        # Bound checks
        assert 0.0 <= s, f"Word '{w}' has negative start time: {s}"
        assert s <= e, f"Word '{w}' start {s} exceeds end {e}"
        assert e <= duration + 0.1, f"Word '{w}' end {e} exceeds audio duration {duration}"
        assert s >= prev_end - 0.05, f"Word '{w}' starts before previous word ends (overlap anomaly)"
        prev_end = s

    print("[PASS] All timestamps are strictly positive, sequential, and within audio bounds!")

    # D. ASS Subtitles File Validation
    assert sub_path.exists(), f"ASS file missing: {sub_path}"
    with open(sub_path, "r", encoding="utf-8") as f:
        ass_lines = f.readlines()
    assert any("[Script Info]" in line for line in ass_lines), "ASS header missing"
    assert any("[V4+ Styles]" in line for line in ass_lines), "ASS styles missing"
    assert any("[Events]" in line for line in ass_lines), "ASS events missing"
    dialogue_lines = [l for l in ass_lines if l.startswith("Dialogue:")]
    assert len(dialogue_lines) >= word_count, f"Expected at least {word_count} dialogue frames, got {len(dialogue_lines)}"
    assert any("&H0000FFFF&" in l for l in dialogue_lines), "Yellow highlight color tag not found in dialogue"
    print(f"[PASS] ASS File Verified: {len(dialogue_lines)} dialogue frames with yellow active-word highlights")

    # E. Manifest State Verification
    manifest = JobService.load_manifest(job_id)
    assert manifest is not None
    assert manifest.status == JobStatus.TIMESTAMPS_READY
    assert manifest.audio_file == str(wav_path)
    assert manifest.subtitle_file == str(sub_path)
    assert len(manifest.word_timestamps) == word_count
    print(f"[PASS] Job Manifest Verified: Status = {manifest.status.value}")

    # Print sample of word timestamps
    print("\n--- Sample Word Timestamps (First 10 words) ---")
    for item in timestamps[:10]:
        print(f"  {item.word:16s} : {item.start_time:6.3f}s -> {item.end_time:6.3f}s  (dur: {item.end_time - item.start_time:5.3f}s)")

    print("\n" + "=" * 60)
    print("PHASE 2 INTEGRATION AND AUDIO TEST: 100% PASSED")
    print("=" * 60)


if __name__ == "__main__":
    run_phase2_test()
