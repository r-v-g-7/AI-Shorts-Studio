import logging
from typing import Optional, Dict, Any

from backend.schemas.script_schema import ShortsScript, ScriptGenerationRequest
from backend.services.llm_provider import get_llm_provider, BaseLLMProvider

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
