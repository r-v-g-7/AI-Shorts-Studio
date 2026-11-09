from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
import datetime


class JobStatus(str, Enum):
    DRAFT = "DRAFT"
    SCRIPT_READY = "SCRIPT_READY"
    AUDIO_PROCESSING = "AUDIO_PROCESSING"
    AUDIO_READY = "AUDIO_READY"
    VISUALS_PENDING = "VISUALS_PENDING"
    RENDERING = "RENDERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    # Backward compatibility aliases
    SCRIPT_GENERATING = "SCRIPT_GENERATING"
    AUDIO_SYNTHESIZING = "AUDIO_SYNTHESIZING"
    TIMESTAMPS_EXTRACTING = "TIMESTAMPS_EXTRACTING"
    TIMESTAMPS_READY = "TIMESTAMPS_READY"
    AWAITING_SCENES = "AWAITING_SCENES"
    SCENES_VALIDATED = "SCENES_VALIDATED"


class VisualStyle(str, Enum):
    TALL_TALE_3D = "tall_tale_3d"
    DEEP_DIVE_3D = "deep_dive_3d"
    INSIDE_OUT = "inside_out"
    STICK_STORY = "stick_story"
    NIGHT_FILE_3D = "night_file_3d"
    PAPER_ATLAS = "paper_atlas"

    # Backward compatibility aliases
    CYBER_PUNK_3D = "cyber_punk_3d"
    DOCUMENTARY_REALISM = "documentary_realism"
    CUSTOM = "custom"


class WordTimestamp(BaseModel):
    word: str
    start_time: float
    end_time: float


class SceneSpec(BaseModel):
    index: int = Field(..., ge=0, description="0-indexed scene sequence order")
    narration: str = Field(..., min_length=1, description="Exact spoken narration text for this scene")
    visual_prompt: str = Field(..., min_length=1, description="Google Flow / Veo optimized visual prompt")
    duration_seconds: float = Field(default=5.0, gt=0, description="Estimated/actual duration of this scene in seconds")
    camera_movement: Optional[str] = Field(default=None, description="Camera direction tokens")
    sound_effects: List[str] = Field(default_factory=list, description="Sound effect cues")
    clip_file: Optional[str] = Field(default=None, description="Relative or absolute path to the downloaded video clip")
    is_validated: bool = Field(default=False, description="Whether the clip file exists, is valid 9:16 and readable")


class JobManifest(BaseModel):
    """The central state manifest stored as manifest.json inside each job directory."""
    job_id: str
    title: str
    topic: str
    visual_style: VisualStyle = VisualStyle.TALL_TALE_3D
    status: JobStatus = JobStatus.DRAFT
    error: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.datetime.utcnow().isoformat())

    # Content & Assets
    script_data: Optional[Dict[str, Any]] = None
    scenes: List[SceneSpec] = Field(default_factory=list)
    audio_file: Optional[str] = None
    subtitle_file: Optional[str] = None
    word_timestamps: List[WordTimestamp] = Field(default_factory=list)
    final_video_file: Optional[str] = None

    # Render settings & metrics
    render_metadata: Dict[str, Any] = Field(default_factory=lambda: {
        "aspect_ratio": "9:16",
        "target_resolution": "1080x1920",
        "target_fps": 30,
        "encoder": "h264_nvenc",
        "total_duration_seconds": 0.0,
    })


class JobCreateRequest(BaseModel):
    title: str = Field(..., min_length=2, max_length=200, example="The Mystery of Deep Sea Bioluminescence")
    topic: str = Field(..., min_length=5, max_length=10000, example="Explain how deep sea creatures produce light in the abyss")
    visual_style: VisualStyle = VisualStyle.DEEP_DIVE_3D


class SceneResponse(BaseModel):
    id: int
    scene_index: int
    narration_text: str
    visual_prompt: str
    duration_seconds: float
    clip_path: Optional[str] = None
    status: str

    class Config:
        from_attributes = True


class JobLogResponse(BaseModel):
    id: int
    level: str
    stage: str
    message: str
    timestamp: datetime.datetime

    class Config:
        from_attributes = True


class JobResponse(BaseModel):
    id: str
    title: str
    topic: str
    visual_style: str
    status: str
    error_message: Optional[str] = None
    manifest_path: str
    output_video_path: Optional[str] = None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    scenes: List[SceneResponse] = []

    class Config:
        from_attributes = True
