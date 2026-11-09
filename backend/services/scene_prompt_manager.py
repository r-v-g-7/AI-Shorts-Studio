import json
from pathlib import Path
from typing import Dict, Any, List, Optional
from backend.schemas.job_manifest import VisualStyle
from backend.schemas.script_schema import ShortsScript, SceneScript
from backend.schemas.consistency_schema import (
    VisualReferenceSpec,
    CharacterReference,
    EnvironmentReference
)
from backend.services.job_service import JobService

# Production style matrices for the six supported styles
STYLE_PROFILES: Dict[VisualStyle, Dict[str, Any]] = {
    VisualStyle.TALL_TALE_3D: {
        "name": "Tall Tale 3D",
        "aesthetic": "Stylized 3D handcrafted claymation character animation, tactile stop-motion clay fingerprint textures, subsurface scattering, charming exaggerated proportions",
        "environment": "Handmade miniature world, warm rustic storybook lighting, soft incandescent glow, miniature physical set props",
        "camera_default": "Continuous low-angle tracking shot, tilt-shift miniature depth-of-field, f/2.8, dynamic storybook framing",
        "character_consistency": "Consistent stylized clay protagonist with distinct exaggerated facial features, wearing textured knitted clothing",
        "default_character": {
            "name": "Clay Explorer",
            "role": "Protagonist",
            "appearance": "Charming stylized stop-motion clay adventurer with round tactile features, fingerprint indentations, visible subsurface clay glow",
            "clothing": "Chunky mustard-yellow knitted wool sweater and olive canvas utility trousers",
            "distinctive_features": "Oversized round brass aviator goggles perched on forehead with reflective glass lenses"
        },
        "default_environment": {
            "setting_name": "Miniature Storybook Diorama",
            "lighting_conditions": "Warm golden incandescent desk lamp glow with soft directional rim lighting",
            "atmosphere": "Subtle airborne micro-dust motes drifting in shafts of soft warm light",
            "color_palette": ["#FFB703", "#FB8500", "#023047", "#219EBC"]
        },
        "negative_prompt": "no photorealistic humans, no flat 2D cartoon, no digital vector graphics, no text overlays, no subtitles, no watermarks, no distorted anatomy, no black bars, no 16:9 widescreen letterbox, no blurry background artifacts"
    },
    VisualStyle.DEEP_DIVE_3D: {
        "name": "Deep Dive 3D",
        "aesthetic": "Hyper-realistic Unreal Engine 5 nanite render, volumetric macro depth, raytraced subsurface scattering, 8k cinematic textures, dramatic lighting",
        "environment": "Atmospheric cinematic environment with volumetric depth, dramatic directional lighting, and deep ambient contrast",
        "camera_default": "Slow continuous push-in macro probe lens, cinematic dolly zoom, dynamic rack focus, 60fps smooth pan",
        "character_consistency": "Consistent photorealistic protagonist with detailed physical textures and expressive cinematic lighting",
        "default_character": {
            "name": "Cinematic Protagonist",
            "role": "Protagonist",
            "appearance": "High-fidelity detailed 3D figure with realistic surface textures and volumetric rim lighting",
            "clothing": "Tailored apparel matching narrative role with detailed fabric physics",
            "distinctive_features": "Consistent recognizable silhouette and facial features"
        },
        "default_environment": {
            "setting_name": "Cinematic Atmosphere",
            "lighting_conditions": "Volumetric directional key lighting with deep contrast and rich atmospheric rim lights",
            "atmosphere": "Subtle atmospheric depth and floating ambient particles",
            "color_palette": ["#0B192C", "#1E3E62", "#00ADB5", "#EEEEEE"]
        },
        "negative_prompt": "no cartoon rendering, no oversaturated amateur lighting, no low-resolution textures, no text overlays, no subtitles, no watermarks, no frame borders, no blurry focus"
    },
    VisualStyle.INSIDE_OUT: {
        "name": "Inside Out",
        "aesthetic": "Hyper-detailed 3D anatomical and mechanical cutaway x-ray visualization, transparent acrylic and glass cross-section layers showing inner gears and glowing energy conduits",
        "environment": "Minimalist high-tech dark studio backdrop, crisp rim lighting, floating holographic dissection lines, glowing neon conduits",
        "camera_default": "Smooth continuous 360-degree orbital rotation, vertical linear macro scan, high-precision technical perspective",
        "character_consistency": "Consistent cross-section subject maintaining mechanical and biological alignment across all layers",
        "default_character": {
            "name": "Cybernetic Anatomical Core",
            "role": "Subject",
            "appearance": "Precision engineered hybrid of organic muscle fibers and titanium servo mechanisms encased in crystal-clear tempered acrylic",
            "clothing": "Sleek matte-black carbon composite chassis frame with exposed glowing gold wiring",
            "distinctive_features": "Rotating central magnetic containment core glowing electric emerald green"
        },
        "default_environment": {
            "setting_name": "Holographic Dissection Void",
            "lighting_conditions": "Razor-sharp studio key light with cyan and magenta secondary rim accents",
            "atmosphere": "Fine laser measurement grid lines floating faintly in the air",
            "color_palette": ["#0F172A", "#06B6D4", "#10B981", "#F59E0B"]
        },
        "negative_prompt": "no flat 2D medical diagrams, no messy wireframes, no low-poly models, no text, no labels, no watermarks, no blur"
    },
    VisualStyle.STICK_STORY: {
        "name": "Stick Story",
        "aesthetic": "High-end 3D minimalist stick-figure world, matte physical white porcelain stick figures set inside a realistic 3D physical environment with cinematic physics",
        "environment": "Monochromatic stark contrast background with vibrant single-color accent lighting, clean studio floor with soft ambient contact shadows",
        "camera_default": "Fast-paced dynamic action camera, whip pans, dynamic zoom-ins, high-energy Dutch angles",
        "character_consistency": "Consistent 3D white stick-figure protagonist with sharp expressive body language and fluid physical weight",
        "default_character": {
            "name": "Stick Hero",
            "role": "Protagonist",
            "appearance": "Matte smooth white ceramic stick figure with rounded joints, solid physical presence, dynamic athletic silhouette",
            "clothing": "None (solid clean white porcelain material with subtle light reflection)",
            "distinctive_features": "Single bright crimson red cape that flutters with exaggerated realistic wind physics"
        },
        "default_environment": {
            "setting_name": "Infinite Minimalist Stage",
            "lighting_conditions": "Strong overhead spotlight casting deep crisp contact shadows onto a polished dark floor",
            "atmosphere": "Crisp, hyper-clean atmosphere with occasional floating geometric dust particles",
            "color_palette": ["#18181B", "#FAFAFA", "#EF4444", "#71717A"]
        },
        "negative_prompt": "no 2D rough pencil doodles, no children crayon drawings, no messy sketches, no text overlays, no subtitles, no watermarks, no blurry video"
    },
    VisualStyle.NIGHT_FILE_3D: {
        "name": "Night File 3D",
        "aesthetic": "Eerie vintage found-footage 3D mystery aesthetic, analog night-vision infrared camera, grainy VHS artifacting, volumetric tactical flashlight beams",
        "environment": "Midnight Appalachian forest or abandoned industrial corridor, thick volumetric fog, deep impenetrable shadows, eerie stillness",
        "camera_default": "Claustrophobic handheld bodycam / trail-cam perspective, subtle breathing sway, sudden flashlight re-centering",
        "character_consistency": "Consistent shadowy creature or investigator silhouette glimpsed through dense foliage and foggy darkness",
        "default_character": {
            "name": "Shadow Entity / Field Agent",
            "role": "Subject",
            "appearance": "Tall elongated humanoid silhouette with reflective retinas that catch flashlight glare like a nocturnal predator",
            "clothing": "Tattered charcoal field jacket with frayed collar and tactical webbing",
            "distinctive_features": "Bright iridescent eye-shine when illuminated directly by the flashlight beam"
        },
        "default_environment": {
            "setting_name": "Fog-Drenched Appalachian Pines",
            "lighting_conditions": "Single harsh handheld LED flashlight beam piercing impenetrable black fog with green night-vision cast",
            "atmosphere": "Heavy swirling ground fog and cold condensation mist",
            "color_palette": ["#020617", "#064E3B", "#10B981", "#64748B"]
        },
        "negative_prompt": "no bright daylight, no vibrant cheerful pastel colors, no cartoon figures, no text overlays, no UI graphics, no watermarks, no clean digital look"
    },
    VisualStyle.PAPER_ATLAS: {
        "name": "Paper Atlas",
        "aesthetic": "Intricate multi-layered papercraft pop-up book 3D animation, laser-cut textured cardboard and aged parchment layers with physical depth and cast shadows",
        "environment": "Warm incandescent desk-lamp lighting, visible cotton paper fibers, deckled edges, subtle folding creases, physical papercraft realism",
        "camera_default": "Top-down 45-degree isometric slow dolly pan across unfolding pop-up mechanical structures",
        "character_consistency": "Consistent layered cardboard cut-out figures with articulated paper joints and delicate hand-painted accents",
        "default_character": {
            "name": "Parchment Cartographer",
            "role": "Protagonist",
            "appearance": "3D multi-layered cutout figure made of heavyweight cream cotton rag paper, hand-drawn sepia linework",
            "clothing": "Accordion-folded navy blue textured paper jacket with miniature brass brad buttons",
            "distinctive_features": "Articulated paper arm holding a miniature cut-paper brass astrolabe"
        },
        "default_environment": {
            "setting_name": "Unfolding Grand Atlas",
            "lighting_conditions": "Gentle 2700K incandescent side-lighting revealing paper relief depth and delicate drop shadows",
            "atmosphere": "Subtle paper fold creases, visible fibrous paper tooth, tactile mechanical pop-up hinges",
            "color_palette": ["#FEF3C7", "#D97706", "#78350F", "#1E3A8A"]
        },
        "negative_prompt": "no glossy digital plastic, no shiny raytraced metal, no flat 2D clipart, no text overlays, no watermarks, no blur, no modern CGI shine"
    }
}


