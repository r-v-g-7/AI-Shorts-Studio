"""
Phase 6 verification: Roman-Hinglish script -> edge-tts (hi-IN-MadhurNeural) -> word timestamps
-> ASS subtitles -> Downloads watcher -> automatic render.

Requires internet access (edge-tts calls Microsoft's online neural voice service) and ffmpeg.
"""
import os
import re
import sys
import json
import time
import shutil
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

from backend.config import settings
from backend.database.connection import init_db
from backend.schemas.job_manifest import JobStatus, VisualStyle
from backend.schemas.script_schema import ScriptGenerationRequest
from backend.services.audio_service import AudioService
from backend.services.downloads_watcher import DownloadsWatcher
from backend.services.job_service import JobService
from backend.services.pipeline_controller import PipelineController
from backend.services.scene_intake_service import SceneIntakeService
from backend.services.script_service import ScriptService
from backend.services.video_generator import MockVideoGenerator

VOICE = "hi-IN-MadhurNeural"
DEVANAGARI = re.compile(r"[\u0900-\u097F]")
HINDI_FUNCTION_WORDS = {
    "ek", "ki", "ke", "ka", "ne", "aur", "par", "tha", "thi", "tabhi", "wo", "usne", "apni", "apne", "mein",
    "se", "bola", "boli", "lekin", "phir", "nahi", "ko", "hai", "aaj", "raat", "kal", "bhi", "jo", "diya",
    "di", "ya", "toh", "beta", "laoge", "laaunga", "aaya", "laute", "chadh", "gaya", "gaye", "lagi", "kar",
}

STORY = """A lonely little girl sits on a rooftop every night, talking to the moon because she has nobody else to talk to. One evening, Sheru, a ridiculously rich and overconfident golden lion in a white three-piece suit, discovers her crying because she believes the moon has disappeared behind the clouds forever.
Sheru dramatically promises to bring the moon back. He jumps into his golden supercar, launches a ridiculous high-speed chase through a futuristic city, accidentally destroys his own luxury fountain, gets chased by police drones, and eventually climbs a gigantic radio tower during a thunderstorm.
In an absolutely ridiculous climax, he uses a giant construction crane to pull the clouds apart. The moon reappears, illuminating the entire city.
But here's the twist: the little girl wasn't actually waiting for the moon. She was waiting for her father, who works night shifts and has finally returned home.
Sheru watches them hug. He quietly removes his expensive gold wristwatch and gives it to the girl, saying in deep, emotional Hindi: 'Ghadi toh waqt batati hai, beta... par apne log waqt nikaalte hain.'
The girl laughs and asks Sheru whether he can also bring her the stars tomorrow. Sheru looks at the camera, exhausted, and says: 'Ek chaand ke liye jaan nikal gayi... ab taare bhi chahiye!'"""

QUOTE_1 = "Ghadi toh waqt batati hai, beta... par apne log waqt nikaalte hain."
QUOTE_2 = "Ek chaand ke liye jaan nikal gayi... ab taare bhi chahiye!"


def wait_for(predicate, timeout: float, interval: float = 0.4, what: str = "condition"):
    t0 = time.time()
    while time.time() - t0 < timeout:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"Timed out after {timeout:.0f}s waiting for {what}")


