from typing import List, Optional
from pydantic import BaseModel, Field


class CharacterReference(BaseModel):
    name: str = Field(..., description="Character or subject name")
    role: str = Field(default="Protagonist", description="Narrative role (e.g. Protagonist, Antagonist, Observer)")
    appearance: str = Field(..., description="Physical appearance, skin/material texture, facial features, proportions")
    clothing: str = Field(..., description="Consistent garments, colors, textures, and gear")
    distinctive_features: str = Field(..., description="Unmistakable identifying marks (e.g. glowing violet ocular implant, yellow scarf)")


class EnvironmentReference(BaseModel):
    setting_name: str = Field(..., description="Primary world/setting name (e.g. Mariana Hadal Trench, Clockwork Workshop)")
    lighting_conditions: str = Field(..., description="Primary light source, direction, and color temperature (e.g. deep blue caustic rays with bioluminescent rim light)")
    atmosphere: str = Field(..., description="Atmospheric elements (e.g. particulate marine snow, volumetric mist, dust motes)")
    color_palette: List[str] = Field(default_factory=lambda: ["#000B18", "#002B49", "#00F0FF", "#9D00FF"], description="Hex color codes defining the cohesive palette")


class VisualReferenceSpec(BaseModel):
    """
    Project-level visual reference specification to maximize visual and character
    continuity across independently generated Google Flow / Veo video clips.
    """
    project_id: str
    primary_character: Optional[CharacterReference] = None
    environment: EnvironmentReference
    negative_instructions: str = Field(
        default="no text overlays, no subtitles, no watermarks, no distorted anatomy, no extra limbs, no inconsistent lighting, no modern digital clutter, no black bars, no blur",
        description="Global negative prompt tokens applied to all scenes"
    )
    consistency_disclaimer: str = Field(
        default="Note: In text-to-video diffusion models, text prompts provide directional styling and prompt anchoring. For identical character likeness, use consistent seed prompting or Flow's image reference tools.",
        description="Disclaimer on text-based generative continuity limits"
    )
