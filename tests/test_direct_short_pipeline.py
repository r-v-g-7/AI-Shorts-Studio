import sys
import time
import json
import urllib.request
import urllib.error
from pathlib import Path

BASE_URL = "http://127.0.0.1:8000"


def main():
    print("=" * 80)
    print("  VERIFICATION RUN: /api/short/create_direct (Medical 60s English Short)")
    print("=" * 80)

    # 1. Health check
    req = urllib.request.Request(f"{BASE_URL}/api/health")
    with urllib.request.urlopen(req, timeout=5) as resp:
        health_data = json.loads(resp.read().decode())
        print(f"[1/4] Backend health verified: {health_data.get('status')}")

    # 2. Direct medical short request payload
    payload = {
        "title": "Medical Animation: Cigarette Smoke Inside Human Lungs",
        "language": "en-IN",
        "target_duration": 60.0,
        "visual_style": "inside_out",
        "quality_mode": "BALANCED",
        "creation_mode": "DIRECT",
        "provider_override": "mock",  # Deterministic 1080x1920 generator for automated test
        "scenes": [
            {
                "narration": "The moment a burning cigarette touches human lips, toxic gas and particulate matter at nine hundred degrees Celsius flood the oral cavity, searing delicate epithelial cells.",
                "visual_prompt": "Extreme macro 3D medical animation: lit cigarette tip touching human lips, glowing cherry embers, dense smoke swirling into mouth, volumetric lighting, 9:16 vertical, hyper-realistic 3D cinematic render"
            },
            {
                "narration": "Within ten seconds, carbon monoxide binds aggressively to hemoglobin, starving cardiac muscle of oxygen while heavy sticky tar settles along the bronchial airways.",
                "visual_prompt": "Hyper-detailed 3D anatomical cutaway of human throat and trachea: dark tar particles coating microscopic cilia, red blood cells binding to carbon monoxide molecules, 9:16 vertical, hyper-realistic 3D cinematic render"
            },
            {
                "narration": "Deep inside the alveoli, microscopic air sacs begin to rupture under oxidative stress, triggering chronic inflammation and paralyzing immune macrophages.",
                "visual_prompt": "Extreme close-up 3D cellular animation of alveoli air sacs: delicate membranes rupturing under toxic smoke exposure, macrophage immune cells overwhelmed, glowing cellular conduits, 9:16 vertical, hyper-realistic 3D cinematic render"
            },
            {
                "narration": "Simultaneously, coronary arteries constrict and arterial walls harden, forcing the human heart to pump frantically against soaring blood pressure.",
                "visual_prompt": "Cinematic 3D interior cross-section of coronary artery and beating human heart: arterial walls tightening, turbulent blood flow, high contrast dramatic studio lighting, 9:16 vertical, hyper-realistic 3D cinematic render"
            }
        ]
    }

    print("\n[2/4] Calling POST /api/short/create_direct...")
    req = urllib.request.Request(
        f"{BASE_URL}/api/short/create_direct",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        res_data = json.loads(resp.read().decode())

    job = res_data.get("job")
    job_id = job.get("id")
    print(f"  Created Job ID: {job_id}")
    print(f"  Initial Status: {job.get('status')}")

    # 3. Poll until job completes or fails
    print(f"\n[3/4] Polling queue execution for job {job_id}...")
    start_poll = time.time()
    last_stage = None
    final_job = None

    while time.time() - start_poll < 240:
        time.sleep(2)
        status_req = urllib.request.Request(f"{BASE_URL}/api/jobs/{job_id}")
        with urllib.request.urlopen(status_req, timeout=10) as s_resp:
            curr_job = json.loads(s_resp.read().decode())

        curr_status = curr_job.get("status")
        if curr_status != last_stage:
            print(f"  -> Stage Transition: {curr_status} ({time.time() - start_poll:.1f}s)")
            last_stage = curr_status

        if curr_status == "COMPLETED":
            final_job = curr_job
            break
        elif curr_status == "FAILED":
            print(f"ERROR: Job failed with error: {curr_job.get('error_message')}")
            # print recent logs
            logs_req = urllib.request.Request(f"{BASE_URL}/api/jobs/{job_id}/logs")
            with urllib.request.urlopen(logs_req, timeout=5) as l_resp:
                logs = json.loads(l_resp.read().decode())
                print("Recent logs:")
                for lg in logs[-10:]:
                    print(f"  [{lg.get('stage')}] {lg.get('message')}")
            sys.exit(1)

    if not final_job:
        print("ERROR: Timed out waiting for job to complete.")
        sys.exit(1)

    print(f"\n[4/4] Verifying rendered 60-second video and subtitle artifacts...")
    video_path_str = final_job.get("output_video_path")
    if not video_path_str:
        manifest_req = urllib.request.Request(f"{BASE_URL}/api/jobs/{job_id}/manifest")
        with urllib.request.urlopen(manifest_req, timeout=5) as m_resp:
            mf = json.loads(m_resp.read().decode())
            video_path_str = mf.get("final_video_file")

    video_path = Path(video_path_str)
    assert video_path.exists(), f"Video file not found at: {video_path}"
    file_size_mb = video_path.stat().st_size / (1024 * 1024)
    print(f"  Final Video Path: {video_path}")
    print(f"  File Size: {file_size_mb:.2f} MB")
    assert file_size_mb > 5.0, f"Expected video > 5 MB, got {file_size_mb:.2f} MB"

    # FFprobe validation
    import subprocess
    probe_cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=width,height,codec_name",
        "-of", "json", str(video_path)
    ]
    probe_res = subprocess.run(probe_cmd, capture_output=True, text=True)
    probe_info = json.loads(probe_res.stdout)
    dur = float(probe_info["format"]["duration"])
    streams = probe_info.get("streams", [])
    video_stream = next((s for s in streams if s.get("width")), None)

    print(f"  Video Duration: {dur:.2f} seconds")
    print(f"  Resolution: {video_stream.get('width')}x{video_stream.get('height')}")
    assert 58.0 <= dur <= 62.0, f"Expected ~60s duration, got {dur:.2f}s"
    assert video_stream.get("width") == 1080 and video_stream.get("height") == 1920, "Expected 1080x1920 resolution"

    # Verify subtitle burn
    job_dir = Path("storage/jobs") / job_id
    ass_path = job_dir / "subtitles" / "captions.ass"
    srt_path = job_dir / "subtitles" / "captions.srt"
    assert ass_path.exists(), f"ASS subtitle file missing at {ass_path}"
    assert srt_path.exists(), f"SRT subtitle file missing at {srt_path}"
    print(f"  Subtitles verified: {ass_path.name} ({ass_path.stat().st_size} bytes) & {srt_path.name} ({srt_path.stat().st_size} bytes)")

    # Verify audio narration file
    audio_path = job_dir / "audio" / "narration.wav"
    words_path = job_dir / "audio" / "words.json"
    assert audio_path.exists(), "narration.wav missing"
    assert words_path.exists(), "words.json missing"
    with open(words_path, "r", encoding="utf-8") as f:
        w_data = json.load(f)
    print(f"  Audio verified: {audio_path.name} with {len(w_data.get('timestamps', []))} word timestamps")

    # Sample a frame to verify subtitle rendering visually
    test_frame = Path("storage/temp/medical_sub_frame.png")
    test_frame.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-ss", "00:00:03", "-i", str(video_path),
        "-vframes", "1", str(test_frame)
    ], capture_output=True)
    assert test_frame.exists() and test_frame.stat().st_size > 1000, "Failed to extract test frame"
    print(f"  Visual Subtitle Frame extracted: {test_frame} ({test_frame.stat().st_size} bytes)")

    print("\n" + "=" * 80)
    print("  SUCCESS: 60-Second Direct Medical Short Fully Rendered & Verified!")
    print(f"  Output Video: {video_path}")
    print("=" * 80)


if __name__ == "__main__":
    main()
