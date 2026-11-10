import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # App Settings
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    DEBUG: bool = True

    # Server Settings
    BACKEND_HOST: str = "127.0.0.1"
    BACKEND_PORT: int = 8000

    # Paths
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    STORAGE_DIR: Path = BASE_DIR / "storage"
    JOBS_STORAGE_DIR: Path = STORAGE_DIR / "jobs"
    DATABASE_URL: str = f"sqlite:///{STORAGE_DIR / 'studio.sqlite3'}"
    MODELS_DIR: Path = BASE_DIR / "models" / "kokoro"
    KOKORO_MODEL_PATH: Path = MODELS_DIR / "kokoro-v1.0.onnx"
    KOKORO_VOICES_PATH: Path = MODELS_DIR / "voices-v1.0.bin"

    # LLM & Video Generation Runtime (Google Flow CDP / Gemini / Veo)
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"
    GEMINI_VIDEO_MODEL: str = "gemini-omni-1.1-flash"  # Official Gemini / Veo video generation model
    VIDEO_GENERATION_PROVIDER: str = "flow_browser"    # "flow_browser" (CDP), "gemini" (API), or "mock" (offline)
    CDP_DEBUGGING_URL: str = "http://127.0.0.1:9222"   # Chrome DevTools Protocol endpoint (--remote-debugging-port=9222)
    FLOW_URL: str = "https://labs.google/fx/tools/flow"
    FLOW_SCENE_TIMEOUT_SECONDS: int = 180
    VIDEO_POLL_INTERVAL_SECONDS: int = 10
    VIDEO_MAX_POLL_TIMEOUT_SECONDS: int = 360
    VIDEO_ASPECT_RATIO: str = "9:16"
    ASSETS_DIR: Path = BASE_DIR / "assets"
    BGM_DIR: Path = BASE_DIR / "assets" / "bgm"
    DEFAULT_BGM_PATH: Path = BASE_DIR / "assets" / "bgm" / "ambient_cinematic.wav"

    # Narration language for script generation: "hinglish" (Roman-script Hindi) or "en-us"
    NARRATION_LANGUAGE: str = "hinglish"

    # TTS voice selection. Neural voices such as "hi-IN-MadhurNeural" (male) or
    # "hi-IN-SwaraNeural" (female) are routed to edge-tts; names like "af_bella" go to local Kokoro.
    DEFAULT_VOICE: str = "hi-IN-MadhurNeural"
    TTS_SPEED: float = 1.0
    # If edge-tts is unreachable, fail loudly instead of silently reading Hindi with English phonemes.
    TTS_ALLOW_KOKORO_FALLBACK: bool = False

    # Local Kokoro TTS (English voices)
    KOKORO_VOICE: str = "am_adam"
    KOKORO_LANG: str = "en-us"
    KOKORO_SPEED: float = 1.0

    # Downloads folder watcher (Google Flow clips -> active job incoming_clips/)
    DOWNLOADS_WATCH_ENABLED: bool = True
    DOWNLOADS_DIR: Path = Path.home() / "Downloads"
    DOWNLOADS_POLL_SECONDS: float = 2.0
    DOWNLOADS_STABLE_SECONDS: float = 2.0

    # Subtitle Styling Defaults (9:16 vertical video layout)
    SUBTITLE_FONT_NAME: str = "Arial Black"
    SUBTITLE_FONT_SIZE: int = 72
    SUBTITLE_ACTIVE_COLOR: str = "&H0000FFFF&"      # Bright Yellow (ASS BGR)
    SUBTITLE_INACTIVE_COLOR: str = "&H00FFFFFF&"    # Pure White
    SUBTITLE_OUTLINE_COLOR: str = "&H00000000&"     # Deep Black
    SUBTITLE_OUTLINE_WIDTH: int = 6
    SUBTITLE_MARGIN_V: int = 650                    # Eye-level vertical margin from bottom
    SUBTITLE_WORDS_PER_CHUNK: int = 3               # Mobile retention chunk size

    # Hardware Limits
    HARDWARE_ACCELERATION: str = "nvenc"
    MAX_VRAM_GB: int = 4
    MAX_RAM_GB: int = 16

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()

# Ensure required directories exist
settings.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
settings.JOBS_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
