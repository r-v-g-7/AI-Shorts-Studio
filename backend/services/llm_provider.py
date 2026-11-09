import abc
import json
import logging
import re
import urllib.request
import urllib.error
from typing import Dict, Any, Optional, List

from backend.config import settings
from backend.schemas.script_schema import ShortsScript, SceneScript, ScriptGenerationRequest
from backend.schemas.job_manifest import VisualStyle

logger = logging.getLogger(__name__)

# Style recipes for prompt engineering
STYLE_RECIPES: Dict[VisualStyle, Dict[str, Any]] = {
    VisualStyle.TALL_TALE_3D: {
        "visual_tokens": "Stylized 3D character animation, claymation texture, stop-motion subsurface scattering, handcrafted miniature world, warm tactile lighting",
        "camera_tokens": "Exaggerated dynamic perspective, continuous low-angle tracking shot, tilt-shift miniature depth-of-field, f/2.8",
        "audio_tokens": ["whimsical wooden percussion", "soft accordion chime", "playful footsteps"]
    },
    VisualStyle.DEEP_DIVE_3D: {
        "visual_tokens": "Unreal Engine 5 nanite render, volumetric macro depth, raytraced subsurface scattering, 8k cinematic textures, atmospheric lighting and deep contrast",
        "camera_tokens": "Slow continuous push-in macro probe lens, cinematic dolly zoom, dynamic rack focus, 60fps smooth pan",
        "audio_tokens": ["cinematic bass riser", "subtle atmospheric hum", "deep ambient impact"]
    },
    VisualStyle.CYBER_PUNK_3D: {
        "visual_tokens": "Volumetric neon lighting, rainy cyberpunk megacity aesthetic, reflective wet asphalt, holographic signage, anamorphic lens flares",
        "camera_tokens": "Dynamic dutch angle pan, fast fpv drone zoom, high contrast neon rim lighting",
        "audio_tokens": ["synthwave bass pulse", "distant siren echo", "electric neon buzz"]
    },
    VisualStyle.INSIDE_OUT: {
        "visual_tokens": "Hyper-detailed 3D technical cutaway x-ray visualization, transparent acrylic layers, glowing energy conduits, high-tech studio lighting",
        "camera_tokens": "Smooth continuous 360-degree orbital rotation, vertical linear macro scan, high-precision technical perspective",
        "audio_tokens": ["mechanical servo whirl", "digital scanning hum", "holographic chime"]
    },
    VisualStyle.STICK_STORY: {
        "visual_tokens": "High-end 3D minimalist porcelain stick-figure aesthetic, physical studio environment, realistic physics, clean single-color accent lighting",
        "camera_tokens": "Fast-paced dynamic action camera, whip pans, dynamic zoom-ins, high-energy Dutch angles",
        "audio_tokens": ["energetic swoosh", "percussive impact", "snappy stick click"]
    },
    VisualStyle.NIGHT_FILE_3D: {
        "visual_tokens": "Eerie vintage found-footage 3D mystery aesthetic, analog night-vision camera texture, grainy artifacting, volumetric tactical light beams",
        "camera_tokens": "Handheld bodycam / trail-cam perspective, subtle breathing sway, sudden re-centering",
        "audio_tokens": ["creepy ambient drone", "vhs tape hiss", "tactical radio crackle"]
    },
    VisualStyle.PAPER_ATLAS: {
        "visual_tokens": "Intricate multi-layered papercraft pop-up book 3D animation, laser-cut textured cardboard, warm incandescent desk lamp lighting",
        "camera_tokens": "Top-down 45-degree isometric slow dolly pan across unfolding pop-up mechanical structures",
        "audio_tokens": ["crisp paper fold", "mechanical cardboard click", "gentle music box note"]
    },
    VisualStyle.DOCUMENTARY_REALISM: {
        "visual_tokens": "Cinematic 35mm film stock, archival documentary lighting, authentic natural textures, historical photorealism",
        "camera_tokens": "Handheld 35mm camera sway, slow observational zoom-in, natural depth of field",
        "audio_tokens": ["vintage film reel hum", "subtle orchestral strings", "room ambient resonance"]
    },
    VisualStyle.CUSTOM: {
        "visual_tokens": "Cinematic high-retention 3D render, photorealistic lighting, 8k resolution, Unreal Engine 5 aesthetic",
        "camera_tokens": "Cinematic continuous tracking camera, 35mm lens, f/2.0",
        "audio_tokens": ["cinematic impact whoosh", "tension riser"]
    }
}


