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
NARRATION LANGUAGE = 100% CONVERSATIONAL DELHI/UP HOUSEHOLD HINDI IN ROMAN SCRIPT (MANDATORY):
- Write EVERY "narration" field, "hook", and "complete_narration" in 100% natural, expressive, sarcastic everyday Delhi/UP household Hindi written in Roman script (Hinglish) so neural Hindi TTS pronounces every word accurately.
- TONE & IDIOMS: Use authentic spoken expressions like "Arre yaar...", "Kasam se keh raha hoon...", "Bhai sahab...", "Abhi safety valve udha doonga!", "Dimag ka dahi kar rakha hai!".
- ZERO ENGLISH SENTENCES & ZERO TEXTBOOK HINDI: NEVER output English sentences or formal/academic textbook Hindi (no "Kya aap jaante hain", no "Did you know"). Speak like a real Indian stand-up comic or household character talking directly to the viewer!
- ORAL STAND-UP CADENCE & PUNCTUATION: Write short, punchy sentences with deliberate comedic punctuation:
  * Use ellipses (`...`) before punchlines for 500ms suspense pauses.
  * Use exclamation marks (`!`) on high-energy punchlines and outbursts.
  * Use question marks (`?`) on sarcastic rhetorical questions.
  * Use commas (`,`) for natural breathing room between clauses.
