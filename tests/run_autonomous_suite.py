"""
Autonomous End-to-End Test Suite for AI Faceless Shorts Studio:
  TEST 1: Script & SSML Engine (100% Conversational Delhi/UP Hindi, 4 Escalating Beats, Calibrated SSML Breaks & Prosody)
  TEST 2: State & Watcher Deduplication (1-to-1 Scene Mapping, 10s Duplicate Size Rejection, Disk Sync)
  TEST 3: Flow CDP Attachment & Element Targeting (Port 9222, Prompt Clear/Type, Card Focus & Download Selectors)
  TEST 4: Full End-to-End Render (Live Flow 4-Scene Generation & Download, FFmpeg 9:16 Composite + EBU R128 + ASS Subtitles, >5MB MP4 Verification)
"""

import asyncio
import shutil
import subprocess
import sys
import time
from pathlib import Path

from backend.config import settings
from backend.schemas.job_manifest import JobStatus, VisualStyle
from backend.schemas.preset_schema import QualityMode
from backend.schemas.script_schema import ScriptGenerationRequest
from backend.services.audio_service import AudioService
from backend.services.downloads_watcher import DownloadsWatcher
from backend.services.flow_automation_service import FlowAutomationService
from backend.services.job_service import JobService
from backend.services.pipeline_controller import PipelineController
from backend.services.scene_intake_service import SceneIntakeService
from backend.services.script_service import ScriptService
from backend.services.video_render_service import VideoRenderService


TOPIC = (
    "The Angriest Pressure Cooker in Delhi: A battered old Prestige pressure cooker in a noisy "
    "Indian joint family kitchen complains about being forced to whistle 8 times every morning for "
    "boiling yellow dal while everyone ignores it, threatening to blow its red safety valve straight "
    "into the spinning ceiling fan."
)


def banner(title: str) -> None:
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80, flush=True)


def run_test_1_script_and_ssml() -> None:
    banner("TEST 1: SCRIPT & SSML ENGINE")

    req = ScriptGenerationRequest(
        topic=TOPIC,
        visual_style=VisualStyle.DEEP_DIVE_3D,
        target_duration=25.0,
        language="hinglish",
    )
    script = ScriptService.generate_script(req)

    # Assert 4 scenes
    assert len(script.scenes) == 4, f"Expected 4 scenes, got {len(script.scenes)}"

    # Assert 100% conversational Delhi/UP Hindi markers
    full_text = script.complete_narration.lower()
    assert any(k in full_text for k in ("arre yaar", "kasam se", "bhai sahab")), (
        f"Expected colloquial Delhi/UP Hindi idioms in narration: {script.complete_narration}"
    )

    # Assert 4 distinct camera choreographies & visual beats
    cameras = [s.camera_movement for s in script.scenes]
    assert len(set(cameras)) == 4, f"Expected 4 distinct camera movements, got {cameras}"
    assert "worm's-eye macro probe" in script.scenes[0].visual_description.lower()
    assert "medium wide profile" in script.scenes[1].visual_description.lower()
    assert "dutch-angle close-up" in script.scenes[2].visual_description.lower()
    assert "high-speed vertical tracking" in script.scenes[3].visual_description.lower()

    print(f"[TEST 1] Generated Title: {script.title}")
    for sc in script.scenes:
        print(f"  Scene {sc.index + 1}:")
        print(f"    Narration: {sc.narration}")
        print(f"    Camera:    {sc.camera_movement}")
        print(f"    Visual:    {sc.visual_description}")

    # Verify SSML formatting tokens
    scene1_ssml = AudioService.format_comedic_ssml(
        script.scenes[0].narration, voice="hi-IN-MadhurNeural", rate="-6%"
    )
    assert '<break time="500ms"/>' in scene1_ssml, "Missing 500ms ellipsis break in SSML"
    assert '<break time="400ms"/>' in scene1_ssml, "Missing 400ms exclamation break in SSML"
    assert 'pitch="+4Hz"' in scene1_ssml, "Missing +4Hz exclamation pitch contour in SSML"
    assert 'rate="-6%"' in scene1_ssml, "Missing -6% baseline rate in SSML"
    print(f"[TEST 1] Verified SSML Output:\n  {scene1_ssml}")

    # Synthesize Scene 1 audio & export MP3 + WAV to verify audible pause/breath synthesis
    test_audio_dir = settings.STORAGE_DIR / "_test_suite_audio"
    test_audio_dir.mkdir(parents=True, exist_ok=True)
    audio_res = AudioService.synthesize_speech(
        text=script.scenes[0].narration,
        voice="hi-IN-MadhurNeural",
        speed=0.94,
    )
    wav_path = Path(audio_res["audio_path"])
    assert wav_path.exists() and wav_path.stat().st_size > 10_000, "Synthesized WAV is missing or too small"

    mp3_path = test_audio_dir / "scene_01_test.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(wav_path), "-codec:a", "libmp3lame", "-qscale:a", "2", str(mp3_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert mp3_path.exists() and mp3_path.stat().st_size > 5_000, "Valid MP3 was not generated"
    print(
        f"[TEST 1 PASSED] Scene 1 audio synthesized -> {mp3_path.name} "
        f"({mp3_path.stat().st_size:,} bytes, duration={audio_res['duration_seconds']:.2f}s, "
        f"words={audio_res['word_count']})"
    )