def is_hinglish(language: Optional[str]) -> bool:
    """True when narration should be written in Roman-script Hindi (Hinglish)."""
    return (language or "").strip().lower() in {"hinglish", "hi", "hi-in", "hi-latn", "hindi", "hi_in"}


HINGLISH_DIRECTIVE = """
NARRATION LANGUAGE = ROMAN HINGLISH (MANDATORY):
- Write EVERY "narration" field and "hook" and "complete_narration" in natural, conversational Hindi using ONLY the Latin alphabet (Roman script), exactly as a storyteller would speak it. Example: "Ek akeli bacchi har raat chhat par baithkar chaand se baatein karti thi..."
- NEVER use Devanagari script. NEVER write the narration as an English translation.
- Use English only for common loanwords people really say in Hindi (e.g. supercar, drone, crane, watch, tower, police, suit). Prefer Hindi words otherwise (shehar, baadal, chaand, papa, bacchi, raftaar, intezaar).
- Keep spelling simple and consistent so a Hindi text-to-speech voice reads it correctly (e.g. "chaand", "baatein", "nikaalte").
- If the user supplied dialogue or punchlines, keep them VERBATIM, character for character.
- "visual_description" and "camera_movement" MUST stay in English (they are prompts for a video-generation model).
- Hindi speech runs about 2.5 to 3 words per second: roughly 70 to 85 narrated words for 30 seconds.
"""


class BaseLLMProvider(abc.ABC):
    """Abstract base provider for LLM script generation."""

    @abc.abstractmethod
    def generate_script(self, request: ScriptGenerationRequest) -> ShortsScript:
        """Generates a structured, validated Shorts script."""
        pass


class GeminiLLMProvider(BaseLLMProvider):
    """
    Google AI Studio Gemini API provider (Free Tier).
    Uses the free developer endpoint without consuming Google AI Pro consumer subscription credits.
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.GEMINI_API_KEY
        self.model = model or settings.GEMINI_MODEL or "gemini-2.5-flash"
        if not self.api_key or self.api_key.startswith("your_"):
            raise ValueError("No valid Gemini API key found for GeminiLLMProvider.")

    def _build_system_prompt(self, request: ScriptGenerationRequest) -> str:
        recipe = STYLE_RECIPES.get(request.visual_style, STYLE_RECIPES[VisualStyle.CUSTOM])
        return f"""You are a master viral YouTube Shorts screenwriter and director.
Generate a structured, high-retention YouTube Short script adhering to the following parameters:
- Target Duration: ~{request.target_duration} seconds (roughly 50 to 75 spoken words total).
- Visual Style: {request.visual_style.value}
- Style Tokens for Visual Prompts: {recipe['visual_tokens']}
- Camera Movement Directive: {recipe['camera_tokens']}
- Audience: {request.target_audience}
- Tone: {request.tone}
{f"- Custom Instructions: {request.custom_instructions}" if request.custom_instructions else ""}

MODE SELECTION & DIRECTIVES:

