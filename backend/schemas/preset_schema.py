from enum import Enum
from typing import Dict, Any, List
from pydantic import BaseModel, Field


class QualityMode(str, Enum):
    DRAFT = "DRAFT"
    BALANCED = "BALANCED"
    HIGH_QUALITY = "HIGH_QUALITY"


class ProductionPreset(BaseModel):
    id: str
    name: str
    target_duration_seconds: int
    estimated_word_count: int
    target_wpm: int
    quality_mode: QualityMode
    description: str
    recommended_scenes: int = 5


PRODUCTION_PRESETS: Dict[str, ProductionPreset] = {
    "preset_30s": ProductionPreset(
        id="preset_30s",
        name="30s Viral Hook",
        target_duration_seconds=30,
        estimated_word_count=75,
        target_wpm=150,
        quality_mode=QualityMode.BALANCED,
        description="Fast-paced, high retention short with immediate hook and snappy reveal.",
        recommended_scenes=4
    ),
    "preset_45s": ProductionPreset(
        id="preset_45s",
        name="45s Story Arc",
        target_duration_seconds=45,
        estimated_word_count=110,
        target_wpm=145,
        quality_mode=QualityMode.BALANCED,
        description="Balanced narrative structure with setup, development, and climax.",
        recommended_scenes=5
    ),
    "preset_60s": ProductionPreset(
        id="preset_60s",
        name="60s Deep Dive",
        target_duration_seconds=60,
        estimated_word_count=150,
        target_wpm=140,
        quality_mode=QualityMode.HIGH_QUALITY,
        description="Comprehensive breakdown with layered visual transitions and climax.",
        recommended_scenes=6
    ),
    "preset_draft": ProductionPreset(
        id="preset_draft",
        name="Fast Draft Preview",
        target_duration_seconds=30,
        estimated_word_count=75,
        target_wpm=150,
        quality_mode=QualityMode.DRAFT,
        description="Rapid preview render with low CPU usage for quick visual check.",
        recommended_scenes=4
    )
}