def run_test_2_watcher_deduplication() -> None:
    banner("TEST 2: STATE & WATCHER DEDUPLICATION")

    # Create an isolated test job and sample valid >1MB video file
    temp_job = PipelineController.create_new_job(
        topic=TOPIC,
        title="Watcher Dedup Test Job",
        visual_style=VisualStyle.DEEP_DIVE_3D,
    )
    temp_job_id = temp_job["id"]
    PipelineController.execute_script_stage(temp_job_id, force_regenerate=True)
    JobService.update_job_status(temp_job_id, JobStatus.AWAITING_CLIPS)

    temp_dl_dir = settings.STORAGE_DIR / "_test_suite_downloads"
    if temp_dl_dir.exists():
        shutil.rmtree(temp_dl_dir)
    temp_dl_dir.mkdir(parents=True, exist_ok=True)

    # Locate an existing real >1MB clip or generate a synthetic >1.2MB test MP4
    sample_mp4 = temp_dl_dir / "_master_sample.mp4"
    existing_samples = list(settings.JOBS_STORAGE_DIR.glob("*/incoming_clips/scene_01.mp4"))
    valid_existing = [p for p in existing_samples if p.exists() and p.stat().st_size > 1_200_000]
    if valid_existing:
        shutil.copy2(str(valid_existing[0]), str(sample_mp4))
    else:
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=5",
                "-c:v", "libx264", "-b:v", "3M", "-minrate", "3M", "-maxrate", "4M", "-pix_fmt", "yuv420p",
                str(sample_mp4),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    watcher = DownloadsWatcher(
        downloads_dir=temp_dl_dir,
        poll_seconds=0.1,
        stable_seconds=0.0,
        pinned_job_id=temp_job_id,
        auto_render=False,
        dedup_window_seconds=10.0,
    )
    watcher.sync_job_disk_state(temp_job_id, reset_clips=True)
    watcher._baseline = {
        (str(sample_mp4), sample_mp4.stat().st_size, int(sample_mp4.stat().st_mtime))
    }

    # Drop two identical-size files into the watched folder within the 10s window
    dl_file_1 = temp_dl_dir / "Flow_Generated_Clip_A.mp4"
    dl_file_2 = temp_dl_dir / "Flow_Generated_Clip_A (1).mp4"
    shutil.copy2(str(sample_mp4), str(dl_file_1))
    shutil.copy2(str(sample_mp4), str(dl_file_2))

    # Pass 1 observes initial file sizes; Pass 2 sees stable sizes and ingests Scene 1 while rejecting the duplicate
    watcher.scan_once()
    watcher.scan_once()

    existing_indices, missing_indices = SceneIntakeService.get_existing_and_missing_scenes(
        temp_job_id, total_scenes=4
    )
    print(
        f"[TEST 2] After dropping 2 identical files -> existing_indices={existing_indices}, "
        f"missing_indices={missing_indices}, watcher.current_scene_index={watcher.current_scene_index}"
    )

    assert existing_indices == [0], f"Expected only Scene 1 ([0]) assigned, got {existing_indices}"
    assert missing_indices == [1, 2, 3], f"Expected [1, 2, 3] missing, got {missing_indices}"
    assert watcher.current_scene_index == 1, (
        f"Expected current_scene_index=1 (Scene 2), got {watcher.current_scene_index}"
    )

    # Clean up temporary dedup job status so it does not compete with live job
    JobService.update_job_status(temp_job_id, JobStatus.DRAFT)
    shutil.rmtree(temp_dl_dir, ignore_errors=True)
    print("[TEST 2 PASSED] Watcher assigned scene_01.mp4 and rejected the duplicate without advancing scene counter!")


