import os
import json
from pathlib import Path

from backend.schemas.job_manifest import VisualStyle
from backend.schemas.script_schema import ScriptGenerationRequest
from backend.services.script_service import ScriptService
from backend.services.scene_prompt_manager import ScenePromptManager
from backend.services.pipeline_controller import PipelineController

LION_MOON_STORY = """A lonely little girl sits on a rooftop every night, talking to the moon because she has nobody else to talk to. One evening, Sheru, a ridiculously rich and overconfident golden lion in a white three-piece suit, discovers her crying because she believes the moon has disappeared behind the clouds forever.
Sheru dramatically promises to bring the moon back. He jumps into his golden supercar, launches a ridiculous high-speed chase through a futuristic city, accidentally destroys his own luxury fountain, gets chased by police drones, and eventually climbs a gigantic radio tower during a thunderstorm.
In an absolutely ridiculous climax, he uses a giant construction crane to pull the clouds apart. The moon reappears, illuminating the entire city.
But here's the twist: the little girl wasn't actually waiting for the moon. She was waiting for her father, who works night shifts and has finally returned home.
Sheru watches them hug. He quietly removes his expensive gold wristwatch and gives it to the girl, saying in deep, emotional Hindi: 'Ghadi toh waqt batati hai, beta... par apne log waqt nikaalte hain.'
The girl laughs and asks Sheru whether he can also bring her the stars tomorrow. Sheru looks at the camera, exhausted, and says: 'Ek chaand ke liye jaan nikal gayi... ab taare bhi chahiye!'
End with a spectacular cinematic wide shot of the glowing city, the moon, and Sheru laughing with the little girl and her father."""


def test_cinematic_story_mode():
    print("=" * 70)
    print("AI SHORTS STUDIO - CINEMATIC STORY MODE VERIFICATION")
    print("=" * 70)

    # 1. Generate Script for "The Lion Who Stole the Moon" under DEEP_DIVE_3D style
    print("\n--- 1. Generating Script in Cinematic Story Mode ---")
    req = ScriptGenerationRequest(
        topic=LION_MOON_STORY,
        visual_style=VisualStyle.DEEP_DIVE_3D,
        target_duration=30.0,
        target_audience="General audience, entertainment",
        language="en-us",  # English narration path (Hinglish is covered by test_hindi_pipeline.py)
        tone="cinematic, emotional, dramatic, humorous"
    )

    script = ScriptService.generate_script(req)
    print(f"Title: {script.title}")
    print(f"Hook: {script.hook}")
    print(f"Total Scenes: {len(script.scenes)}")
    print(f"Total Estimated Duration: {script.total_estimated_duration}s")

    # 2. Assertions on Narrative & Character Preservation
    print("\n--- 2. Verifying Narrative & Character Preservation ---")
    full_narration = script.complete_narration
    print(f"\nFull Spoken Narration:\n{full_narration}\n")

    assert "Sheru" in full_narration, "Sheru the lion must be present in narration"
    assert "girl" in full_narration.lower(), "Little girl must be present in narration"
    assert "father" in full_narration.lower(), "Father twist must be present in narration"
    assert "crane" in full_narration.lower(), "Crane climax beat must be present in narration"
    assert "supercar" in full_narration.lower(), "Supercar chase must be present in narration"

    # 3. Assertions on Verbatim Roman Hinglish Dialogue
    print("--- 3. Verifying Verbatim Roman Hinglish Dialogue & Punchlines ---")
    quote_1 = "Ghadi toh waqt batati hai, beta... par apne log waqt nikaalte hain."
    quote_2 = "Ek chaand ke liye jaan nikal gayi... ab taare bhi chahiye!"
    assert quote_1 in full_narration, f"Expected quote 1 '{quote_1}' in narration"
    assert quote_2 in full_narration, f"Expected quote 2 '{quote_2}' in narration"
    print(f"[PASS] Dialogue Quote 1 preserved verbatim: '{quote_1}'")
    print(f"[PASS] Dialogue Quote 2 preserved verbatim: '{quote_2}'")

    # 4. Assertions on Zero Documentary / Marine Science Filler
    print("\n--- 4. Verifying Zero Documentary / Marine Filler ---")
    filler_words = [
        "science struggled to explain",
        "extreme environmental forces",
        "phenomenon",
        "abyssal leviathan",
        "mariana hadal trench",
        "marine snow",
        "bioluminescent photophore"
    ]
    for filler in filler_words:
        assert filler not in full_narration.lower(), f"Filler word '{filler}' found in story narration!"
        assert filler not in " ".join(s.visual_description.lower() for s in script.scenes), f"Filler '{filler}' found in visual descriptions!"
    print("[PASS] 100% clean: Zero generic science documentary or ocean filler detected.")

    # 5. Test Scene Prompt Manager & Visual Reference Spec
    print("\n--- 5. Verifying Dynamic Visual Reference Spec & Prompt Continuity ---")
    ref_spec = ScenePromptManager.create_visual_reference_spec("test-job-lion", script)
    print(f"Dynamic Character Spec: Name='{ref_spec.primary_character.name}', Appearance='{ref_spec.primary_character.appearance[:60]}...'")
    print(f"Dynamic Environment Spec: Setting='{ref_spec.environment.setting_name}', Lighting='{ref_spec.environment.lighting_conditions[:60]}...'")

    assert ref_spec.primary_character.name == "Sheru", "Character name must be 'Sheru'"
    assert "lion" in ref_spec.primary_character.appearance.lower()
    assert "suit" in ref_spec.primary_character.clothing.lower()
    assert "city" in ref_spec.environment.setting_name.lower() or "rooftop" in ref_spec.environment.setting_name.lower()
    assert "mariana" not in ref_spec.environment.setting_name.lower()

    # 6. Verify Scene Prompts
    prompts = ScenePromptManager.generate_scene_prompts(script, visual_reference=ref_spec)
    print(f"\nGenerated {len(prompts)} Production Prompts:")
    for p in prompts:
        print(f"\n[{p['scene_id']}] ({p['duration_seconds']}s):")
        print(f"  Spoken: \"{p['narration']}\"")
        print(f"  Prompt: {p['production_prompt']}")
        assert "Sheru" in p["production_prompt"], "Each prompt must include Sheru anchor"
        assert "Mariana Hadal Trench" not in p["production_prompt"], "Style must not inject Mariana Trench into city story"

    print("\n" + "=" * 70)
    print("ALL CINEMATIC STORY MODE TESTS PASSED (100%)")
    print("=" * 70)


if __name__ == "__main__":
    test_cinematic_story_mode()