class ScenePromptManager:
    """
    Manages generation, formatting, and export of individual production prompts
    for Google Flow / Veo with cross-scene character and environment consistency.
    """

    @classmethod
    def create_visual_reference_spec(
        cls,
        job_id: str,
        script: ShortsScript,
        character_override: Optional[CharacterReference] = None,
        environment_override: Optional[EnvironmentReference] = None
    ) -> VisualReferenceSpec:
        """
        Creates a structured project-level Visual Reference Specification
        to maintain character appearance, clothing, and environmental continuity.
        Dynamically extracts story characters and environments from the script.
        """
        style_key = script.visual_style
        profile = STYLE_PROFILES.get(style_key, STYLE_PROFILES[VisualStyle.DEEP_DIVE_3D])

        text_corpus = (
            f"{script.title} {script.complete_narration} " +
            " ".join(s.visual_description for s in script.scenes) + " " +
            " ".join(s.narration for s in script.scenes)
        ).lower()

        # Dynamic character extraction
        if character_override:
            char_spec = character_override
        elif "sheru" in text_corpus or ("lion" in text_corpus and "suit" in text_corpus):
            char_spec = CharacterReference(
                name="Sheru",
                role="Protagonist",
                appearance="Majestic anthropomorphic golden lion with a regal golden mane, warm amber eyes, and confident posture",
                clothing="Immaculate bespoke white three-piece suit with gold buttons and silk pocket square",
                distinctive_features="Golden lion fur, tailored white suit, luxury gold wristwatch"
            )
        elif "mariana" in text_corpus or "abyssal" in text_corpus or "bioluminescent" in text_corpus:
            char_spec = CharacterReference(
                name="Abyssal Bioluminescent Specimen",
                role="Subject",
                appearance="Translucent deep-sea organism with semi-transparent gelatinous dermal layer displaying glowing interior anatomical organs",
                clothing="Natural bioluminescent dermal plates with iridescent cyan scales",
                distinctive_features="Pulsing double-helix violet photophore line running down its spine"
            )
        else:
            char_spec = CharacterReference(
                name=profile["default_character"]["name"],
                role=profile["default_character"]["role"],
                appearance=profile["default_character"]["appearance"],
                clothing=profile["default_character"]["clothing"],
                distinctive_features=profile["default_character"]["distinctive_features"]
            )

        # Dynamic environment extraction
        if environment_override:
            env_spec = environment_override
        elif any(k in text_corpus for k in ["rooftop", "city", "supercar", "tower", "crane", "streets"]):
            env_spec = EnvironmentReference(
                setting_name="Futuristic City Skyline & Rooftops",
                lighting_conditions="Midnight ambient glow with radiant moon illumination and neon architectural rim lighting",
                atmosphere="Atmospheric city night mist under clearing storm clouds",
                color_palette=["#0A192F", "#172A45", "#F5A623", "#F8F9FA"]
            )
        elif any(k in text_corpus for k in ["mariana", "trench", "abyss", "hadal", "deep sea"]):
            env_spec = EnvironmentReference(
                setting_name="Mariana Hadal Trench",
                lighting_conditions="Pitch black void illuminated solely by glowing bioluminescent caustics and subtle cobalt rim light",
                atmosphere="Dense particulate marine snow slowly drifting through volumetric water beams",
                color_palette=["#000B18", "#002B49", "#00F0FF", "#9D00FF"]
            )
        else:
            env_spec = EnvironmentReference(
                setting_name=profile["default_environment"]["setting_name"],
                lighting_conditions=profile["default_environment"]["lighting_conditions"],
                atmosphere=profile["default_environment"]["atmosphere"],
                color_palette=profile["default_environment"]["color_palette"]
            )

        return VisualReferenceSpec(
            project_id=job_id,
            primary_character=char_spec,
            environment=env_spec,
            negative_instructions=profile["negative_prompt"]
        )

    @classmethod
    def generate_scene_prompts(
        cls,
        script: ShortsScript,
        style_override: Optional[VisualStyle] = None,
        visual_reference: Optional[VisualReferenceSpec] = None
    ) -> List[Dict[str, Any]]:
        style_key = style_override or script.visual_style
        profile = STYLE_PROFILES.get(style_key, STYLE_PROFILES[VisualStyle.DEEP_DIVE_3D])

        # Character & Environment anchors
        if visual_reference and visual_reference.primary_character:
            char = visual_reference.primary_character
            char_anchor = f"Subject: {char.name}, {char.appearance}, wearing {char.clothing}. Key feature: {char.distinctive_features}."
        else:
            char_anchor = f"Subject: {profile['character_consistency']}."

        if visual_reference and visual_reference.environment:
            env = visual_reference.environment
            palette_str = ", ".join(env.color_palette)
            env_anchor = f"Setting: {env.setting_name}. Lighting: {env.lighting_conditions}. Atmosphere: {env.atmosphere}. Palette: [{palette_str}]."
        else:
            env_anchor = f"Environment: {profile['environment']}."

        negative_text = visual_reference.negative_instructions if visual_reference else profile["negative_prompt"]

        production_prompts: List[Dict[str, Any]] = []

        for scene in script.scenes:
            camera = scene.camera_movement or profile["camera_default"]
            
            # Formulate the Google Flow / Veo prompt
            full_prompt = (
                f"9:16 vertical video. {profile['aesthetic']}. "
                f"Scene: {scene.visual_description}. "
                f"{char_anchor} "
                f"{env_anchor} "
                f"Cinematography: {camera}. "
                f"Duration: {scene.estimated_duration_seconds:.1f}s. Ultra-high definition, cinematic lighting."
            )

            prompt_entry = {
                "scene_index": scene.index,
                "scene_id": f"scene_{scene.index + 1:02d}",
                "narration": scene.narration,
                "duration_seconds": scene.estimated_duration_seconds,
                "aspect_ratio": "9:16",
                "visual_style": profile["name"],
                "production_prompt": full_prompt,
                "camera_directive": camera,
                "negative_prompt": negative_text,
                "suggested_filename": f"scene_{scene.index + 1:02d}.mp4"
            }
            production_prompts.append(prompt_entry)

        return production_prompts

    @classmethod
    def export_prompts(
        cls,
        job_id: str,
        script: ShortsScript,
        visual_reference: Optional[VisualReferenceSpec] = None
    ) -> Dict[str, str]:
        """
        Exports prompts to JSON and formatted human-readable TXT, plus
        exports the character and environment reference sheet in the job folder.
        """
        # Generate or use visual reference
        ref_spec = visual_reference or cls.create_visual_reference_spec(job_id, script)
        prompts = cls.generate_scene_prompts(script, visual_reference=ref_spec)
        job_dir = JobService.get_job_directory(job_id)

        json_file = job_dir / "prompts.json"
        txt_file = job_dir / "prompts.txt"
        ref_json_file = job_dir / "reference_sheet.json"
        ref_txt_file = job_dir / "reference_sheet.txt"

        # 1. Write structured prompts JSON
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump({
                "job_id": job_id,
                "title": script.title,
                "visual_style": script.visual_style.value,
                "scene_count": len(prompts),
                "visual_reference": ref_spec.model_dump(),
                "prompts": prompts
            }, f, indent=2)

        # 2. Write structured reference sheet JSON
        with open(ref_json_file, "w", encoding="utf-8") as f:
            f.write(ref_spec.model_dump_json(indent=2))

        # 3. Write formatted Reference Sheet TXT
        char = ref_spec.primary_character
        env = ref_spec.environment
        ref_lines = [
            "=" * 70,
            "AI SHORTS STUDIO - VISUAL & CHARACTER REFERENCE SHEET",
            f"Job ID: {job_id}",
            f"Title:  {script.title}",
            "=" * 70,
            "",
            "[ PRIMARY CHARACTER SPECIFICATION ]",
            f"Name:                 {char.name if char else 'N/A'}",
            f"Role:                 {char.role if char else 'N/A'}",
            f"Physical Appearance:  {char.appearance if char else 'N/A'}",
            f"Clothing & Material:  {char.clothing if char else 'N/A'}",
            f"Distinctive Feature:  {char.distinctive_features if char else 'N/A'}",
            "",
            "[ ENVIRONMENT SPECIFICATION ]",
            f"Setting Name:         {env.setting_name}",
            f"Lighting Conditions:  {env.lighting_conditions}",
            f"Atmosphere:           {env.atmosphere}",
            f"Color Palette:        {', '.join(env.color_palette)}",
            "",
            "[ GOOGLE FLOW / VEO CONTINUITY ADVICE ]",
            ref_spec.consistency_disclaimer,
            "1. When generating clips in Google Flow, keep the primary character tokens verbatim in each prompt.",
            "2. If Google Flow supports seed locking, reuse the same seed or use the image-to-video reference tool with scene_01.",
            "3. Use the global negative prompt below for all scenes to avoid unwanted artifacts.",
            "",
            "[ GLOBAL NEGATIVE PROMPT ]",
            ref_spec.negative_instructions,
            "=" * 70
        ]
        with open(ref_txt_file, "w", encoding="utf-8") as f:
            f.write("\n".join(ref_lines))

        # 4. Write human-friendly TXT with clear copy-paste blocks for Google Flow
        txt_lines = [
            "=" * 70,
            "AI SHORTS STUDIO - GOOGLE FLOW / VEO SCENE PRODUCTION SHEET",
            f"Job ID: {job_id}",
            f"Title:  {script.title}",
            f"Style:  {script.visual_style.value.upper()}",
            f"Aspect: 9:16 Vertical Video (1080x1920)",
            "=" * 70,
            "",
            "INSTRUCTIONS FOR GOOGLE FLOW / VEO:",
            "1. Copy each 'PRODUCTION PROMPT' below into Google Flow / Veo.",
            "2. Set Aspect Ratio to 9:16 (Vertical).",
            "3. Generate and download each video clip.",
            "4. Rename and save files as indicated (e.g. scene_01.mp4, scene_02.mp4...)",
            f"   into the job's incoming folder: storage/jobs/{job_id}/incoming_clips/",
            "5. The studio's hot-folder watcher will automatically detect, validate, and assemble them.",
            "",
            "-" * 70,
        ]

        for p in prompts:
            txt_lines.extend([
                f"\n[ SCENE {p['scene_index'] + 1} OF {len(prompts)} ] --> Save as: {p['suggested_filename']}",
                f"Estimated Duration: {p['duration_seconds']:.1f} seconds",
                f"Spoken Narration:   \"{p['narration']}\"",
                "",
                "--- COPY PROMPT BELOW ---",
                p["production_prompt"],
                "",
                "--- NEGATIVE PROMPT (If supported) ---",
                p["negative_prompt"],
                "-" * 70
            ])

        with open(txt_file, "w", encoding="utf-8") as f:
            f.write("\n".join(txt_lines))

        return {
            "json_path": str(json_file),
            "txt_path": str(txt_file),
            "ref_json_path": str(ref_json_file),
            "ref_txt_path": str(ref_txt_file)
        }