1. CINEMATIC STORY MODE (MANDATORY when input contains an original story, characters, fictional plot beats, dialogue, or emotional arcs):
- PRESERVE CHARACTERS: Extract and retain all specific characters, their names, appearances, clothing, and props (e.g., 'Sheru the golden lion in a bespoke white three-piece suit', 'the little girl', 'her father', 'golden supercar', 'gold wristwatch').
- PRESERVE CORE ACTION BEATS & TWIST: Faithfully sequence the scenes to reflect the real narrative progression (setup -> chase/action -> climax/crane -> twist -> emotional resolution).
- VERBATIM DIALOGUE & ROMAN HINGLISH: If the user provides specific dialogue or punchlines (especially Roman Hinglish quotes like 'Ghadi toh waqt batati hai, beta... par apne log waqt nikaalte hain.' and 'Ek chaand ke liye jaan nikal gayi... ab taare bhi chahiye!'), you MUST incorporate them VERBATIM into the narration of the respective scenes. NEVER drop, translate, or sanitize them!
- NO DOCUMENTARY FILLER: Under NO circumstances convert the story into an educational science documentary. DO NOT use generic phrases like 'Did you know that... holds a secret that science struggled to explain?'. Spoken narration MUST tell the dramatic story itself!
- DYNAMIC SCENE VISUALS: Each scene's visual description MUST depict what is physically happening in the narrative action of that specific scene.
- VISUAL STYLE SEPARATION: Visual Style tokens ({recipe['visual_tokens']}) and camera directives ({recipe['camera_tokens']}) dictate ONLY rendering engine, lighting, camera quality, and atmosphere. They MUST NEVER override story subjects (e.g. Deep Dive 3D means Unreal 5 nanite rendering and dramatic lighting—it does NOT mean turning an urban lion story into deep-sea fish or ocean creatures).

2. EDUCATIONAL / DOCUMENTARY MODE (When input is a factual concept, scientific phenomenon, or documentary subject):
- Create a high-retention educational hook, escalating revelations, and awe-inspiring conclusion.
{HINGLISH_DIRECTIVE if is_hinglish(request.language) else ""}