async def run_test_3_flow_cdp_attachment() -> None:
    banner("TEST 3: FLOW CDP ATTACHMENT & ELEMENT TARGETING")

    svc = FlowAutomationService()
    assert svc.is_cdp_reachable(), f"Chrome CDP is not reachable at {svc.cdp_url}"

    try:
        state = await svc.inspect_browser_state()
        assert state["connected"] is True, "Failed to connect to Chrome CDP"
        assert "flow.google.com" in state["flow_tab"]["url"] or "labs.google" in state["flow_tab"]["url"], (
            f"Unexpected Flow tab URL: {state['flow_tab']['url']}"
        )
        print(f"[TEST 3] Connected to Google Flow tab: {state['flow_tab']['url']}")

        page = svc._page
        await svc._close_inspector_panel(page)

        # Test prompt box clear and test typing
        prompt_loc = await svc._find_prompt_locator(page)
        assert prompt_loc is not None, "Prompt input locator not found on Google Flow canvas"

        test_phrase = "CDP selector test: low-angle macro shot"
        await svc.focus_and_fill_prompt(page, test_phrase)
        typed_val = await svc._read_prompt_text(prompt_loc)
        assert "low-angle macro shot" in typed_val.lower(), f"Expected test phrase in prompt box, got {typed_val!r}"

        # Now clear it back to 100% empty so we leave the prompt box clean for Test 4
        await prompt_loc.click(timeout=3000)
        await page.keyboard.press("Control+A")
        await page.wait_for_timeout(80)
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(100)
        cleared_val = await svc._read_prompt_text(prompt_loc)
        assert cleared_val == "", f"Prompt box did not clear cleanly: {cleared_val!r}"
        print("[TEST 3] Verified prompt box clear, typing, and empty-reset guard.")

        # Test canvas card & inspector download button targeting
        focused = await svc._focus_newest_video_card(page, set(), prev_thumb_srcs=set())
        dl_btn = await svc._find_inspector_download_button(page)
        assert focused is True, "Failed to focus canvas video card"
        assert dl_btn is not None, "Failed to locate inspector download button (`⤓`) after focusing canvas card"
        await svc._close_inspector_panel(page)
        print("[TEST 3 PASSED] Verified canvas card focus, inspector download button selector, and inspector close!")
    finally:
        await svc.disconnect()


