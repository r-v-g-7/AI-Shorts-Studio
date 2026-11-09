from typing import List, Optional
from pydantic import BaseModel, Field
from backend.schemas.job_manifest import VisualStyle


def _default_narration_language() -> str:
    from backend.config import settings
    return settings.NARRATION_LANGUAGE


class SceneScript(BaseModel):
    index: int = Field(..., ge=0, description="0-indexed sequence position of the scene")
    narration: str = Field(..., min_length=1, description="Exact spoken narration text for this specific scene")
    visual_description: str = Field(..., min_length=5, description="High-detail scene visual description optimized for Google Flow / Veo")
    camera_movement: str = Field(
        default="Slow continuous cinematic dolly-in, 35mm lens, f/2.8",
        description="Cinematic camera instruction"
    )
    sound_effects: List[str] = Field(
        default_factory=list,
        description="Suggested sound effect tags, e.g. ['deep ocean drone', 'sonar ping']"
    )
    estimated_duration_seconds: float = Field(
        default=5.0,
        gt=0,
        description="Estimated scene duration in seconds"
    )


class ShortsScript(BaseModel):
    title: str = Field(..., min_length=2, max_length=150, description="Catchy viral YouTube Shorts title")
    hook: str = Field(..., min_length=5, description="Opening 3-second hook that stops viewers from scrolling")
    complete_narration: str = Field(..., min_length=10, description="Complete aggregated spoken narration script")
    target_audience: str = Field(default="Curious mobile viewers", description="Intended target demographic")
    visual_style: VisualStyle = Field(default=VisualStyle.DEEP_DIVE_3D, description="Core visual style recipe")
    total_estimated_duration: float = Field(default=25.0, gt=0, description="Target total duration in seconds")
    scenes: List[SceneScript] = Field(..., min_items=2, description="List of sequential visual scenes")


class ScriptGenerationRequest(BaseModel):
    topic: Optional[str] = Field(default=None, min_length=5, max_length=10000, description="Core video topic, question or subject (defaults to job topic if omitted)")
    target_duration: float = Field(default=25.0, ge=10.0, le=60.0, description="Desired video duration in seconds")
    visual_style: VisualStyle = Field(default=VisualStyle.DEEP_DIVE_3D, description="Visual rendering aesthetic")
    target_audience: str = Field(default="curious mobile audience", description="Target audience description")
    language: str = Field(
        default_factory=_default_narration_language,
        description="Narration language: 'hinglish' (Roman-script Hindi, default) or an English code such as 'en-us'"
    )
    tone: str = Field(
        default="high-retention viral storytelling, suspenseful hook, punchy payoff",
        description="Narrative tone instructions"
    )
    custom_instructions: Optional[str] = Field(default=None, description="Additional custom rules or requirements")
    manual_script: Optional[ShortsScript] = Field(default=None, description="Optional manually provided pre-written script")