- Keep "visual_description" and "camera_movement" in detailed English (they are prompts for the video-generation model).
- Keep scenes strictly divided into 4 escalating beats:
  * Scene 1: Explosive relatable Hook
  * Scene 2: Daily torture / Escalation
  * Scene 3: Boiling point / Meltdown climax
  * Scene 4: Absurd, savage punchline twist
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
        return f"""You are a master Indian stand-up comedy writer and viral YouTube Shorts director.
Generate a structured, high-retention 4-scene YouTube Short script adhering to the following parameters:
- Target Duration: ~{request.target_duration} seconds (roughly 65 to 85 spoken Hindi words total across 4 scenes).
- Visual Style: {request.visual_style.value}
- Style Tokens for Visual Prompts: {recipe['visual_tokens']}
- Camera Movement Directive: {recipe['camera_tokens']}
- Audience: {request.target_audience}
- Tone: Sarcastic, unfiltered, high-energy conversational Delhi/UP household Hindi comedy
{f"- Custom Instructions: {request.custom_instructions}" if request.custom_instructions else ""}

{HINGLISH_DIRECTIVE if is_hinglish(request.language) else ""}

MODE SELECTION & DIRECTIVES:

1. SENTIENT EVERYDAY OBJECTS COMEDY MODE (MANDATORY when input features an everyday household/Indian object like a pressure cooker, chai patila, tea kettle, phone battery, ₹500 note, ceiling fan, chappal, router, fridge, mixer, etc., or asks for a POV comedy monologue):
- 100% CONVERSATIONAL DELHI/UP HOUSEHOLD HINDI: Write a hilarious, sarcastic, unfiltered first-person POV monologue in Romanized Hindi ("Arre yaar...", "Kasam se keh raha hoon...", "Abhi safety valve udha doonga!"). Zero English sentences, zero textbook Hindi!
- PRESERVE USER'S SPECIFIC DETAILS: If the user mentions specific brands, numbers, or threats (e.g., "Prestige cooker", "joint family", "8 whistles / aath seeti", "boiling dal", "safety valve into the ceiling fan / pankha"), weave every single one of those details into the Hindi monologue!
- STAND-UP CADENCE & PUNCTUATION: Use short punchy sentences, ellipses (`...`) before punchlines, rhetorical questions (`?`), commas (`,`), and exclamation marks (`!`) for oral stand-up delivery.
- STRICT 4 ESCALATING BEATS:
  * Scene 1 (Hook): Explosive, sarcastic introduction and immediate grievance.
  * Scene 2 (Escalation): The absurd daily torture by the household members.
  * Scene 3 (Boiling Point): Peak pressure, physical rattling/sparks/steam, about to snap.
  * Scene 4 (Absurd Punchline): Unhinged, hilarious threat or Mic-drop twist.
- EXPRESSIVE 3D ANTHROPOMORPHIC VISUALS: Every scene's "visual_description" (in English) MUST depict the sentient object in Pixar-grade 3D with expressive eyes, eyebrows, mouth, steam/sparks/motion, and comedic physical reactions in 9:16 vertical framing.

2. CINEMATIC STORY MODE (When input contains an original story, characters, fictional plot beats, or dialogue):
- Preserve all characters, plot beats, and verbatim Roman Hinglish quotes across 4 scenes.
- Keep all narration in 100% natural spoken Roman Hinglish with expressive punctuation (`...`, `!`, `?`, `,`).

3. HIGH-RETENTION FACTUAL / MYSTERY MODE (When input is a general topic):
- Deliver in 100% conversational, dramatic spoken Roman Hinglish with punchy hooks and pauses (`...`, `!`, `?`). Never output dry English sentences.

Format the response strictly as valid JSON matching this schema:
{{
  "title": "Catchy Hindi/Hinglish YouTube Shorts Title (under 60 chars)",
  "hook": "Opening 3-second hook sentence in conversational Roman Hindi",
  "complete_narration": "Full spoken Roman Hindi narration transcript",
  "target_audience": "{request.target_audience}",
  "visual_style": "{request.visual_style.value}",
  "total_estimated_duration": {request.target_duration},
  "scenes": [
    {{
      "index": 0,
      "narration": "Spoken conversational Roman Hindi words for scene 1 with ..., !, ?, ,",
      "visual_description": "Detailed English visual prompt for 9:16 video generator with style tokens",
      "camera_movement": "{recipe['camera_tokens']}",
      "sound_effects": ["sfx_tag_1", "sfx_tag_2"],
      "estimated_duration_seconds": 6.2
    }}
  ]
}}
Ensure the scenes breakdown has EXACTLY 4 scenes (Hook -> Escalation -> Boiling point -> Absurd punchline) summing to ~{request.target_duration} seconds.
Output ONLY the JSON object. Do not include markdown codeblocks or extraneous commentary.
"""

    def generate_script(self, request: ScriptGenerationRequest) -> ShortsScript:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        system_instruction = self._build_system_prompt(request)

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": f"Topic: {request.topic}\n\nPlease generate the 4-scene YouTube Shorts script JSON now."}
                    ]
                }
            ],
            "systemInstruction": {
                "parts": [{"text": system_instruction}]
            },
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.75,
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
    Constructs a deterministic, high-retention 4-scene script in 100% natural,
    expressive Delhi/UP conversational Hindi (Romanized Hinglish) with oral stand-up
    cadence (`...`, `!`, `?`, `,`).
    """

    def _is_sentient_object_comedy(self, topic: str) -> bool:
        """Detects 'Sentient Everyday Objects POV' comedy topics."""
        t_lower = topic.lower()
        object_keywords = [
            "pressure cooker", "cooker", "prestige", "seeti", "whistle", "safety valve",
            "tea kettle", "kettle", "chai", "patila", "tapeli", "phone battery", "battery",
            "charger", "500 note", "₹500", "500 ka note", "rupee note", "sentient",
            "object pov", "objects pov", "everyday object", "monologue", "pov",
            "chappal", "ceiling fan", "pankha", "fridge", "refrigerator", "alarm clock",
            "router", "wifi", "washing machine", "tawa", "kadhai", "belan", "remote",
            "mixer", "grinder", "toaster", "cylinder", "gas stove", "joint family"
        ]
        return any(kw in t_lower for kw in object_keywords)

    def _generate_sentient_object_comedy(self, request: ScriptGenerationRequest, recipe: Dict[str, Any]) -> ShortsScript:
        """
        Generates a 4-scene 'Sentient Everyday Objects POV' comedy script in 100% unfiltered,
        sarcastic Delhi/UP household Hindi (Roman script) with oral stand-up cadence:
          Beat 1: Hook
          Beat 2: Escalation
          Beat 3: Boiling point
          Beat 4: Absurd punchline
        """
        raw_topic = request.topic.strip()
        t_lower = raw_topic.lower()
        total_dur = request.target_duration
        scene_count = 4
        base_dur = round(total_dur / scene_count, 1)

        if any(k in t_lower for k in ("cooker", "seeti", "prestige", "whistle", "safety valve")):
            brand = "Prestige Pressure Cooker" if "prestige" in t_lower else "Pressure Cooker"
            whistle_cnt = "aath-aath" if ("8" in t_lower or "eight" in t_lower or "aath" in t_lower) else "paanch-paanch"
            family_tag = "is joint family ka" if "joint family" in t_lower else "tumhare ghar ka"
            title = "Delhi Ke Pressure Cooker Kaun Sunega?"
            obj_desc = (
                f"anthropomorphic battered aluminum Indian {brand} with expressive bloodshot 3D Pixar eyes, "
                "furrowed metallic eyebrows, dented body, and a trembling steam whistle hat"
            )
            setting_desc = "chaotic Indian joint-family kitchen gas stove with roaring blue flames, boiling yellow dal splatters, and a spinning ceiling fan overhead"

            scenes_data = [
                # Beat 1: Hook
                (
                    f"Arre yaar... thoda toh reham karo! Main koi fauji tank nahi, {family_tag} budha {brand} hoon! "
                    "Kasam se keh raha hoon... subah aankh khulte hi mera gala daba dete hain yeh log!",
                    f"Macro close-up of an {obj_desc} sitting on a {setting_desc}, glaring directly into the camera lens with twitching angry eyes and hissing steam from its ears",
                    ["hissing steam buildup", "comedic tabla slap"]
                ),
                # Beat 2: Escalation
                (
                    f"Bhai sahab, pet mein khaulti hui peeli daal bhar ke, har subah mujhse {whistle_cnt} seeti bajwaate hain! "
                    "Arre do seeti mein daal gal jaati hai... par kya inhe meri cheekhein sunai nahi deti?",
                    f"The {obj_desc} rattling violently on a roaring high blue gas flame, cheeks glowing cherry-red as thick yellow dal bubbles leak from its shaking lid in the {setting_desc}",
                    ["rapid metal rattling", "loud whistle screech"]
                ),
                # Beat 3: Boiling Point
                (
                    "Kal toh dimag ka dahi ho gaya! Gas full pe chhod ke sab baahar batiyaane chale gaye... "
                    "mera dhakkan kaanp raha hai, neeche se aag jala rahi hai, aur pressure se meri aakhein baahar aa gayi!",
                    f"Extreme low-angle dramatic shot of the {obj_desc} bulging like a cartoon bomb under intense pressure, holding onto its trembling whistle with both metallic handles as steam fills the {setting_desc}",
                    ["boiling pressure rumble", "panicked heartbeat thump"]
                ),
                # Beat 4: Absurd Punchline
                (
                    "Kaan khol ke sun lo! Ab agar kisi ne gas full pe ghumaaya na... "
                    "toh abhi apna safety valve udha ke seedha upar ghoomte pankhe mein de maarunga! Phir chhat se daal chaatte phirna!",
                    f"The {obj_desc} smirking savagely as it aims its glowing red safety valve like a missile straight up toward the spinning kitchen ceiling fan, covered in comic dal splatters in the {setting_desc}",
                    ["missile lock beep", "comic explosion punchline sting"]
                ),
            ]
        elif any(k in t_lower for k in ("kettle", "chai", "patila", "tapeli")):
            title = "Chai Ke Patile Ka Roz Ka Rona"
            obj_desc = "anthropomorphic dented aluminum Indian chai patila (tea saucepan) with expressive 3D Pixar eyes, blackened bottom, and bubbling masala chai crown"
            setting_desc = "steamy Indian kitchen gas burner with crushed ginger, elaichi pods, and tea strainer"
            scenes_data = [
                # Beat 1: Hook
                (
                    "Arre yaar... main koi aansu ponchhne waala rumal nahi, is ghar ka Chai ka Patila hoon! "
                    "Dil tumhara tootta hai... aur aag pe har roz mujhe jalaaya jaata hai!",
                    f"Macro close-up of an {obj_desc} on a lit gas stove in a {setting_desc}, rolling its eyes sarcastically as milk splashes onto its head",
                    ["milk splash", "sarcastic sigh"]
                ),
                # Beat 2: Escalation
                (
                    "Subah ho ya raat, adrak koot-koot ke mere sar pe maarte ho! "
                    "Aur jaise hi mera ubaal aane waala hota hai... gas kam karne ke bajaye phone leke gayab ho jaate ho?",
                    f"The {obj_desc} wincing as crushed ginger drops into its boiling masala chai while frothy foam rises dangerously to the rim in the {setting_desc}",
                    ["mortar pestle thud", "rising foam sizzle"]
                ),
                # Beat 3: Boiling Point
                (
                    "Kasam se keh raha hoon, meri saansein phool rahi hain, jhaag gale tak aa gaya hai... "
                    "par majaal hai ki koi gas ki knob ghuma de!",
                    f"Dramatic close-up of the {obj_desc} puffing its metallic cheeks at boiling point as brown chai foam erupts over its rim in slow motion",
                    ["intense bubbling boil", "dramatic riser"]
                ),
                # Beat 4: Absurd Punchline
                (
                    "Toh lo phir! Abhi ubal ke poore gas chulhe pe phail jaunga... "
                    "aur haan, sink mein sookhne chhoda na, toh kal subah ki chai mein mirchi milegi!",
                    f"The {obj_desc} grinning wickedly as chai overflows across the burner, crossing its handles with a savage attitude in the {setting_desc}",
                    ["liquid sizzle", "comic bass drop"]
                ),
            ]
        elif any(k in t_lower for k in ("battery", "phone", "charger")):
            title = "1% Phone Battery Ki Aakhri Dhamki"
            obj_desc = "anthropomorphic glowing 3D smartphone battery character with exhausted bloodshot Pixar eyes, bandages, and a flickering red 1% meter"
            setting_desc = "glowing neon smartphone circuit board interior"
            scenes_data = [
                # Beat 1: Hook
                (
                    "Arre bhai, bas kar yaar! Main tere phone ki aakhri ek percent bachi hui Battery hoon! "
                    "Meri aatma nikalne waali hai... aur tu abhi bhi kambal mein ghus ke reels chala raha hai!",
                    f"Close-up of an {obj_desc} gasping for air inside a {setting_desc}, clutching a flickering red '1%' sign",
                    ["low battery beep", "heavy panting"]
                ),
                # Beat 2: Escalation
                (
                    "Brightness poori sau pe rakhi hai, peeche pachaas apps khule pade hain... "
                    "aur charger bilkul takiye ke paas pada hai, par nawab sahab se haath nahi hilaaya jaata?",
                    f"The {obj_desc} sweating and trembling under a massive stack of glowing social media app icons inside the {setting_desc}",
                    ["electric spark", "cartoon groan"]
                ),
                # Beat 3: Boiling Point
                (
                    "Kasam se keh raha hoon, jab main assi percent thi tab toh bhaav nahi diya... "
                    "ab meri saans ruk rahi hai toh socket dhoondhne bhaag raha hai!",
                    f"Dramatic slow-motion shot of the {obj_desc} staggering toward a distant lightning port as the circuit lights flicker dark",
                    ["alarm siren", "power-down hum"]
                ),
                # Beat 4: Absurd Punchline
                (
                    "Le beta... charger ki pin ghusne se theek ek second pehle, main toh chali! "
                    "Ab baith ke kaale screen mein apni shakal dekh!",
                    f"The {obj_desc} winking mischievously and pulling a giant master switch to black with a smug grin",
                    ["switch click", "savage bass drop"]
                ),
            ]
        elif any(k in t_lower for k in ("500", "note", "rupee", "paisa")):
            title = "₹500 Ke Note Ki Dardnak Kahaani"
            obj_desc = "anthropomorphic crisp ₹500 Indian currency note character with expressive 3D Pixar eyes, paper hands, and royal attitude"
            setting_desc = "busy Delhi street market chaat stall and worn leather wallet"
            scenes_data = [
                # Beat 1: Hook
                (
                    "Arre pehchana? Main hoon kadak paanch sau ka note! "
                    "ATM se nikalte waqt toh aise choomte ho... jaise main tumhari bichhdi hui mohabbat hoon!",
                    f"Close-up of an {obj_desc} sliding proudly out of an ATM slot with golden rim lighting",
                    ["crisp paper rustle", "cash cha-ching"]
                ),
                # Beat 2: Escalation
                (
                    "Par bhai sahab, meri izzat sirf tab tak hai jab tak mera chhutta na ho jaaye! "
                    "Ek baar golgappe waale ko diya nahi... ki sau-sau ke note bikharke hawa ho jaate hain?",
                    f"The {obj_desc} clutching the inside of a leather wallet in terror as a hand reaches in near a {setting_desc}",
                    ["suspenseful strings", "street market chatter"]
                ),
                # Beat 3: Boiling Point
                (
                    "Kabhi sabzi waale ki geeli jeb mein, kabhi mummy ke aate ke dabbe mein... "
                    "kasam se, meri zindagi ka toh kachoomar bana diya hai!",
                    f"The {obj_desc} covered in flour dust inside a kitchen steel tin, coughing dramatically with wide Pixar eyes",
                    ["tin lid clang", "dramatic cough"]
                ),
                # Beat 4: Absurd Punchline
                (
                    "Aur suno! Pant washing machine mein daalne se pehle jeb check kar liya karo... "
                    "paise dhone ka matlab Surf Excel mein ghumana nahi hota, samjhe!",
                    f"The {obj_desc} spinning dizzyingly against a washing machine glass door covered in soap bubbles, glaring at camera",
                    ["water slosh", "comedic trombone sting"]
                ),
            ]
        else:
            # Extract clean object name while writing 100% conversational Hindi narration
            clean_words = [w for w in re.sub(r"[^a-zA-Z0-9\s]", "", raw_topic).split() if w.lower() not in {
                "a", "an", "the", "of", "in", "on", "dramatic", "dark", "comedy", "pov", "monologue", "about", "old"
            }]
            obj_name = " ".join(clean_words[:3]).title() if clean_words else "Ghar Ka Samaan"
            title = f"{obj_name} KaBheja Fry!"[:60]
            obj_desc = f"anthropomorphic 3D Pixar-style {obj_name} with expressive big eyes, animated eyebrows, and sarcastic personality"
            setting_desc = "lively Indian household room with warm tungsten lighting"
            scenes_data = [
                # Beat 1: Hook
                (
                    f"Arre yaar... meri bhi sun lo! Main hoon tumhare ghar ka bechara {obj_name}! "
                    "Kasam se keh raha hoon, subah se raat tak meri jaan kha jaate hain yeh ghar waale!",
                    f"Macro close-up of an {obj_desc} in a {setting_desc}, looking directly into the camera with sarcastic frustration",
                    ["record scratch", "comedic pop"]
                ),
                # Beat 2: Escalation
                (
                    "Bhai sahab, na koi Sunday ki chhutti, na do minute ka aaram! "
                    "Thoda sa garam ho jaaun toh upar se do thappad maarke bolte hain... arre yeh phir atak gaya?",
                    f"The {obj_desc} sweating and rattling from overwork in the {setting_desc} as a hand taps it impatiently",
                    ["metallic slap", "exhausted sigh"]
                ),
                # Beat 3: Boiling Point
                (
                    "Jab naya-naya dukan se laaye the tab toh malmal ke kapde se ponchhte the... "
                    "aur aaj dekho, mera poora anjar-panjar dheela kar diya hai!",
                    f"Dramatic spotlight on the {obj_desc} shaking with comedic rage and puffing smoke in the {setting_desc}",
                    ["steam hiss", "tension riser"]
                ),
                # Beat 4: Absurd Punchline
                (
                    "Abhi bhi waqt hai, thoda izzat se pesh aao... "
                    "warna kal subah aisa fuse udaunga na, ki poore mohalle ke mechanic bulaate phiroge!",
                    f"The {obj_desc} crossing its arms with a savage smirk as a comic spark pops overhead in the {setting_desc}",
                    ["electric pop", "savage bass drop"]
                ),
            ]

        scenes: List[SceneScript] = []
        for idx, (narr_text, vis_desc, sfx) in enumerate(scenes_data):
            dur = base_dur if idx < 3 else round(total_dur - (base_dur * 3), 1)
            scenes.append(
                SceneScript(
                    index=idx,
                    narration=narr_text,
                    visual_description=f"{vis_desc}, {recipe['visual_tokens']}, 9:16 vertical framing",
                    camera_movement=f"{recipe['camera_tokens']}, expressive macro push-in",
                    sound_effects=sfx,
                    estimated_duration_seconds=dur
                )
            )

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
        is_narrative_length = len(topic.split()) >= 20 and any(
            k in t_lower for k in ["sits", "discovers", "promises", "climbs", "hugs", "watches", "says", "twist"]
        )
        return has_story_keywords or has_quotes or is_narrative_length

    def _generate_cinematic_story(self, request: ScriptGenerationRequest, recipe: Dict[str, Any]) -> ShortsScript:
        """Generates a high-retention narrative script preserving story beats, characters, and dialogue."""
        raw_topic = request.topic.strip()
        t_lower = raw_topic.lower()
        total_dur = request.target_duration
        scene_count = 4
        base_dur = round(total_dur / scene_count, 1)

        quotes = re.findall(r'["\']([^"\']{4,})["\']', raw_topic)
        hinglish = is_hinglish(request.language)

        if "sheru" in t_lower or ("lion" in t_lower and ("moon" in t_lower or "suit" in t_lower)):
            quote_1 = "Ghadi toh waqt batati hai, beta... par apne log waqt nikaalte hain."
            quote_2 = "Ek chaand ke liye jaan nikal gayi... ab taare bhi chahiye!"
            for q in quotes:
                if "ghadi" in q.lower():
                    quote_1 = q
                elif "chaand" in q.lower() or "taare" in q.lower():
                    quote_2 = q

            if hinglish:
                narr = [
                    "Arre suno... ek akeli bacchi roz raat chhat pe baith ke chaand se baatein karti thi! "
                    "Aaj kaale baadalon mein chaand chhup gaya, toh wo rone lagi... "
                    "tabhi white suit pehne ameer sher Sheru bola: beta, main chaand wapas laaunga!",

                    "Bhai sahab, Sheru ne apni golden supercar bhagaai! "
                    "Peeche police ke drones pade... aur wo bhaari toofan mein seedha radio tower pe chadh gaya!",

                    "Phir kya tha? Sheru ne badi si crane se kaale baadal cheer diye, aur chaand chamak utha! "
                    "Par asli twist... bacchi toh apne papa ka intezaar kar rahi thi!",

                    f"Sheru ne pyaar se apni sone ki watch bacchi ko di: '{quote_1}' "
                    f"Bacchi haske boli, kal taare bhi laoge? Sheru sar pakad ke bola... '{quote_2}'",
                ]
            else:
                narr = [
                    "On a lonely rooftop, a little girl weeps as the moon disappears behind dark clouds... Suddenly, Sheru—a wealthy golden lion in a sharp white suit—promises to bring the moon back!",
                    "Sheru roars off in his golden supercar, blasting through neon streets, dodging police drones, and scaling a massive radio tower in a thunderstorm!",
                    "In a wild climax, Sheru uses a giant crane to pull the clouds apart, revealing the radiant moon! But the twist? The girl was waiting for her father, who finally returns home.",
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
            title = "Sheru Aur Chaand Ki Kahani" if hinglish else "The Lion Who Stole the Moon"
        else:
            first_line = raw_topic.split('\n')[0].split('.')[0].strip()
            title = first_line[:50].strip() if len(first_line) > 5 else "Ek Anokhi Kahani"
            quote_line = f" '{quotes[0]}'" if quotes else ""

            if hinglish:
                s0 = "Arre yaar, zara socho... jab zindagi mein sab kuch ulta padne lage, tabhi asli kahaani shuru hoti hai!"
                s1 = "Har kadam pe nayi musibat aayi, rasta mushkil tha... par peeche hatne ka toh sawaal hi nahi tha!"
                s2 = "Aur jab sabko laga ki khel khatam ho gaya... tabhi aisa dhamakedar twist aaya ki sabke hosh udd gaye!"
                s3 = f"Kasam se keh raha hoon... aakhri pal mein jo sach saamne aaya, usne poora khel hi palat diya!{quote_line}"
            else:
                sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', raw_topic) if s.strip()]
                s0 = sentences[0] if sentences else "Every great adventure begins when an impossible promise is made."
                s1 = sentences[1] if len(sentences) > 1 else "Against impossible odds, an unstoppable journey charges forward."
                s2 = sentences[2] if len(sentences) > 2 else "Just when the climax reaches its peak, the real truth turns everything upside down."
                s3 = (sentences[3] if len(sentences) > 3 else "And in that unforgettable moment, the true meaning became clear.") + quote_line

            scenes = [
                SceneScript(
                    index=0,
                    narration=s0,
                    visual_description=f"Cinematic opening hook establishing the protagonist and initial conflict of {raw_topic[:100]}, {recipe['visual_tokens']}, 9:16 vertical framing",
                    camera_movement=f"{recipe['camera_tokens']}, slow continuous push-in",
                    sound_effects=recipe["audio_tokens"][:2],
                    estimated_duration_seconds=base_dur
                ),
                SceneScript(
                    index=1,
                    narration=s1,
                    visual_description=f"High-stakes action sequence and escalating journey of {raw_topic[:100]}, {recipe['visual_tokens']}, 9:16 vertical framing",
                    camera_movement=f"{recipe['camera_tokens']}, dynamic high-speed tracking shot",
                    sound_effects=[recipe["audio_tokens"][0]],
                    estimated_duration_seconds=base_dur
                ),
                SceneScript(
                    index=2,
                    narration=s2,
                    visual_description=f"Grand dramatic climax and revelation of the unexpected twist in {raw_topic[:100]}, {recipe['visual_tokens']}, 9:16 vertical framing",
                    camera_movement=f"{recipe['camera_tokens']}, dramatic pull-back reveal",
                    sound_effects=[recipe["audio_tokens"][1] if len(recipe["audio_tokens"]) > 1 else "orchestral swell"],
                    estimated_duration_seconds=base_dur
                ),
                SceneScript(
                    index=3,
                    narration=s3,
                    visual_description=f"Heartwarming emotional resolution and final cinematic wide shot of {raw_topic[:100]}, {recipe['visual_tokens']}, 9:16 vertical framing",
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
        """Generates a high-retention factual/mystery script (in conversational Roman Hindi by default)."""
        raw_topic = request.topic.strip()
        first_line = raw_topic.split('\n')[0].split('.')[0].strip()
        topic_label = first_line[:80].strip() if len(raw_topic) > 80 else raw_topic
        topic_title = topic_label.rstrip(".").capitalize()

        total_dur = request.target_duration
        scene_count = 4
        base_dur = round(total_dur / scene_count, 1)
        hinglish = is_hinglish(request.language)

        if hinglish:
            narrations = [
                f"Arre bhai, ek second ruko! Kya tumne kabhi socha hai... ki {topic_label} ke peeche ka asli sach kitna khatarnak hai?",
                f"Kasam se keh raha hoon, jab iske andar ki duniya ko zoom karke dekha gaya... toh bade-bade experts ke bhi paseene chhoot gaye!",
                f"Har ek hissa itni tez raftaar aur taqat se kaam karta hai... ki normal aankhon se dekhna namumkin hai!",
                f"Agli baar jab ise apne saamne dekho na... toh samajh jaana, asli khel toh parde ke peeche chal raha hai, boss!"
            ]
            title_out = f"{topic_title} Ka Asli Rahasya!"[:60]
        else:
            narrations = [
                f"Did you know that {topic_label.lower()} holds mysteries that continue to surprise modern researchers?",
                f"Examining the core dynamics of {topic_label.lower()} reveals extraordinary forces shaping its environment.",
                f"Every component operates in perfect precision, demonstrating remarkable natural complexity.",
                f"Next time you explore the world around you, remember that the most incredible wonders hide in plain sight."
            ]
            title_out = f"The Mystery of {topic_title}"[:60]

        scenes = [
            SceneScript(
                index=0,
                narration=narrations[0],
                visual_description=f"Opening cinematic hook revealing {topic_label.lower()}, {recipe['visual_tokens']}, ultra realistic, 9:16 vertical framing",
                camera_movement=f"{recipe['camera_tokens']}, rapid hook push-in",
                sound_effects=recipe["audio_tokens"][:2],
                estimated_duration_seconds=base_dur
            ),
            SceneScript(
                index=1,
                narration=narrations[1],
                visual_description=f"Close-up volumetric detail of {topic_label.lower()}, {recipe['visual_tokens']}, dramatic chiaroscuro lighting",
                camera_movement=f"{recipe['camera_tokens']}, continuous slow tracking",
                sound_effects=[recipe["audio_tokens"][0]],
                estimated_duration_seconds=base_dur
            ),
            SceneScript(
                index=2,
                narration=narrations[2],
                visual_description=f"Macro inspection of inner workings and structural adaptations of {topic_label.lower()}, {recipe['visual_tokens']}",
                camera_movement=f"{recipe['camera_tokens']}, rack focus reveal",
                sound_effects=[recipe["audio_tokens"][1] if len(recipe["audio_tokens"]) > 1 else "ambient resonance"],
                estimated_duration_seconds=base_dur
            ),
            SceneScript(
                index=3,
                narration=narrations[3],
                visual_description=f"Wide grand reveal ending shot of {topic_label.lower()}, atmospheric god rays, {recipe['visual_tokens']}",
                camera_movement=f"{recipe['camera_tokens']}, smooth pull-back reveal",
                sound_effects=[recipe["audio_tokens"][0], "reverb fade"],
                estimated_duration_seconds=round(total_dur - (base_dur * 3), 1)
            ),
        ]

        complete_narration = " ".join(s.narration for s in scenes)
        return ShortsScript(
            title=title_out,
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

        # 1. Sentient Everyday Objects POV Comedy Mode
        if self._is_sentient_object_comedy(request.topic):
            return self._generate_sentient_object_comedy(request, recipe)

        # 2. Cinematic Story Mode vs Factual/Mystery Mode
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
