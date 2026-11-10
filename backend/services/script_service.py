import re
import logging
from typing import Optional, Dict, Any, List

from backend.schemas.job_manifest import VisualStyle
from backend.schemas.script_schema import ShortsScript, SceneScript, ScriptGenerationRequest
from backend.services.llm_provider import get_llm_provider, BaseLLMProvider, STYLE_RECIPES

logger = logging.getLogger(__name__)


class ScriptService:
    @staticmethod
    def generate_script(
        request: ScriptGenerationRequest,
        provider: Optional[BaseLLMProvider] = None,
        api_key: Optional[str] = None
    ) -> ShortsScript:
        """
        Generates and strictly validates a structured Shorts script with scene-by-scene
        narrations, Flow/Veo visual prompts, camera movement directives, and sound effects.
        """
        active_provider = provider or get_llm_provider(api_key=api_key)
        logger.info(f"Generating script for topic '{request.topic}' using {active_provider.__class__.__name__}")

        script = active_provider.generate_script(request)

        # Validate script coherence
        if not script.scenes or len(script.scenes) < 2:
            raise ValueError("Script must contain at least 2 distinct visual scenes.")

        # Ensure total duration and scene durations are reasonable
        calculated_duration = sum(s.estimated_duration_seconds for s in script.scenes)
        if calculated_duration <= 0:
            raise ValueError("Total scene duration must be greater than zero.")

        # Re-sync complete narration if needed
        if not script.complete_narration.strip():
            script.complete_narration = " ".join(s.narration.strip() for s in script.scenes)

        logger.info(
            f"Script generated successfully: '{script.title}' ({len(script.scenes)} scenes, ~{calculated_duration:.1f}s)"
        )
        return script

    @classmethod
    def parse_raw_script_to_scenes(
        cls,
        raw_script: str,
        num_scenes: int = 4,
        visual_style: VisualStyle = VisualStyle.DEEP_DIVE_3D,
        title: Optional[str] = None
    ) -> ShortsScript:
        """
        Mode B: Takes a complete verbatim raw voiceover script and splits it into
        `num_scenes` sequential visual scenes WITHOUT REWRITING, altering, or hallucinating
        any spoken words. Synthesizes cinematic visual instructions and camera choreography.
        """
        text = raw_script.strip()
        if not text:
            raise ValueError("Raw script text cannot be empty.")

        num_scenes = max(1, min(8, int(num_scenes or 4)))
        clean_title = (title or "").strip() or f"Short: {text[:32]}..."

        # Sentence-aware splitting with complete word preservation
        raw_sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        if not raw_sentences:
            raw_sentences = [text]

        scene_narrations: List[str] = []
        if len(raw_sentences) >= num_scenes:
            # Distribute sentences evenly across scenes
            k, m = divmod(len(raw_sentences), num_scenes)
            chunks = [raw_sentences[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(num_scenes)]
            scene_narrations = [" ".join(c).strip() for c in chunks if c]
        else:
            # Fall back to word-level chunking to avoid altering any words
            words = text.split()
            k, m = divmod(len(words), num_scenes)
            chunks = [words[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(num_scenes)]
            scene_narrations = [" ".join(c).strip() for c in chunks if c]

        # Ensure we have exactly num_scenes
        while len(scene_narrations) < num_scenes:
            scene_narrations.append(scene_narrations[-1] if scene_narrations else text)

        recipe = STYLE_RECIPES.get(visual_style, STYLE_RECIPES[VisualStyle.DEEP_DIVE_3D])
        camera_moves = [
            "Low-angle worm's-eye macro probe shot peering upward, f/2.0 shallow depth of field",
            "Medium wide profile shot with dynamic pan across chaotic environment action, 35mm lens",
            "Dutch-angle extreme close-up with high-tension handheld vibration and volumetric atmospheric lighting",
            "High-speed vertical tracking shot tilting straight upward following explosive motion, 60fps",
            "Slow continuous push-in macro probe lens, cinematic dolly zoom, 60fps smooth pan",
            "Dynamic 360-degree orbital rotation around anatomical/mechanical core"
        ]

        scenes: List[SceneScript] = []
        dur_per_scene = round(60.0 / num_scenes, 2)
        for i, narration in enumerate(scene_narrations):
            cam = camera_moves[i % len(camera_moves)]
            vis_desc = f"{recipe['visual_tokens']} illustrating: {narration[:80]}"
            scenes.append(
                SceneScript(
                    index=i,
                    narration=narration,
                    visual_description=vis_desc,
                    camera_movement=cam,
                    estimated_duration_seconds=dur_per_scene,
                    sound_effects=recipe["audio_tokens"]
                )
            )

        return ShortsScript(
            title=clean_title,
            hook=scene_narrations[0][:60],
            complete_narration=text,
            visual_style=visual_style,
            target_audience="General mobile audience",
            scenes=scenes
        )