def run():
    print("=" * 72)
    print("PHASE 6 - NATIVE HINDI/HINGLISH NARRATION + DOWNLOADS INTAKE VERIFICATION")
    print("=" * 72)
    init_db()

    # ------------------------------------------------------------------ 1. Script
    print("\n--- 1. Script generation in Roman Hinglish ---")
    req = ScriptGenerationRequest(
        topic=STORY, language="hinglish", visual_style=VisualStyle.DEEP_DIVE_3D, target_duration=30.0
    )
    script = ScriptService.generate_script(req)
    narration = script.complete_narration
    print(f"Title: {script.title}\nNarration:\n{narration}\n")

    assert not DEVANAGARI.search(narration), "Narration must be Roman script, not Devanagari"
    assert QUOTE_1 in narration and QUOTE_2 in narration, "User dialogue must be preserved verbatim"
    for name in ("Sheru", "bacchi", "chaand"):
        assert name in narration, f"'{name}' missing from Hinglish narration"
    words = re.findall(r"[A-Za-z']+", narration.lower())
    hindi_share = sum(1 for w in words if w in HINDI_FUNCTION_WORDS) / len(words)
    print(f"[INFO] Hindi function-word share: {hindi_share:.0%} of {len(words)} words")
    assert hindi_share >= 0.20, "Narration reads as English, not conversational Hinglish"
    assert len(narration.split()) <= 110, f"Script is {len(narration.split())} words: too long for a Short"
    english_openers = ("did you know", "science struggled", "in a ridiculous climax", "on a lonely rooftop")
    assert not any(p in narration.lower() for p in english_openers), "English narration leaked into Hinglish script"
    for s in script.scenes:  # video prompts must stay English
        assert not DEVANAGARI.search(s.visual_description)
    print("[PASS] Script is Roman Hinglish, dialogue verbatim, visual prompts still English")

    # ------------------------------------------------------------------ 2. Audio
    print("\n--- 2. Audio via edge-tts (hi-IN-MadhurNeural) ---")
    job = PipelineController.create_new_job(topic=STORY, title="Hindi Pipeline Test", visual_style=VisualStyle.DEEP_DIVE_3D)
    job_id = job["id"]
    PipelineController.execute_script_stage(job_id, generation_request=req)
    audio = PipelineController.execute_audio_stage(job_id, voice=VOICE, speed=1.0)

    assert audio["engine"] == "edge-tts", f"Wrong engine: {audio['engine']}"
    assert audio["voice"] == VOICE
    wav = Path(audio["audio_path"])
    info = sf.info(str(wav))
    print(f"[INFO] WAV: {info.samplerate} Hz, {info.channels} ch, {info.duration:.2f}s, {wav.stat().st_size / 1024:.0f} KB")
    assert info.samplerate == 24000 and info.channels == 1 and info.subtype == "PCM_16"
    assert abs(info.duration - audio["duration_seconds"]) < 0.01
    assert 15 < info.duration < 50, f"Unexpected narration length {info.duration:.1f}s (script has {len(narration.split())} words)"
    print(f"[PASS] hi-IN-MadhurNeural narration rendered ({audio['duration_seconds']:.2f}s, {audio['inference_time_seconds']:.1f}s to synthesize)")

    # ------------------------------------------------------------------ 3. Timestamps
    print("\n--- 3. Word timestamps from edge-tts boundaries ---")
    ts = audio["timestamps"]
    assert audio["timing_source"].startswith("edge-tts-WordBoundary"), f"Timing is not from boundaries: {audio['timing_source']}"
    script_tokens = " ".join(s.narration.strip() for s in JobService.load_manifest(job_id).scenes).split()
    print(f"[INFO] {len(ts)} timed words for {len(script_tokens)} script tokens (source: {audio['timing_source']})")
    assert abs(len(ts) - len(script_tokens)) <= 3
    prev_end = 0.0
    for w in ts:
        assert w.end_time > w.start_time >= prev_end - 1e-6, f"Non-monotonic timing at '{w.word}'"
        assert w.end_time <= info.duration + 0.01
        prev_end = w.end_time
    assert info.duration - ts[-1].end_time < 1.5, "Last word ends far before the audio does: timing drifted"
    durations = [w.end_time - w.start_time for w in ts]
    assert max(durations) - min(durations) > 0.1, "Word durations are uniform: looks estimated, not measured"

    data, sr = sf.read(str(wav), dtype="float32")
    voiced = 0
    for w in ts:
        seg = data[int(w.start_time * sr): max(int(w.end_time * sr), int(w.start_time * sr) + 1)]
        rms_db = 20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-9)
        voiced += rms_db > -45
    voiced_ratio = voiced / len(ts)
    print(f"[INFO] {voiced_ratio:.0%} of word intervals contain speech energy (> -45 dBFS)")
    assert voiced_ratio >= 0.90, "Timestamps do not line up with audible speech"
    print("[PASS] Timestamps are monotonic, bounded by the audio, and land on audible speech")

    # ------------------------------------------------------------------ 4. Subtitles
    print("\n--- 4. Roman Hinglish ASS subtitles ---")
    ass_path = Path(PipelineController.execute_subtitles_stage(job_id, force_regenerate=True))
    ass = ass_path.read_text(encoding="utf-8")
    style_line = next(l for l in ass.splitlines() if l.startswith("Style: Default"))
    f = style_line.split(",")
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert f[3] == "&H00FFFFFF&" and f[4] == "&H0000FFFF&" and f[5] == "&H00000000&", f"Colours wrong: {f[3:6]}"
    assert f[7] == "-1", "Subtitles must be bold"
    assert f[15] == "1" and f[16] == "6", f"Expected BorderStyle 1 / 6px outline, got {f[15]}/{f[16]}"
    assert f[21] == "650", f"Eyeline MarginV expected 650, got {f[21]}"
    print(f"[INFO] Style: {settings.SUBTITLE_FONT_NAME} {f[2]}pt bold, white {f[3]}, yellow {f[4]}, outline {f[16]}px black, MarginV {f[21]}")

    active_re = re.compile(r"\{\\c&H0000FFFF&\}(.+?)\{\\c&H00FFFFFF&\}")
    actives = []
    for line in ass.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        h, m, s = parts[1].split(":")
        start = int(h) * 3600 + int(m) * 60 + float(s)
        mt = active_re.search(parts[9])
        assert mt, f"Dialogue line without a yellow active word: {line}"
        actives.append((start, mt.group(1)))
    assert len(actives) == len(ts), f"{len(actives)} highlight frames for {len(ts)} words"
    for (a_start, a_word), w in zip(actives, ts):
        assert abs(a_start - w.start_time) <= 0.011, f"'{w.word}' highlights at {a_start:.2f}s, spoken at {w.start_time:.2f}s"
        assert a_word == re.sub(r"[\\{}]", "", w.word).upper()
    assert not DEVANAGARI.search(ass)
    print(f"[PASS] {len(actives)} word-by-word highlight frames align with edge-tts timestamps (<= 10 ms)")
    print("       Sample:", " ".join(w for _, w in actives[:8]), "...")

    # ------------------------------------------------------------------ 5/6. Watcher + auto render
    print("\n--- 5. Downloads watcher: detect, move, intake ---")
    manifest = JobService.load_manifest(job_id)
    assert manifest.status == JobStatus.VISUALS_PENDING
    scene_count = len(manifest.scenes)
    assert scene_count == 4

    downloads = Path(tempfile.mkdtemp(prefix="fake_downloads_"))
    staging = Path(tempfile.mkdtemp(prefix="fake_clips_"))
    try:
        # A stale download from before the watcher existed must never be touched.
        old_file = downloads / "scene_04_OLD.mp4"
        old_file.write_bytes(b"old download")
        os.utime(old_file, (time.time() - 3600, time.time() - 3600))

        gen = MockVideoGenerator()
        for s in manifest.scenes:
            gen.generate_scene_clip("test", max(2.0, s.duration_seconds), staging / f"scene_{s.index + 1:02d}.mp4", scene_index=s.index)

        watcher = DownloadsWatcher(downloads_dir=downloads, poll_seconds=0.3, stable_seconds=0.8, pinned_job_id=job_id)
        watcher.start()
        time.sleep(0.5)
        incoming = SceneIntakeService.get_incoming_directory(job_id)
        try:
            # (a) in-progress Chrome download + unrelated file must be ignored
            shutil.copy2(staging / "scene_01.mp4", downloads / "scene_01.mp4.crdownload")
            shutil.copy2(staging / "scene_02.mp4", downloads / "holiday_video.mp4")
            time.sleep(2.5)
            assert not list(incoming.glob("*.mp4")), "Partial/unrelated downloads were wrongly ingested"
            assert (downloads / "scene_01.mp4.crdownload").exists() and (downloads / "holiday_video.mp4").exists()
            print("[PASS] Ignored scene_01.mp4.crdownload (in progress), holiday_video.mp4 (no scene number), and stale pre-start download")

            # (b) the download finishes (browser renames .crdownload -> .mp4), scenes 1-3 arrive
            os.replace(downloads / "scene_01.mp4.crdownload", downloads / "scene_01.mp4")
            for i in (2, 3):
                shutil.copy2(staging / f"scene_{i:02d}.mp4", downloads / f"scene_{i:02d}.mp4")
            wait_for(lambda: len(list(incoming.glob("scene_0*.mp4"))) == 3, 15, what="scenes 1-3 to be moved into incoming_clips")
            time.sleep(1.5)
            assert not any((downloads / f"scene_0{i}.mp4").exists() for i in (1, 2, 3)), "Clips should be MOVED out of Downloads"
            status = watcher.status()
            assert status["scenes_ready"] == 3 and status["scenes_total"] == 4, status
            assert JobService.get_job(job_id)["status"] == JobStatus.VISUALS_PENDING.value
            print("[PASS] Scenes 1-3 moved to incoming_clips/ and validated; job still waiting (3/4), no premature render")

            # (c) last clip arrives -> automatic render
            print("\n--- 6. Auto-render once all 4 clips have arrived ---")
            shutil.copy2(staging / "scene_04.mp4", downloads / "scene_04.mp4")
            wait_for(lambda: JobService.get_job(job_id)["status"] == JobStatus.COMPLETED.value, 180, interval=1.0,
                     what="automatic render to finish")
        finally:
            watcher.stop()

        assert old_file.exists() and old_file.read_bytes() == b"old download", "Stale download was modified"
        assert watcher.status()["scenes_ready"] == 4
        out = JobService.get_job_directory(job_id) / "output" / "final_short.mp4"
        probe = SceneIntakeService.probe_video(out)
        assert probe["is_valid"] and (probe["width"], probe["height"]) == (1080, 1920), probe
        qc = json.loads((JobService.get_job_directory(job_id) / "output" / "quality_report.json").read_text(encoding="utf-8"))
        print(f"[PASS] Auto-render complete: {probe['width']}x{probe['height']}, {probe['duration_seconds']:.1f}s, QC={qc['overall_status']}, production_ready={qc['is_production_ready']}")
        assert qc["is_production_ready"], f"QC flagged the render: {qc['overall_status']}"
        logs = [l["message"] for l in JobService.get_logs(job_id) if l["stage"] == "DOWNLOADS_WATCHER"]
        assert any("queued FFmpeg render" in m for m in logs)
        print(f"[INFO] Job log entries from watcher: {len(logs)}")
    finally:
        shutil.rmtree(downloads, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)

    print("\n" + "=" * 72)
    print("ALL HINDI / HINGLISH PIPELINE + DOWNLOADS INTAKE TESTS PASSED")
    print("=" * 72)


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        print(f"\n[FAIL] {e}")
        sys.exit(1)