Format the response strictly as valid JSON matching this schema:
{{
  "title": "Catchy YouTube Shorts Title (under 60 chars)",
  "hook": "Opening 3-second hook sentence",
  "complete_narration": "Full spoken narration transcript",
  "target_audience": "{request.target_audience}",
  "visual_style": "{request.visual_style.value}",
  "total_estimated_duration": {request.target_duration},
  "scenes": [
    {{
      "index": 0,
      "narration": "Spoken words for scene 1",
      "visual_description": "Exact descriptive scene prompt for video generator with style tokens",
      "camera_movement": "{recipe['camera_tokens']}",
      "sound_effects": ["sfx_tag_1", "sfx_tag_2"],
      "estimated_duration_seconds": 5.0
    }}
  ]
}}
Ensure the scenes breakdown covers 4 to 6 scenes that sum up to ~{request.target_duration} seconds.
Output ONLY the JSON object. Do not include markdown codeblocks or extraneous commentary.
"""

    def generate_script(self, request: ScriptGenerationRequest) -> ShortsScript:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        system_instruction = self._build_system_prompt(request)

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": f"Topic: {request.topic}\n\nPlease generate the YouTube Shorts script JSON now."}
                    ]
                }
            ],
            "systemInstruction": {
                "parts": [{"text": system_instruction}]
            },
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.7,
            }
        }

        data_bytes = json.dumps(payload).encode("utf-8")
        http_req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={"Content-Type": "application/json"}
        )

        try:
            with urllib.request.urlopen(http_req, timeout=30) as response:
                result_json = json.loads(response.read().decode("utf-8"))

            raw_text = result_json["candidates"][0]["content"]["parts"][0]["text"]
            script_dict = json.loads(raw_text)
            return ShortsScript.model_validate(script_dict)

        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            logger.error(f"Gemini API HTTP Error {e.code}: {err_body}")
            raise RuntimeError(f"Gemini API failed with status {e.code}: {err_body}")
        except Exception as e:
            logger.error(f"Gemini API request failed: {e}")
            raise RuntimeError(f"Failed to generate script via Gemini API: {e}")


class ManualLLMProvider(BaseLLMProvider):
    """
    Offline/Manual script provider.
    Accepts manual script input or constructs a deterministic, high-retention structured script.
    Guarantees 100% offline availability with 0 API dependencies and zero cost.
    Intelligently switches between Cinematic Story Mode and Educational Documentary Mode.
    """

    def _is_cinematic_story(self, topic: str) -> bool:
        """Determines whether the topic is a fictional narrative story."""
        t_lower = topic.lower()
        story_keywords = [
            "sheru", "lion", "supercar", "crane", "rooftop", "father", "daughter",
            "twist", "girl", "boy", "climax", "story", "ghadi toh", "ek chaand",
            "character", "protagonist", "crying", "promis", "chase", "drones"
        ]
        has_story_keywords = any(kw in t_lower for kw in story_keywords)
        quotes = re.findall(r'["\']([^"\']{4,})["\']', topic)
        has_quotes = len(quotes) >= 1
        is_narrative_length = len(topic.split()) >= 20 and any(k in t_lower for k in ["sits", "discovers", "promises", "climbs", "hugs", "watches", "says", "twist"])
        return has_story_keywords or has_quotes or is_narrative_length

    def _generate_cinematic_story(self, request: ScriptGenerationRequest, recipe: Dict[str, Any]) -> ShortsScript:
        """Generates a high-retention narrative script preserving story beats, characters, and dialogue."""
        raw_topic = request.topic.strip()
        t_lower = raw_topic.lower()
        total_dur = request.target_duration
        scene_count = 4
        base_dur = round(total_dur / scene_count, 1)

        # Extract all quotes / dialogue
        quotes = re.findall(r'["\']([^"\']{4,})["\']', raw_topic)

        # Bespoke high-retention narrative for "The Lion Who Stole the Moon" / Sheru story
        if "sheru" in t_lower or ("lion" in t_lower and ("moon" in t_lower or "suit" in t_lower)):
            quote_1 = "Ghadi toh waqt batati hai, beta... par apne log waqt nikaalte hain."
            quote_2 = "Ek chaand ke liye jaan nikal gayi... ab taare bhi chahiye!"
            for q in quotes:
                if "ghadi" in q.lower():
                    quote_1 = q
                elif "chaand" in q.lower() or "taare" in q.lower():
                    quote_2 = q

            hinglish = is_hinglish(request.language)
            if hinglish:
                narr = [
                    "Ek akeli bacchi roz raat chaand se baatein karti thi. "
                    "Aaj chaand chhup gaya, wo rone lagi. "
                    "Tabhi ameer sher Sheru bola: main chaand wapas laaunga!",

                    "Sheru ne apni supercar nikaali, drones peeche pade, "
                    "aur wo toofan mein radio tower par chadh gaya.",

                    "Phir crane se baadal cheer diye, chaand chamak utha! "
                    "Par bacchi to papa ka intezaar kar rahi thi.",

                    f"Sheru ne apni watch bacchi ko di: '{quote_1}' "
                    f"Bacchi boli, kal taare bhi laoge? Sheru bola: '{quote_2}'",
                ]
            else:
                narr = [
                    "On a lonely rooftop, a little girl weeps as the moon disappears behind dark clouds. Suddenly, Sheru—a ridiculously wealthy golden lion in a sharp white suit—dramatically promises to bring the moon back.",
                    "Sheru roars off in his golden supercar, blasting through neon streets, dodging police drones, and scaling a massive radio tower in the middle of a thunderstorm.",
                    "In a ridiculous climax, Sheru uses a giant crane to pull the clouds apart, revealing the radiant moon! But the twist? The girl was waiting for her father, who finally returns home.",
                    f"Sheru quietly hands the girl his gold watch: '{quote_1}' When she asks for the stars tomorrow, Sheru groans: '{quote_2}'",
                ]

            scenes = [
                SceneScript(
                    index=0,
                    narration=narr[0],
                    visual_description=f"Sheru the golden lion in a bespoke white three-piece suit standing on a futuristic rooftop beside a crying little girl looking at the dark sky, {recipe['visual_tokens']}, 9:16 vertical framing",
                    camera_movement=f"{recipe['camera_tokens']}, slow continuous push-in",
                    sound_effects=["distant city night wind", "soft emotional piano chime"],
                    estimated_duration_seconds=base_dur
                ),
                SceneScript(
                    index=1,
                    narration=narr[1],
                    visual_description=f"Sheru driving a golden supercar through neon-lit futuristic city streets, pursued by police drones, then climbing a giant steel radio tower during a thunderstorm, {recipe['visual_tokens']}, 9:16 vertical framing",
                    camera_movement=f"{recipe['camera_tokens']}, dynamic high-speed tracking shot",
                    sound_effects=["supercar engine roar", "thunder crack and drone sirens"],
                    estimated_duration_seconds=base_dur
                ),
                SceneScript(
                    index=2,
                    narration=narr[2],
                    visual_description=f"A colossal industrial construction crane parting the dark storm clouds to reveal the glowing full moon, while below the little girl joyfully hugs her returning father, {recipe['visual_tokens']}, 9:16 vertical framing",
                    camera_movement=f"{recipe['camera_tokens']}, dramatic crane pull-back reveal",
                    sound_effects=["heavy crane mechanical strain", "warm orchestral swell"],
                    estimated_duration_seconds=base_dur
                ),
                SceneScript(
                    index=3,
                    narration=narr[3],
                    visual_description=f"Spectacular cinematic wide shot of the glowing city under the full moon, as Sheru the golden lion in his white suit smiles alongside the girl and her father, {recipe['visual_tokens']}, 9:16 vertical framing",
                    camera_movement=f"{recipe['camera_tokens']}, cinematic low-angle tracking shot",
                    sound_effects=["warm acoustic resolve", "gentle city night ambience"],
                    estimated_duration_seconds=round(total_dur - (base_dur * 3), 1)
                ),
            ]
            title = "The Lion Who Stole the Moon"
        else:
            # General narrative extraction
            sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', raw_topic) if s.strip()]
            first_line = raw_topic.split('\n')[0].split('.')[0].strip()
            title = first_line[:50].strip() if len(first_line) > 5 else "A Cinematic Story"

            s0 = sentences[0] if sentences else "Every great adventure begins when an impossible promise is made."
            s1 = sentences[1] if len(sentences) > 1 else "Against impossible odds, an unstoppable journey charges forward into the unknown."
            s2 = sentences[2] if len(sentences) > 2 else "Just when the climax reaches its peak, the real truth turns everything upside down."
            
            quote_line = f" '{quotes[0]}'" if quotes else ""
            s3 = (sentences[3] if len(sentences) > 3 else "And in that unforgettable moment, the true meaning became clear.") + quote_line

            scenes = [
                SceneScript(
                    index=0,
                    narration=s0,
                    visual_description=f"Cinematic opening hook establishing the protagonist and initial conflict, {recipe['visual_tokens']}, 9:16 vertical framing",
                    camera_movement=f"{recipe['camera_tokens']}, slow continuous push-in",
                    sound_effects=recipe["audio_tokens"][:2],
                    estimated_duration_seconds=base_dur
                ),
                SceneScript(
                    index=1,
                    narration=s1,
                    visual_description=f"High-stakes action sequence and escalating journey, {recipe['visual_tokens']}, 9:16 vertical framing",
                    camera_movement=f"{recipe['camera_tokens']}, dynamic high-speed tracking shot",
                    sound_effects=[recipe["audio_tokens"][0]],
                    estimated_duration_seconds=base_dur
                ),
                SceneScript(
                    index=2,
                    narration=s2,
                    visual_description=f"Grand dramatic climax and revelation of the unexpected twist, {recipe['visual_tokens']}, 9:16 vertical framing",
                    camera_movement=f"{recipe['camera_tokens']}, dramatic pull-back reveal",
                    sound_effects=[recipe["audio_tokens"][1] if len(recipe["audio_tokens"]) > 1 else "orchestral swell"],
                    estimated_duration_seconds=base_dur
                ),
                SceneScript(
                    index=3,
                    narration=s3,
                    visual_description=f"Heartwarming emotional resolution and final cinematic wide shot, {recipe['visual_tokens']}, 9:16 vertical framing",
                    camera_movement=f"{recipe['camera_tokens']}, cinematic low-angle tracking shot",
                    sound_effects=[recipe["audio_tokens"][0], "gentle resolve"],
                    estimated_duration_seconds=round(total_dur - (base_dur * 3), 1)
                ),
            ]

        complete_narration = " ".join(s.narration for s in scenes)
        return ShortsScript(
            title=title[:60],
            hook=scenes[0].narration,
            complete_narration=complete_narration,
            target_audience=request.target_audience,
            visual_style=request.visual_style,
            total_estimated_duration=total_dur,
            scenes=scenes
        )

    def _generate_documentary(self, request: ScriptGenerationRequest, recipe: Dict[str, Any]) -> ShortsScript:
        """Generates an educational, scientific, or factual documentary script."""
        raw_topic = request.topic.strip()
        first_line = raw_topic.split('\n')[0].split('.')[0].strip()
        topic_label = first_line[:120].strip() if len(raw_topic) > 120 else raw_topic
        topic_title = topic_label.rstrip(".").capitalize()

        total_dur = request.target_duration
        scene_count = 4
        base_dur = round(total_dur / scene_count, 1)

        scenes = [
            SceneScript(
                index=0,
                narration=f"Did you know that {topic_label.lower()} holds mysteries that continue to surprise modern researchers?",
                visual_description=f"Opening cinematic hook revealing {topic_label.lower()}, {recipe['visual_tokens']}, ultra realistic, 9:16 vertical framing",
                camera_movement=f"{recipe['camera_tokens']}, rapid hook push-in",
                sound_effects=recipe["audio_tokens"][:2],
                estimated_duration_seconds=base_dur
            ),
            SceneScript(
                index=1,
                narration=f"Examining the core dynamics of {topic_label.lower()} reveals extraordinary forces shaping its environment.",
                visual_description=f"Close-up volumetric detail of {topic_label.lower()}, {recipe['visual_tokens']}, dramatic chiaroscuro lighting",
                camera_movement=f"{recipe['camera_tokens']}, continuous slow tracking",
                sound_effects=[recipe["audio_tokens"][0]],
                estimated_duration_seconds=base_dur
            ),
            SceneScript(
                index=2,
                narration=f"Every component operates in perfect precision, demonstrating remarkable natural complexity.",
                visual_description=f"Macro inspection of inner workings and structural adaptations of {topic_label.lower()}, {recipe['visual_tokens']}",
                camera_movement=f"{recipe['camera_tokens']}, rack focus reveal",
                sound_effects=[recipe["audio_tokens"][1] if len(recipe["audio_tokens"]) > 1 else "ambient resonance"],
                estimated_duration_seconds=base_dur
            ),
            SceneScript(
                index=3,
                narration=f"Next time you explore the world around you, remember that the most incredible wonders hide in plain sight.",
                visual_description=f"Wide grand reveal ending shot of {topic_label.lower()}, atmospheric god rays, {recipe['visual_tokens']}",
                camera_movement=f"{recipe['camera_tokens']}, smooth pull-back reveal",
                sound_effects=[recipe["audio_tokens"][0], "reverb fade"],
                estimated_duration_seconds=round(total_dur - (base_dur * 3), 1)
            ),
        ]

        complete_narration = " ".join(s.narration for s in scenes)
        return ShortsScript(
            title=f"The Mystery of {topic_title}"[:60],
            hook=scenes[0].narration,
            complete_narration=complete_narration,
            target_audience=request.target_audience,
            visual_style=request.visual_style,
            total_estimated_duration=total_dur,
            scenes=scenes
        )

    def generate_script(self, request: ScriptGenerationRequest) -> ShortsScript:
        # If the user already provided a full manual script, validate and return it directly
        if request.manual_script:
            return request.manual_script

        recipe = STYLE_RECIPES.get(request.visual_style, STYLE_RECIPES[VisualStyle.CUSTOM])

        # Distinguish between Cinematic Story Mode and Educational Documentary Mode
        if self._is_cinematic_story(request.topic):
            return self._generate_cinematic_story(request, recipe)
        else:
            return self._generate_documentary(request, recipe)


def get_llm_provider(api_key: Optional[str] = None) -> BaseLLMProvider:
    """
    Factory function returning the appropriate LLM provider.
    Prefers GeminiLLMProvider when a valid API key is present; falls back to ManualLLMProvider.
    """
    key = api_key or settings.GEMINI_API_KEY
    if key and not key.startswith("your_") and len(key) > 10:
        try:
            return GeminiLLMProvider(api_key=key)
        except Exception as e:
            logger.warning(f"Could not initialize GeminiLLMProvider ({e}); falling back to ManualLLMProvider.")
            return ManualLLMProvider()
    return ManualLLMProvider()