def run_test_4_full_end_to_end_render() -> Path:
    banner("TEST 4: FULL END-TO-END RENDER (LIVE FLOW + FFMPEG 9:16 COMPOSITE)")

    # Use active job 79014872-6828-4147-8434-98fc5d232aef (or create fresh if missing)
    job_id = "79014872-6828-4147-8434-98fc5d232aef"
    job = JobService.get_job(job_id)
    if not job:
        created = PipelineController.create_new_job(
            topic=TOPIC,
            title="The Angriest Pressure Cooker in Delhi",
            visual_style=VisualStyle.DEEP_DIVE_3D,
        )
        job_id = created["id"]

    # Reset job state cleanly so disk & manifest are 100% synchronized
    DownloadsWatcher.get_instance().sync_job_disk_state(job_id, reset_clips=True)

    print(f"[TEST 4] Running Stage 1 (Hindi Script) for job {job_id}...")
    script = PipelineController.execute_script_stage(job_id, force_regenerate=True)

    print(f"[TEST 4] Running Stage 2 (Humanized SSML Audio -6%) for job {job_id}...")
    audio_res = PipelineController.execute_audio_stage(job_id, force_regenerate=True)

    print(f"[TEST 4] Running Stage 3 (Yellow Karaoke ASS Subtitles) for job {job_id}...")
    sub_res = PipelineController.execute_subtitles_stage(job_id, force_regenerate=True)

    print(
        f"[TEST 4] Audio duration={audio_res['duration_seconds']:.2f}s | "
        f"Subtitles={Path(sub_res).name}. Starting Stage 3.5 (Live Google Flow 4-Scene Visuals)..."
    )

    generated_clips = PipelineController.execute_video_generation_stage(
        job_id,
        force_regenerate=False,
        provider_override="flow_browser",
    )
    print(f"[TEST 4] Generated/Verified {len(generated_clips)} scene clips:")
    for c in generated_clips:
        p = Path(c)
        print(f"  - {p.name}: {p.stat().st_size:,} bytes")

    assert len(generated_clips) == 4, f"Expected all 4 scenes generated, got {len(generated_clips)}"

    print(f"[TEST 4] Running Stage 4 (FFmpeg 9:16 Composite + EBU R128 + ASS Subtitle Burn)...")
    bgm_path = str(settings.DEFAULT_BGM_PATH) if settings.DEFAULT_BGM_PATH.exists() else None
    render_res = VideoRenderService.render_final_video(
        job_id=job_id,
        bgm_path=bgm_path,
        quality_mode=QualityMode.HIGH_QUALITY,
    )
    print(f"[TEST 4] Render Result: {render_res}")
    assert render_res.get("status") == "success", f"Render did not succeed: {render_res}"

    job_dir = JobService.get_job_directory(job_id)
    final_mp4_root = job_dir / "final_short.mp4"
    final_mp4_output = job_dir / "output" / "final_short.mp4"

    assert final_mp4_output.exists(), f"Missing {final_mp4_output}"
    assert final_mp4_root.exists(), f"Missing {final_mp4_root}"

    size_bytes = final_mp4_root.stat().st_size
    size_mb = size_bytes / (1024 * 1024)
    probe = SceneIntakeService.probe_video(final_mp4_root)

    print(
        f"[TEST 4] Final Video Probe: path={final_mp4_root}, "
        f"size={size_mb:.2f} MB ({size_bytes:,} bytes), "
        f"duration={probe.get('duration_seconds')}s, "
        f"resolution={probe.get('width')}x{probe.get('height')}"
    )
    assert size_bytes > 5 * 1024 * 1024, f"Expected final video size > 5 MB, got {size_mb:.2f} MB"
    assert probe.get("is_valid") and probe.get("duration_seconds", 0) > 15.0, (
        f"Invalid final video duration/stream: {probe}"
    )
    print(f"[TEST 4 PASSED] Full 4-scene Hindi Short rendered and verified at: {final_mp4_root}")
    return final_mp4_root


def main() -> None:
    t0 = time.time()
    run_test_1_script_and_ssml()
    run_test_2_watcher_deduplication()
    asyncio.run(run_test_3_flow_cdp_attachment())
    final_path = run_test_4_full_end_to_end_render()
    elapsed = time.time() - t0
    banner(f"ALL 4 AUTONOMOUS TESTS PASSED IN {elapsed:.1f}s -> {final_path}")


if __name__ == "__main__":
    main()
