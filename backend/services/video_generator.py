import os
import time
import shutil
import logging
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Dict, Any, List

from backend.config import settings

logger = logging.getLogger(__name__)


class BaseVideoGenerator(ABC):
    """Abstract Base Class for Video Generation Providers."""

    @abstractmethod
    def generate_scene_clip(
        self,
        scene_prompt: str,
        duration_seconds: float,
        output_path: Path,
        negative_prompt: Optional[str] = None,
        aspect_ratio: str = "9:16",
        job_id: Optional[str] = None,
        scene_index: Optional[int] = None
    ) -> Path:
        """Generates a video clip from a prompt and writes it to output_path."""
        pass


class GeminiVideoGenerator(BaseVideoGenerator):
    """
    Official Google Gemini / Veo Video Generation Provider using google-genai SDK.
    Supports asynchronous generation, LRO polling, and direct MP4 asset downloads.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
        poll_interval: Optional[int] = None,
        max_timeout: Optional[int] = None
    ):
        self.api_key = (api_key or settings.GEMINI_API_KEY or "").strip()
        self.model_name = (model_name or settings.GEMINI_VIDEO_MODEL or "gemini-omni-1.1-flash").strip()
        self.poll_interval = poll_interval or settings.VIDEO_POLL_INTERVAL_SECONDS
        self.max_timeout = max_timeout or settings.VIDEO_MAX_POLL_TIMEOUT_SECONDS

    def generate_scene_clip(
        self,
        scene_prompt: str,
        duration_seconds: float,
        output_path: Path,
        negative_prompt: Optional[str] = None,
        aspect_ratio: str = "9:16",
        job_id: Optional[str] = None,
        scene_index: Optional[int] = None
    ) -> Path:
        if not self.api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not configured in .env. "
                "Please configure your Google Gemini API key from Google AI Studio (https://aistudio.google.com/) "
                "to enable automated video generation."
            )

        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise RuntimeError(
                "The 'google-genai' package is required for video generation. "
                "Run: pip install google-genai"
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(
            f"Initiating Gemini video generation for Job '{job_id}' Scene {scene_index} "
            f"using model '{self.model_name}' (aspect_ratio={aspect_ratio}, duration={duration_seconds:.1f}s)..."
        )

        # Initialize official GenAI client
        client = genai.Client(api_key=self.api_key)

        # Configure video parameters (clamp duration to integer seconds supported by API, typically 4-8s)
        clamped_duration = int(max(3, min(8, round(duration_seconds))))
        config_kwargs: Dict[str, Any] = {
            "aspect_ratio": aspect_ratio,
            "duration_seconds": clamped_duration,
        }
        if negative_prompt and negative_prompt.strip():
            config_kwargs["negative_prompt"] = negative_prompt.strip()

        video_config = types.GenerateVideosConfig(**config_kwargs)

        # Submit asynchronous video generation operation with retry for transient errors
        operation = None
        max_retries = 3
        for attempt in range(max_retries):
            try:
                operation = client.models.generate_videos(
                    model=self.model_name,
                    prompt=scene_prompt,
                    config=video_config
                )
                break
            except Exception as e:
                err_str = str(e)
                if ("429" in err_str or "quota" in err_str.lower() or "503" in err_str) and attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 10
                    logger.warning(f"Transient error on video request: {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Failed to submit video generation request to Gemini: {e}")
                    raise RuntimeError(f"Google Gemini video generation API request failed: {e}")

        if not operation:
            raise RuntimeError("Failed to obtain video generation operation from Gemini API.")

        logger.info(f"Operation submitted (name: {getattr(operation, 'name', 'unnamed')}). Polling for completion...")

        # Poll operation until complete or timeout
        start_time = time.time()
        while not operation.done:
            elapsed = time.time() - start_time
            if elapsed > self.max_timeout:
                raise TimeoutError(
                    f"Gemini video generation timed out after {int(elapsed)} seconds. "
                    f"Operation: {getattr(operation, 'name', 'unnamed')}"
                )

            logger.info(f"Waiting for video generation... ({int(elapsed)}s elapsed)")
            time.sleep(self.poll_interval)

            try:
                operation = client.operations.get(operation)
            except Exception as poll_err:
                logger.warning(f"Error checking operation status: {poll_err}. Will retry next cycle.")

        # Check for operation errors
        if getattr(operation, "error", None):
            err_msg = getattr(operation.error, "message", str(operation.error))
            raise RuntimeError(f"Gemini video generation operation reported error: {err_msg}")

        # Retrieve video response
        response = getattr(operation, "response", None)
        if not response or not getattr(response, "generated_videos", None):
            raise RuntimeError(
                "Gemini completed video generation operation but returned no generated video assets. "
                "Check safety filters and prompt constraints."
            )

        generated_video = response.generated_videos[0]
        video_obj = generated_video.video

        # Save downloaded video asset to destination
        logger.info(f"Downloading completed video asset to {output_path}...")
        try:
            if hasattr(video_obj, "save"):
                video_obj.save(str(output_path))
            elif hasattr(video_obj, "video_bytes") and video_obj.video_bytes:
                with open(output_path, "wb") as f:
                    f.write(video_obj.video_bytes)
            else:
                client.files.download(file=video_obj)
                if hasattr(video_obj, "save"):
                    video_obj.save(str(output_path))
                else:
                    raise RuntimeError("Unable to extract video bytes from Gemini response object.")
        except Exception as save_err:
            raise RuntimeError(f"Failed to save Gemini video asset to {output_path}: {save_err}")

        if not output_path.exists() or output_path.stat().st_size < 1000:
            raise RuntimeError(f"Saved video file is empty or missing: {output_path}")

        logger.info(f"Successfully saved generated scene video: {output_path} ({output_path.stat().st_size / 1024:.1f} KB)")
        return output_path


class MockVideoGenerator(BaseVideoGenerator):
    """
    Deterministic Video Generator for testing and offline development.
    Uses FFmpeg to generate an authentic 1080x1920 9:16 playable vertical video
    matching exact duration, style, and label requirements with zero external API calls.
    """

    COLOR_PALETTES = [
        "#1a365d", "#0d5c75", "#144552", "#2b4162",
        "#0f2027", "#203a43", "#2c5364", "#0077b6"
    ]

    def generate_scene_clip(
        self,
        scene_prompt: str,
        duration_seconds: float,
        output_path: Path,
        negative_prompt: Optional[str] = None,
        aspect_ratio: str = "9:16",
        job_id: Optional[str] = None,
        scene_index: Optional[int] = None
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        idx = scene_index if scene_index is not None else 0
        color = self.COLOR_PALETTES[idx % len(self.COLOR_PALETTES)]

        logger.info(f"[MockVideoGenerator] Generating synthetic test clip for Scene {idx + 1} ({duration_seconds:.2f}s, testsrc pattern)...")

        # Generate genuine 1080x1920 30fps vertical MP4 (>1.5MB) using FFmpeg
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "testsrc=size=1080x1920:rate=30",
            "-t", f"{duration_seconds:.2f}",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-b:v", "2.5M",
            "-minrate", "2M",
            "-maxrate", "4M",
            "-bufsize", "5M",
            "-pix_fmt", "yuv420p",
            str(output_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Mock video generation failed: {res.stderr.strip()}")

        # Ensure clip is also copied to incoming_clips if job_id is provided
        if job_id:
            try:
                from backend.services.scene_intake_service import SceneIntakeService
                inc_dir = SceneIntakeService.get_incoming_directory(job_id)
                inc_clip = inc_dir / output_path.name
                shutil.copy2(str(output_path), str(inc_clip))
            except Exception as e:
                logger.warning(f"Could not copy mock clip to incoming_clips: {e}")

        return output_path


class FlowBrowserVideoGenerator(BaseVideoGenerator):
    """
    Google Flow Browser Automation Provider using Playwright over Chrome DevTools Protocol (CDP).
    Connects to http://127.0.0.1:9222, enters each scene prompt in Google Flow, waits for
    rendering, and downloads the clip into the Downloads folder and job scenes directory.
    """

    def __init__(self, cdp_url: Optional[str] = None):
        self.cdp_url = cdp_url or settings.CDP_DEBUGGING_URL

    def generate_scene_clip(
        self,
        scene_prompt: str,
        duration_seconds: float,
        output_path: Path,
        negative_prompt: Optional[str] = None,
        aspect_ratio: str = "9:16",
        job_id: Optional[str] = None,
        scene_index: Optional[int] = None
    ) -> Path:
        from backend.services.flow_automation_service import FlowAutomationService
        service = FlowAutomationService(cdp_url=self.cdp_url)
        idx = scene_index if scene_index is not None else 0
        return service.run_scene_sync(
            prompt=scene_prompt,
            scene_index=idx,
            output_path=output_path,
            job_id=job_id
        )


def get_video_generator(provider_override: Optional[str] = None) -> BaseVideoGenerator:
    """Factory function returning the configured video generator."""
    provider = (provider_override or settings.VIDEO_GENERATION_PROVIDER or "flow_browser").lower().strip()
    if provider == "mock":
        return MockVideoGenerator()
    if provider == "flow_browser":
        return FlowBrowserVideoGenerator()
    key = (settings.GEMINI_API_KEY or "").strip()
    if not key or key.startswith("your_"):
        logger.warning("GEMINI_API_KEY is not configured; using MockVideoGenerator to synthesize 1080x1920 footage.")
        return MockVideoGenerator()
    return GeminiVideoGenerator()
