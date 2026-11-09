# AI Faceless Shorts Studio 🎬 — Fully Automated 1-Click Production

An end-to-end automated production studio for generating high-retention, vertical 9:16 YouTube Shorts engineered specifically for an **HP Victus laptop** (**RTX 3050 4GB VRAM** and **16GB RAM**).

---

## 🚀 The 1-Click Experience

```
[ENTER STORY] ➔ [CLICK "GENERATE FULL VIDEO"] ➔ [RECEIVE DOWNLOADABLE 1080x1920 MP4]
```
Zero manual browser visits. Zero manual downloads. Zero file moving.

---

## 🏛️ System Architecture

```mermaid
flowchart TD
    A[React Studio Dashboard] -->|1. Enter Story & Click 'Generate Full Video'| B[FastAPI Controller & Sequential Queue]
    B -->|2. Generate Script & Prompts| C[Gemini LLM / Script Service]
    B -->|3. Synthesize Voice Audio| D[Kokoro-82M ONNX - CPU 0MB VRAM]
    B -->|4. Generate Karaoke Subtitles| E[Word-Level Timestamps -> ASS Subtitle Engine]
    B -->|5. Auto-Generate AI Video Clips| F[Google GenAI Video API / Veo]
    F -->|6. Save Clamped 9:16 Clips| G[storage/jobs/{id}/scenes/]
    G --> H[FFmpeg 9:16 Compositor]
    D --> H
    E --> H
    I[Ambient BGM with Voice Ducking] --> H
    H -->|7. Loudnorm -14 LUFS & Hardware Encode| J[final_short.mp4]
    J --> K[Automated 8-Factor QC Gate]
    K -->|8. Mark Completed| A
```

---

## ⚡ Video Generation Integration (Google GenAI / Veo API)

* **Official SDK:** Uses Google's official `google-genai` Python library (`from google import genai; from google.genai import types`).
* **Model:** Default configured to Google's recommended video generation model (`gemini-omni-1.1-flash` / `veo-2.0-generate-001`).
* **Asynchronous Polling:** Submits long-running video generation operations (`client.models.generate_videos`), polls `client.operations.get(operation)` until complete, and downloads resulting MP4 bytes directly to the job's `scenes/` directory.
* **Aspect Ratio:** Generates native 9:16 vertical video at requested scene durations.
* **Safety & Transparency:** Never replaces failed API calls with silent placeholders. Exact API errors or quota messages are returned directly to the UI.
* **Deterministic Offline Testing:** Includes a built-in `MockVideoGenerator` for running full end-to-end local test suites without consuming external API credits.

---

## 🔑 Configuration & API Keys

To enable live Google AI video generation:

1. Obtain an API key from **Google AI Studio**: [https://aistudio.google.com/](https://aistudio.google.com/)
2. Open your `.env` file in the project root:
   ```ini
   # Add your Google Gemini API key:
   GEMINI_API_KEY=AIzaSy...your_actual_key_here

   # Video Model (default: gemini-omni-1.1-flash or veo-2.0-generate-001)
   GEMINI_VIDEO_MODEL=gemini-omni-1.1-flash

   # Video Generation Provider: "gemini" for live API, or "mock" for offline testing
   VIDEO_GENERATION_PROVIDER=gemini
   ```
> **Note on Google AI Pro & Video API Billing:** While Google AI Pro grants browser access to Google Flow, direct programmatic video generation through the Gemini API requires an active Google AI Studio project with billing enabled for pay-per-use video quotas. If the key is not set, the app clearly alerts you on the dashboard.

---

## 🛠️ Quick Steps to Run the Studio

### Step 1: Open PowerShell in Project Directory
```powershell
cd C:\Users\Rishab\Desktop\AI-Shorts-Studio
```

### Step 2: Start the Studio Server
```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

### Step 3: Open in Browser
Visit: **[http://127.0.0.1:8000](http://127.0.0.1:8000)**

1. Paste your story or narrative hook into the left form.
2. Click **⚡ Generate Full Video (1-Click)**.
3. The studio automatically produces your script, voiceover, subtitles, video footage, ambient audio ducking, and renders the final 1080x1920 MP4.
4. Watch the completed short in the embedded player and click **⬇️ Download Finished MP4**!

---

## 🧪 Testing

Run the complete automated end-to-end verification test suite:
```powershell
.\.venv\Scripts\python.exe test_full_automation.py
```
Verifies project creation, video provider contract & credential safety, 1-click full pipeline execution, all intermediate disk assets, video streaming, and the download attachment endpoint.
