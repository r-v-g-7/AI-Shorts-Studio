import re
import time
import json
import base64
import asyncio
import logging
import subprocess
import tempfile
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import numpy as np
import soundfile as sf
from kokoro_onnx import Kokoro

from backend.config import settings
from backend.schemas.job_manifest import WordTimestamp, JobStatus
from backend.services.job_service import JobService
from backend.database.repository import JobRepository

logger = logging.getLogger(__name__)

# Microsoft neural voice names look like "hi-IN-MadhurNeural"; Kokoro voices look like "af_bella".
_EDGE_VOICE_RE = re.compile(r"^[a-z]{2,3}-[A-Za-z]{2,4}-\w+Neural$")
_TICKS_PER_SECOND = 10_000_000  # edge-tts offsets/durations are in 100-nanosecond ticks

# Multilingual Neural Voice & Regional Dialect Registry
VOICE_PROFILES: Dict[str, Dict[str, Any]] = {
    "en-IN": {
        "voice": "en-IN-PrabhatNeural",
        "alt_voice": "en-IN-NeerjaNeural",
        "rate": "+0%",
        "pitch": "+0Hz",
        "lang_code": "en-IN",
        "label": "English (India) - Prabhat",
        "language_name": "English (India)"
    },
    "en-US": {
        "voice": "en-US-AndrewNeural",
        "alt_voice": "en-US-AvaNeural",
        "rate": "+0%",
        "pitch": "+0Hz",
        "lang_code": "en-US",
        "label": "English (US) - Andrew",
        "language_name": "English (US)"
    },
    "hi-IN": {
        "voice": "hi-IN-MadhurNeural",
        "alt_voice": "hi-IN-SwaraNeural",
        "rate": "-5%",
        "pitch": "+0Hz",
        "lang_code": "hi-IN",
        "label": "Hindi (India) - Madhur",
        "language_name": "Hindi"
    },
    "bn-IN": {
        "voice": "bn-IN-BashkarNeural",
        "alt_voice": "bn-IN-TanishaaNeural",
        "rate": "-3%",
        "pitch": "+0Hz",
        "lang_code": "bn-IN",
        "label": "Bengali (India) - Bashkar",
        "language_name": "Bengali"
    },
    "pa-IN": {
        "voice": "hi-IN-MadhurNeural",
        "alt_voice": "hi-IN-SwaraNeural",
        "rate": "+2%",
        "pitch": "+2Hz",
        "lang_code": "hi-IN",
        "label": "Punjabi (Cadence) - Madhur",
        "language_name": "Punjabi"
    },
    "mr-IN": {
        "voice": "mr-IN-ManoharNeural",
        "alt_voice": "mr-IN-AarohiNeural",
        "rate": "-3%",
        "pitch": "+0Hz",
        "lang_code": "mr-IN",
        "label": "Marathi (India) - Manohar",
        "language_name": "Marathi"
    },
    "haryanvi": {
        "voice": "hi-IN-MadhurNeural",
        "alt_voice": "hi-IN-SwaraNeural",
        "rate": "-2%",
        "pitch": "-2Hz",
        "lang_code": "hi-IN",
        "label": "Haryanvi (Deep Cadence) - Madhur",
        "language_name": "Haryanvi"
    },
    "rajasthani": {
        "voice": "hi-IN-MadhurNeural",
        "alt_voice": "hi-IN-SwaraNeural",
        "rate": "-4%",
        "pitch": "+3Hz",
        "lang_code": "hi-IN",
        "label": "Rajasthani (Melodic Cadence) - Madhur",
        "language_name": "Rajasthani"
    },
}

# Default comedic SSML break durations (seconds)
SSML_BREAK_DURATIONS = {
    "...": 0.500,  # <break time="500ms"/>
    "…": 0.500,    # <break time="500ms"/>
    "!": 0.400,    # <break time="400ms"/> + <prosody rate="-3%" pitch="+4Hz">
    "?": 0.350,    # <break time="350ms"/> + rising pitch
    ":": 0.250,    # <break time="250ms"/>
    ",": 0.250,    # <break time="250ms"/>
}


def is_edge_voice(voice: Optional[str]) -> bool:
    return bool(voice and _EDGE_VOICE_RE.match(voice))


class AudioService:
    _kokoro_instance: Optional[Kokoro] = None

    # ------------------------------------------------------------------
    # Multilingual & Dialect Profile Resolution
    # ------------------------------------------------------------------
    @classmethod
    def resolve_language_profile(cls, language: Optional[str] = None, voice_override: Optional[str] = None) -> Dict[str, Any]:
        """Resolves language or dialect into neural voice, rate, pitch, and locale."""
        key = (language or "en-IN").strip()
        k_lower = key.lower().replace("_", "-")
        matched_profile = None
        for p_key, p_val in VOICE_PROFILES.items():
            if k_lower == p_key.lower() or k_lower in p_key.lower() or k_lower == p_val["language_name"].lower():
                matched_profile = p_val.copy()
                break
        if not matched_profile:
            matched_profile = VOICE_PROFILES["en-IN"].copy()

        if voice_override and voice_override.strip():
            matched_profile["voice"] = voice_override.strip()
            v_parts = voice_override.strip().split("-")
            if len(v_parts) >= 2:
                matched_profile["lang_code"] = f"{v_parts[0]}-{v_parts[1]}"
        return matched_profile

    # ------------------------------------------------------------------
    # Provider Detection (ElevenLabs -> SSML edge-tts -> Kokoro)
    # ------------------------------------------------------------------
    @classmethod
    def is_elevenlabs_configured(cls) -> bool:
        """Returns True if ELEVENLABS_API_KEY is present and valid in settings/.env."""
        key = (getattr(settings, "ELEVENLABS_API_KEY", "") or "").strip()
        return bool(key and not key.startswith("your_"))

    # ------------------------------------------------------------------
    # SSML Formatting & Comedic Pauses / Prosody Engine
    # ------------------------------------------------------------------
    @classmethod
    def format_comedic_ssml(
        cls,
        text: str,
        voice: Optional[str] = None,
        rate: str = "-6%",
        lang_code: Optional[str] = None
    ) -> str:
        """
        Converts narrative text and punctuation (!, ?, ..., :, ,) into natural SSML with
        comedic breaks and elevated prosody on exclamations.
        """
        selected_voice = voice or settings.DEFAULT_VOICE
        locale = lang_code
        if not locale:
            parts = selected_voice.split("-")
            locale = f"{parts[0]}-{parts[1]}" if len(parts) >= 2 else "en-US"

        raw = cls.strip_ssml_tags(text).strip()

        # Split into sentence-like clauses keeping terminal punctuation (!, ?, ..., .)
        sentence_parts = re.split(r"(\.\.\.|…|[!?.]+)", raw)
        ssml_clauses: List[str] = []

        idx = 0
        while idx < len(sentence_parts):
            clause = sentence_parts[idx].strip()
            punct = sentence_parts[idx + 1] if (idx + 1) < len(sentence_parts) else ""
            idx += 2

            if not clause and not punct:
                continue

            # Replace internal ellipses, commas, and colons with SSML breaks
            clause_ssml = re.sub(r"\.\.\.|…", '... <break time="500ms"/>', clause)
            clause_ssml = re.sub(r",", ', <break time="250ms"/>', clause_ssml)
            clause_ssml = re.sub(r":", ': <break time="250ms"/>', clause_ssml)

            if "!" in punct:
                ssml_clauses.append(
                    f'<prosody rate="-3%" pitch="+4Hz">{clause_ssml}!</prosody>'
                    f'<break time="400ms"/>'
                )
            elif "?" in punct:
                ssml_clauses.append(
                    f'<prosody rate="-4%" pitch="+6Hz">{clause_ssml}?</prosody>'
                    f'<break time="350ms"/>'
                )
            elif "..." in punct or "…" in punct:
                ssml_clauses.append(f'{clause_ssml}... <break time="500ms"/>')
            elif "." in punct:
                ssml_clauses.append(f'{clause_ssml}. <break time="280ms"/>')
            else:
                ssml_clauses.append(clause_ssml)

        body = " ".join(ssml_clauses)
        return (
            f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="{locale}">'
            f'<voice name="{selected_voice}">'
            f'<prosody rate="{rate}">{body}</prosody>'
            "</voice>"
            "</speak>"
        )

    @staticmethod
    def strip_ssml_tags(text: str) -> str:
        """
        Strips XML/SSML tags (<speak>, <break .../>, <prosody ...>) while preserving
        punctuation so TTS engines that escape XML never read tag names aloud.
        """
        if "<" not in text:
            return text
        # Convert explicit <break time="450ms"/> after words without punctuation into '...'
        cleaned = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", cleaned).strip()

    @classmethod
    def _detect_token_break_and_prosody(cls, word_token: str) -> Tuple[float, float]:
        """
        Inspects a script word token for trailing narrative punctuation and returns
        (target_pause_seconds, prosody_gain_multiplier).
        """
        tok = word_token.strip()
        if tok.endswith("...") or tok.endswith("…"):
            return (SSML_BREAK_DURATIONS["..."], 1.0)
        if tok.endswith("!"):
            return (SSML_BREAK_DURATIONS["!"], 1.12)
        if tok.endswith("?"):
            return (SSML_BREAK_DURATIONS["?"], 1.08)
        if tok.endswith(":"):
            return (SSML_BREAK_DURATIONS[":"], 1.02)
        if tok.endswith(","):
            return (SSML_BREAK_DURATIONS[","], 1.0)
        return (0.0, 1.0)

    @classmethod
    def _apply_ssml_breaks_and_prosody(
        cls,
        samples: np.ndarray,
        sample_rate: int,
        word_timestamps: List[WordTimestamp],
    ) -> Tuple[np.ndarray, List[WordTimestamp]]:
        """
        Enforces exact SSML comedic pause timings and expressive prosody directly on the
        synthesized audio waveform and shifts all subsequent WordTimestamps in lockstep:
          - Ellipsis (`...`) -> >= 450ms pause
          - Exclamation (`!`) -> >= 350ms pause + elevated clause prosody (+1.0 dB presence)
          - Question (`?`) -> >= 350ms pause + elevated clause prosody
          - Colon (`:`) -> >= 250ms pause
          - Comma (`,`) -> >= 200ms pause
        """
        if not word_timestamps or len(samples) == 0:
            return samples, word_timestamps

        audio = samples.astype(np.float32).copy()

        # 1. Apply subtle elevated prosody gain on exclamation/question clauses
        clause_start_idx = 0
        for i, wt in enumerate(word_timestamps):
            pause_sec, prosody_gain = cls._detect_token_break_and_prosody(wt.word)
            is_clause_end = pause_sec > 0 or wt.word.strip().endswith(".")
            if is_clause_end:
                if prosody_gain > 1.0:
                    s_sample = max(0, int(word_timestamps[clause_start_idx].start_time * sample_rate))
                    e_sample = min(len(audio), int(wt.end_time * sample_rate))
                    if e_sample > s_sample:
                        # Smoothly elevate prosody presence with soft-clipping protection
                        boosted = audio[s_sample:e_sample] * prosody_gain
                        audio[s_sample:e_sample] = np.tanh(boosted)
                clause_start_idx = i + 1

        # 2. Splice exact comedic pauses after punctuated words and shift timestamps in lockstep
        segments: List[np.ndarray] = []
        cursor_sample = 0
        cumulative_shift_sec = 0.0
        updated_timestamps: List[WordTimestamp] = []

        for i, wt in enumerate(word_timestamps):
            orig_start = wt.start_time
            orig_end = wt.end_time
            new_start = round(orig_start + cumulative_shift_sec, 3)
            new_end = round(orig_end + cumulative_shift_sec, 3)
            updated_timestamps.append(
                WordTimestamp(word=wt.word, start_time=new_start, end_time=new_end)
            )

            target_pause, _ = cls._detect_token_break_and_prosody(wt.word)
            if target_pause <= 0.0:
                continue

            # Measure existing natural gap before the next spoken word
            if i + 1 < len(word_timestamps):
                existing_gap = max(0.0, word_timestamps[i + 1].start_time - orig_end)
                split_time = (orig_end + word_timestamps[i + 1].start_time) / 2.0
            else:
                existing_gap = 0.0
                split_time = orig_end

            extra_pause = max(0.0, target_pause - existing_gap)
            if extra_pause >= 0.020:
                split_sample = min(len(audio), max(cursor_sample, int(split_time * sample_rate)))
                segments.append(audio[cursor_sample:split_sample])
                silence_samples = int(round(extra_pause * sample_rate))
                segments.append(np.zeros(silence_samples, dtype=np.float32))
                cursor_sample = split_sample
                cumulative_shift_sec += float(silence_samples) / float(sample_rate)

        if cursor_sample < len(audio):
            segments.append(audio[cursor_sample:])

        # Add a natural 120ms tail breath so the final punchline never clips abruptly
        segments.append(np.zeros(int(0.12 * sample_rate), dtype=np.float32))
        final_audio = np.concatenate(segments) if segments else audio
        return final_audio, updated_timestamps

    # ------------------------------------------------------------------
    # ElevenLabs Optional Premium Provider (eleven_multilingual_v2)
    # ------------------------------------------------------------------
    @classmethod
    def _run_elevenlabs(
        cls,
        text: str,
        voice_id: Optional[str] = None,
    ) -> Tuple[np.ndarray, int, List[WordTimestamp], str]:
        """
        Synthesizes expressive human speech via ElevenLabs `eleven_multilingual_v2`
        with stability=0.45 and similarity_boost=0.85.
        """
        api_key = (settings.ELEVENLABS_API_KEY or "").strip()
        if not api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is not configured.")

        clean_text = cls.strip_ssml_tags(text)
        target_voice_id = (
            voice_id
            if (voice_id and not is_edge_voice(voice_id) and not voice_id.startswith(("af_", "am_", "bf_", "bm_")))
            else getattr(settings, "ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")
        )
        model_id = getattr(settings, "ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
        stability = float(getattr(settings, "ELEVENLABS_STABILITY", 0.40))
        similarity_boost = float(getattr(settings, "ELEVENLABS_SIMILARITY_BOOST", 0.88))

        payload = json.dumps({
            "text": clean_text,
            "model_id": model_id,
            "voice_settings": {
                "stability": stability,
                "similarity_boost": similarity_boost,
                "style": 0.35,
                "use_speaker_boost": True
            }
        }).encode("utf-8")

        url_with_ts = f"https://api.elevenlabs.io/v1/text-to-speech/{target_voice_id}/with-timestamps"
        req = urllib.request.Request(
            url_with_ts,
            data=payload,
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        with tempfile.TemporaryDirectory() as tmp:
            mp3_path = Path(tmp) / "elevenlabs.mp3"
            wav_path = Path(tmp) / "elevenlabs.wav"
            alignment_data = None

            try:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    resp_json = json.loads(resp.read().decode("utf-8"))
                audio_b64 = resp_json.get("audio_base64")
                if not audio_b64:
                    raise RuntimeError("ElevenLabs /with-timestamps returned empty audio_base64.")
                mp3_path.write_bytes(base64.b64decode(audio_b64))
                alignment_data = resp_json.get("alignment") or resp_json.get("normalized_alignment")
            except Exception as ts_err:
                logger.info(f"ElevenLabs /with-timestamps fallback to standard stream ({ts_err})...")
                url_std = f"https://api.elevenlabs.io/v1/text-to-speech/{target_voice_id}"
                req_std = urllib.request.Request(
                    url_std,
                    data=payload,
                    headers={
                        "xi-api-key": api_key,
                        "Content-Type": "application/json",
                        "Accept": "audio/mpeg",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req_std, timeout=45) as resp:
                    mp3_path.write_bytes(resp.read())

            cls._mp3_to_wav(mp3_path, wav_path, sample_rate=24000)
            samples, sample_rate = sf.read(str(wav_path), dtype="float32")

        total_duration = float(len(samples)) / float(sample_rate)
        word_timestamps = cls._alignment_to_word_timestamps(clean_text, alignment_data, total_duration)
        timing_source = "elevenlabs-character-alignment" if alignment_data else "elevenlabs-weighted-alignment"
        return samples, sample_rate, word_timestamps, timing_source

    @classmethod
    def _alignment_to_word_timestamps(
        cls,
        text: str,
        alignment: Optional[Dict[str, Any]],
        total_duration: float
    ) -> List[WordTimestamp]:
        """Converts ElevenLabs character alignment arrays into word-level WordTimestamps."""
        tokens = text.split()
        if not tokens:
            return []

        if alignment and alignment.get("characters") and alignment.get("character_start_times_seconds"):
            chars = alignment["characters"]
            starts = alignment["character_start_times_seconds"]
            ends = alignment.get("character_end_times_seconds") or starts
            words_out: List[WordTimestamp] = []
            cur_chars: List[str] = []
            w_start: Optional[float] = None
            w_end: float = 0.0

            for ch, st, en in zip(chars, starts, ends):
                if str(ch).isspace():
                    if cur_chars and w_start is not None:
                        words_out.append(
                            WordTimestamp(
                                word="".join(cur_chars),
                                start_time=round(float(w_start), 3),
                                end_time=round(max(float(w_start) + 0.05, float(w_end)), 3),
                            )
                        )
                        cur_chars = []
                        w_start = None
                else:
                    if w_start is None:
                        w_start = float(st)
                    w_end = float(en)
                    cur_chars.append(str(ch))

            if cur_chars and w_start is not None:
                words_out.append(
                    WordTimestamp(
                        word="".join(cur_chars),
                        start_time=round(float(w_start), 3),
                        end_time=round(min(total_duration, max(float(w_start) + 0.05, float(w_end))), 3),
                    )
                )
            if words_out:
                return words_out

        # Fallback: proportional distribution by token length + punctuation pause weights
        weights = []
        for tok in tokens:
            pause_w, _ = cls._detect_token_break_and_prosody(tok)
            weights.append(max(1.0, float(len(tok)) + pause_w * 12.0))
        total_w = float(sum(weights))
        cursor = 0.0
        out: List[WordTimestamp] = []
        for tok, w in zip(tokens, weights):
            dur = total_duration * (w / total_w)
            out.append(WordTimestamp(word=tok, start_time=round(cursor, 3), end_time=round(cursor + dur, 3)))
            cursor += dur
        return out

    # ------------------------------------------------------------------
    # Kokoro (local, offline, English phonemes)
    # ------------------------------------------------------------------
    @classmethod
    def get_kokoro(cls) -> Kokoro:
        """Lazy-loads Kokoro ONNX model and voice styles on CPU."""
        if cls._kokoro_instance is None:
            model_path = str(settings.KOKORO_MODEL_PATH)
            voices_path = str(settings.KOKORO_VOICES_PATH)

            if not Path(model_path).exists():
                raise FileNotFoundError(f"Kokoro model not found at {model_path}")
            if not Path(voices_path).exists():
                raise FileNotFoundError(f"Voices file not found at {voices_path}")

            logger.info("Initializing Kokoro ONNX CPU engine...")
            cls._kokoro_instance = Kokoro(model_path, voices_path)
            logger.info(f"Kokoro initialized successfully with {len(cls._kokoro_instance.get_voices())} voices.")
        return cls._kokoro_instance

    @classmethod
    def get_available_voices(cls) -> List[str]:
        kokoro = cls.get_kokoro()
        return kokoro.get_voices()

    @classmethod
    def _run_kokoro(
        cls, text: str, voice: str, speed: float
    ) -> Tuple[np.ndarray, int, List[WordTimestamp], str]:
        kokoro = cls.get_kokoro()
        clean_text = cls.strip_ssml_tags(text)

        # Step 1: Pre-process text into discrete words
        raw_words = clean_text.strip().split()
        if not raw_words:
            raise ValueError("No valid words found in narration text.")

        # Step 2: Phonemize word-by-word to preserve exact word boundaries
        phoneme_tokens = []
        for word in raw_words:
            phoneme = kokoro.tokenizer.phonemize(word, lang=settings.KOKORO_LANG)
            phoneme_tokens.append(phoneme)

        joined_phonemes = " ".join(phoneme_tokens)

        # Step 3: Inference via create_timed with is_phonemes=True
        samples, sample_rate, raw_timings = kokoro.create_timed(
            joined_phonemes,
            voice=voice,
            speed=speed,
            is_phonemes=True
        )
        total_audio_duration = float(len(samples)) / float(sample_rate)

        # Step 4: Group raw phoneme timings into word-level boundaries
        word_chunks: List[List[Any]] = []
        current_chunk: List[Any] = []

        for t in raw_timings:
            if t.phoneme == " ":
                if current_chunk:
                    word_chunks.append(current_chunk)
                    current_chunk = []
            else:
                current_chunk.append(t)
        if current_chunk:
            word_chunks.append(current_chunk)

        # Step 5: Construct and normalize word timestamps
        word_timestamps: List[WordTimestamp] = []
        num_aligned = min(len(raw_words), len(word_chunks))

        for i in range(num_aligned):
            chunk = word_chunks[i]
            w_start = round(float(chunk[0].start), 3)
            w_end = round(float(chunk[-1].end), 3)

            if w_end <= w_start:
                w_end = round(w_start + 0.1, 3)

            w_end = min(w_end, round(total_audio_duration, 3))
            w_start = min(w_start, w_end)

            word_timestamps.append(WordTimestamp(word=raw_words[i], start_time=w_start, end_time=w_end))

        # Fill any tail words if phoneme chunk mismatch occurred
        if len(raw_words) > len(word_chunks):
            last_end = word_timestamps[-1].end_time if word_timestamps else 0.0
            remaining_time = max(0.2, total_audio_duration - last_end)
            step = remaining_time / (len(raw_words) - len(word_chunks))
            for i in range(len(word_chunks), len(raw_words)):
                start = round(last_end, 3)
                end = round(min(total_audio_duration, last_end + step), 3)
                word_timestamps.append(WordTimestamp(word=raw_words[i], start_time=start, end_time=end))
                last_end = end

        return samples, sample_rate, word_timestamps, "kokoro-phoneme-timing"

    # ------------------------------------------------------------------
    # edge-tts (Microsoft neural voices, native Hindi, SSML breaks & -6% rate)
    # ------------------------------------------------------------------
    @staticmethod
    def _speed_to_rate(speed: Optional[float] = None) -> str:
        """
        Converts numeric speed to edge-tts rate string.
        Defaults to '-6%' (deliberate, humanized comedic cadence) when speed is None, 1.0, 0.95, or 0.94.
        """
        if speed is None or abs(float(speed) - 1.0) < 1e-3 or abs(float(speed) - 0.95) < 1e-3 or abs(float(speed) - 0.94) < 1e-3:
            return getattr(settings, "TTS_DEFAULT_RATE", "-6%") or "-6%"
        pct = int(round((float(speed) - 1.0) * 100))
        return f"{pct:+d}%"

    @staticmethod
    async def _edge_stream(
        text: str,
        voice: str,
        rate: str,
        mp3_path: Path,
        pitch: str = "+0Hz",
        volume: str = "+0%",
    ) -> List[Dict[str, Any]]:
        import edge_tts

        # edge-tts >= 7 defaults to SentenceBoundary; word-level timing must be requested explicitly.
        communicate = edge_tts.Communicate(
            text, voice, rate=rate, pitch=pitch, volume=volume, boundary="WordBoundary"
        )
        events: List[Dict[str, Any]] = []
        audio_bytes = 0
        with open(mp3_path, "wb") as f:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                    audio_bytes += len(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    events.append(chunk)
        if audio_bytes == 0:
            raise RuntimeError(f"edge-tts returned no audio for voice '{voice}'.")
        return events

    @classmethod
    def _edge_stream_blocking(
        cls,
        text: str,
        voice: str,
        rate: str,
        mp3_path: Path,
        pitch: str = "+0Hz",
        volume: str = "+0%",
    ) -> List[Dict[str, Any]]:
        """Runs the async edge-tts stream on a private event loop (safe from any calling thread)."""
        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                with ThreadPoolExecutor(max_workers=1) as ex:
                    return ex.submit(
                        asyncio.run, cls._edge_stream(text, voice, rate, mp3_path, pitch=pitch, volume=volume)
                    ).result()
            except Exception as e:  # network hiccups, throttling, websocket drops
                last_err = e
                logger.warning(f"edge-tts attempt {attempt}/3 failed: {e}")
                time.sleep(1.5 * attempt)
        raise RuntimeError(f"edge-tts synthesis failed after 3 attempts: {last_err}")

    @staticmethod
    def _mp3_to_wav(mp3_path: Path, wav_path: Path, sample_rate: int = 24000) -> None:
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3_path),
            "-ar", str(sample_rate), "-ac", "1", "-c:a", "pcm_s16le", str(wav_path)
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"FFmpeg MP3->WAV conversion failed: {res.stderr.strip()}")

    @staticmethod
    def _normalize_token(s: str) -> str:
        return re.sub(r"[^\w]", "", s, flags=re.UNICODE).lower()

    @classmethod
    def _events_to_word_timestamps(
        cls, text: str, events: List[Dict[str, Any]], total_duration: float
    ) -> Tuple[List[WordTimestamp], str]:
        """
        Converts edge-tts WordBoundary events into normalized WordTimestamps.
        Preserves the original script tokens (with punctuation) when the counts line up;
        otherwise maps spoken tokens back to punctuated script tokens so SSML breaks
        trigger on exact punctuation boundaries.
        """
        tokens = text.split()
        spoken = [e for e in events if str(e.get("text", "")).strip()]

        if not spoken:
            weights = [max(1, len(t)) for t in tokens]
            total_w = float(sum(weights))
            cursor = 0.0
            out: List[WordTimestamp] = []
            for tok, w in zip(tokens, weights):
                dur = total_duration * (w / total_w)
                out.append(WordTimestamp(word=tok, start_time=round(cursor, 3), end_time=round(cursor + dur, 3)))
                cursor += dur
            return out, "estimated-by-character-weight"

        if len(spoken) == len(tokens):
            labels = tokens
            source = "edge-tts-ssml-WordBoundary"
        else:
            # Align spoken tokens to punctuated script tokens so punctuation (!, ?, ..., ,) is preserved
            labels = []
            tok_cursor = 0
            for e in spoken:
                sp_word = str(e["text"]).strip()
                sp_norm = cls._normalize_token(sp_word)
                matched_label = sp_word
                for lookahead in range(tok_cursor, min(len(tokens), tok_cursor + 3)):
                    cand_norm = cls._normalize_token(tokens[lookahead])
                    if cand_norm and (cand_norm == sp_norm or sp_norm in cand_norm or cand_norm in sp_norm):
                        matched_label = tokens[lookahead]
                        tok_cursor = lookahead + 1
                        break
                labels.append(matched_label)
            source = "edge-tts-ssml-WordBoundary(aligned)"
            logger.info(f"edge-tts returned {len(spoken)} boundaries for {len(tokens)} script tokens; aligned punctuation.")

        out = []
        prev_end = 0.0
        for label, ev in zip(labels, spoken):
            start = float(ev["offset"]) / _TICKS_PER_SECOND
            end = start + float(ev["duration"]) / _TICKS_PER_SECOND
            start = max(start, prev_end)  # strictly non-overlapping, monotonic
            end = max(end, start + 0.05)
            end = min(end, total_duration)
            start = min(start, end)
            out.append(WordTimestamp(word=label, start_time=round(start, 3), end_time=round(end, 3)))
            prev_end = out[-1].end_time
        return out, source

    @classmethod
    def _run_edge(
        cls, text: str, voice: str, speed: Optional[float] = None
    ) -> Tuple[np.ndarray, int, List[WordTimestamp], str, str]:
        rate = cls._speed_to_rate(speed)
        clean_text = cls.strip_ssml_tags(text)
        ssml_markup = cls.format_comedic_ssml(clean_text, voice=voice, rate=rate)

        # Split into sentence clauses to apply authentic neural pitch/rate prosody contours
        raw_parts = re.split(r"([!?.]+)", clean_text)
        clauses: List[Tuple[str, str, str, str]] = []  # (clause_text_with_punct, rate, pitch, volume)
        idx = 0
        while idx < len(raw_parts):
            seg = raw_parts[idx].strip()
            punct = raw_parts[idx + 1] if (idx + 1) < len(raw_parts) else ""
            idx += 2
            if not seg:
                continue
            full_clause = f"{seg}{punct}".strip()
            if "!" in punct:
                clauses.append((full_clause, "-3%", "+4Hz", "+6%"))
            elif "?" in punct:
                clauses.append((full_clause, "-4%", "+6Hz", "+4%"))
            else:
                clauses.append((full_clause, rate, "+0Hz", "+0%"))

        if len(clauses) > 1 and len(clauses) <= 12:
            try:
                all_samples: List[np.ndarray] = []
                all_timestamps: List[WordTimestamp] = []
                sample_rate = 24000
                time_offset = 0.0

                with tempfile.TemporaryDirectory() as tmp:
                    for c_idx, (c_text, c_rate, c_pitch, c_vol) in enumerate(clauses):
                        mp3_p = Path(tmp) / f"clause_{c_idx}.mp3"
                        wav_p = Path(tmp) / f"clause_{c_idx}.wav"
                        ev = cls._edge_stream_blocking(
                            c_text, voice, c_rate, mp3_p, pitch=c_pitch, volume=c_vol
                        )
                        cls._mp3_to_wav(mp3_p, wav_p, sample_rate=sample_rate)
                        c_samples, sample_rate = sf.read(str(wav_p), dtype="float32")
                        c_dur = float(len(c_samples)) / float(sample_rate)
                        c_ts, _ = cls._events_to_word_timestamps(c_text, ev, c_dur)

                        # Apply internal comma/ellipsis pauses (250ms / 500ms) within the clause
                        c_samples, c_ts = cls._apply_ssml_breaks_and_prosody(c_samples, sample_rate, c_ts)
                        c_dur = float(len(c_samples)) / float(sample_rate)

                        for wt in c_ts:
                            all_timestamps.append(
                                WordTimestamp(
                                    word=wt.word,
                                    start_time=round(wt.start_time + time_offset, 3),
                                    end_time=round(wt.end_time + time_offset, 3),
                                )
                            )
                        all_samples.append(c_samples)
                        time_offset += c_dur

                merged_samples = np.concatenate(all_samples) if all_samples else np.zeros(2400, dtype=np.float32)
                return merged_samples, sample_rate, all_timestamps, "edge-tts-ssml-prosody-clauses", ssml_markup
            except Exception as clause_err:
                logger.warning(f"Clause-level prosody synthesis fallback to unified stream ({clause_err})")

        with tempfile.TemporaryDirectory() as tmp:
            mp3_path = Path(tmp) / "edge.mp3"
            wav_tmp = Path(tmp) / "edge.wav"
            events = cls._edge_stream_blocking(clean_text, voice, rate, mp3_path)
            cls._mp3_to_wav(mp3_path, wav_tmp)
            samples, sample_rate = sf.read(str(wav_tmp), dtype="float32")

        raw_duration = float(len(samples)) / float(sample_rate)
        raw_timestamps, source = cls._events_to_word_timestamps(clean_text, events, raw_duration)

        # Apply SSML comedic pauses (... -> 500ms, ! -> 400ms + prosody, ? -> 350ms, , -> 250ms) and sync timestamps
        samples, timestamps = cls._apply_ssml_breaks_and_prosody(samples, sample_rate, raw_timestamps)
        return samples, sample_rate, timestamps, source, ssml_markup

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @classmethod
    def synthesize_speech(
        cls,
        text: str,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        job_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Synthesizes human comedic narration with exact word-level timestamps:
        1. If ELEVENLABS_API_KEY is present in .env -> ElevenLabs (`eleven_multilingual_v2`,
           stability=0.45, similarity_boost=0.85).
        2. Otherwise -> SSML-enhanced edge-tts (`hi-IN-MadhurNeural` / `hi-IN-SwaraNeural`)
           with automatic comedic pauses (`...` -> 450ms, `!` -> 350ms + elevated prosody,
           `,` -> 200ms) and `-5%` default speech rate.
        3. Local Kokoro fallback only for explicit English Kokoro voices (`af_bella`, `am_adam`).
        """
        if not text or not text.strip():
            raise ValueError("Narration text cannot be empty.")
        text = text.strip()

        selected_voice = voice or settings.DEFAULT_VOICE
        start_time = time.time()
        ssml_markup: Optional[str] = None
        selected_rate = cls._speed_to_rate(speed)
        selected_speed = float(speed if speed is not None else settings.TTS_SPEED)

        # Priority 1: Optional Premium ElevenLabs Provider (if ELEVENLABS_API_KEY is configured in .env)
        if cls.is_elevenlabs_configured():
            try:
                logger.info("ELEVENLABS_API_KEY detected: synthesizing with ElevenLabs (eleven_multilingual_v2)...")
                samples, sample_rate, word_timestamps, timing_source = cls._run_elevenlabs(
                    text, voice_id=voice
                )
                samples, word_timestamps = cls._apply_ssml_breaks_and_prosody(
                    samples, sample_rate, word_timestamps
                )
                engine = f"elevenlabs ({settings.ELEVENLABS_MODEL_ID})"
                selected_voice = getattr(settings, "ELEVENLABS_VOICE_ID", selected_voice)
                ssml_markup = cls.format_comedic_ssml(text, voice=selected_voice, rate=selected_rate)
            except Exception as el_err:
                logger.warning(f"ElevenLabs synthesis failed ({el_err}); falling back to SSML-enhanced edge-tts.")
                samples, sample_rate, word_timestamps, timing_source, ssml_markup = cls._run_edge(
                    text, selected_voice if is_edge_voice(selected_voice) else settings.DEFAULT_VOICE, selected_speed
                )
                engine = "edge-tts-ssml"
                selected_voice = selected_voice if is_edge_voice(selected_voice) else settings.DEFAULT_VOICE
        elif is_edge_voice(selected_voice):
            # Priority 2: SSML-enhanced edge-tts with -5% default rate and comedic breaks
            try:
                samples, sample_rate, word_timestamps, timing_source, ssml_markup = cls._run_edge(
                    text, selected_voice, selected_speed
                )
                engine = "edge-tts-ssml"
            except Exception as e:
                if not settings.TTS_ALLOW_KOKORO_FALLBACK:
                    raise RuntimeError(f"edge-tts voice '{selected_voice}' failed: {e}") from e
                logger.warning(f"edge-tts failed ({e}); falling back to Kokoro (English phonemes!).")
                engine, selected_voice = "kokoro (fallback)", settings.KOKORO_VOICE
                selected_speed = float(settings.KOKORO_SPEED)
                samples, sample_rate, word_timestamps, timing_source = cls._run_kokoro(
                    text, selected_voice, selected_speed
                )
        else:
            # Priority 3: Local English Kokoro ONNX voice
            engine = "kokoro"
            selected_speed = float(speed or settings.KOKORO_SPEED)
            samples, sample_rate, word_timestamps, timing_source = cls._run_kokoro(
                text, selected_voice, selected_speed
            )

        inference_duration = time.time() - start_time
        total_audio_duration = float(len(samples)) / float(sample_rate)

        # Determine output paths and save artifacts
        if job_id:
            job_dir = JobService.get_job_directory(job_id)
            wav_path = job_dir / "audio" / "narration.wav"
            json_path = job_dir / "audio" / "words.json"
        else:
            temp_dir = settings.STORAGE_DIR / "temp"
            temp_dir.mkdir(parents=True, exist_ok=True)
            wav_path = temp_dir / f"speech_{int(time.time())}.wav"
            json_path = temp_dir / f"words_{int(time.time())}.json"
        wav_path.parent.mkdir(parents=True, exist_ok=True)

        sf.write(str(wav_path), samples, sample_rate, subtype="PCM_16")

        timestamp_dicts = [w.model_dump() for w in word_timestamps]
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "job_id": job_id,
                "engine": engine,
                "voice": selected_voice,
                "speed": selected_speed,
                "rate": selected_rate,
                "ssml": ssml_markup,
                "timing_source": timing_source,
                "sample_rate": sample_rate,
                "total_duration_seconds": round(total_audio_duration, 3),
                "word_count": len(word_timestamps),
                "timestamps": timestamp_dicts
            }, f, indent=2, ensure_ascii=False)

        # Update Job Manifest & DB if job_id provided
        if job_id:
            manifest = JobService.load_manifest(job_id)
            if manifest:
                manifest.audio_file = str(wav_path)
                manifest.word_timestamps = word_timestamps
                manifest.render_metadata["total_duration_seconds"] = round(total_audio_duration, 3)
                manifest.render_metadata["audio_sample_rate"] = sample_rate
                manifest.render_metadata["voice"] = selected_voice
                manifest.render_metadata["tts_engine"] = engine
                manifest.render_metadata["tts_rate"] = selected_rate
                manifest.render_metadata["word_timing_source"] = timing_source
                manifest.status = JobStatus.AUDIO_READY
                JobService.save_manifest(manifest)

            JobRepository.update_job_status(job_id, JobStatus.AUDIO_READY.value)
            JobRepository.add_log(
                job_id,
                "INFO",
                "AUDIO_SYNTHESIS",
                f"Generated narration WAV ({total_audio_duration:.2f}s, {len(word_timestamps)} words) in "
                f"{inference_duration:.2f}s using {engine} voice '{selected_voice}' (rate={selected_rate}) [{timing_source}]"
            )

        return {
            "audio_path": str(wav_path),
            "timestamps_path": str(json_path),
            "duration_seconds": round(total_audio_duration, 3),
            "sample_rate": sample_rate,
            "engine": engine,
            "voice": selected_voice,
            "speed": selected_speed,
            "rate": selected_rate,
            "ssml": ssml_markup,
            "timing_source": timing_source,
            "word_count": len(word_timestamps),
            "inference_time_seconds": round(inference_duration, 3),
            "timestamps": word_timestamps
        }

    # ------------------------------------------------------------------
    # Multilingual Timeline Budgeting & Direct Scene Audio Engine
    # ------------------------------------------------------------------
    @classmethod
    def synthesize_direct_scenes(
        cls,
        scenes: List[Any],
        language: str = "en-IN",
        voice: Optional[str] = None,
        target_total_duration: float = 60.0,
        job_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Synthesizes speech per scene with multilingual neural TTS and timeline budgeting:
        - Resolves language / dialect profile (English, Hindi, Bengali, Punjabi, Marathi, Haryanvi, Rajasthani).
        - Computes target per-scene intervals (e.g. 60s / 4 scenes = 15.0s per scene).
        - Pads or subtle-scales each scene audio so transitions land exactly on clean 12-15s intervals.
        - Synchronizes scene.duration_seconds and global WordTimestamps in lockstep.
        - Concatenates into master audio/narration.wav and saves audio/words.json.
        """
        if not scenes:
            raise ValueError("No scenes provided for audio synthesis.")

        start_time = time.time()
        profile = cls.resolve_language_profile(language, voice_override=voice)
        selected_voice = profile["voice"]
        rate = profile["rate"]
        pitch = profile["pitch"]
        lang_code = profile["lang_code"]

        num_scenes = len(scenes)
        target_scene_dur = round(target_total_duration / max(1, num_scenes), 2)
        sample_rate = 24000

        all_scene_samples: List[np.ndarray] = []
        global_word_timestamps: List[WordTimestamp] = []
        cumulative_time_offset = 0.0

        job_dir = JobService.get_job_directory(job_id) if job_id else (settings.STORAGE_DIR / "temp")
        audio_dir = job_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)

        for s_idx, scene in enumerate(scenes):
            raw_text = getattr(scene, "narration", "") if hasattr(scene, "narration") else scene.get("narration", "")
            clean_text = cls.strip_ssml_tags(raw_text).strip()
            if not clean_text:
                continue

            scene_num = s_idx + 1
            with tempfile.TemporaryDirectory() as tmp:
                mp3_p = Path(tmp) / f"scene_{scene_num:02d}.mp3"
                wav_p = Path(tmp) / f"scene_{scene_num:02d}.wav"

                if is_edge_voice(selected_voice):
                    evs = cls._edge_stream_blocking(clean_text, selected_voice, rate, mp3_p, pitch=pitch)
                    cls._mp3_to_wav(mp3_p, wav_p, sample_rate=sample_rate)
                    s_samples, sample_rate = sf.read(str(wav_p), dtype="float32")
                    raw_dur = float(len(s_samples)) / float(sample_rate)
                    raw_ts, _ = cls._events_to_word_timestamps(clean_text, evs, raw_dur)
                    s_samples, s_ts = cls._apply_ssml_breaks_and_prosody(s_samples, sample_rate, raw_ts)
                else:
                    s_samples, sample_rate, s_ts, _ = cls._run_kokoro(clean_text, selected_voice, speed=1.0)

            actual_dur = float(len(s_samples)) / float(sample_rate)

            # Timeline Budgeting:
            # If scene is shorter than target interval (e.g. 13s < 15s), pad with natural silence
            # so each scene transitions cleanly on exact 15-second intervals.
            if actual_dur < target_scene_dur:
                pad_sec = target_scene_dur - actual_dur
                pad_samples = np.zeros(int(round(pad_sec * sample_rate)), dtype=np.float32)
                s_samples = np.concatenate([s_samples, pad_samples])
                final_scene_dur = target_scene_dur
            else:
                tail_pad = np.zeros(int(round(0.20 * sample_rate)), dtype=np.float32)
                s_samples = np.concatenate([s_samples, tail_pad])
                final_scene_dur = float(len(s_samples)) / float(sample_rate)

            # Shift scene word timestamps to global master timeline
            for wt in s_ts:
                global_word_timestamps.append(
                    WordTimestamp(
                        word=wt.word,
                        start_time=round(wt.start_time + cumulative_time_offset, 3),
                        end_time=round(min(wt.end_time + cumulative_time_offset, cumulative_time_offset + final_scene_dur), 3)
                    )
                )

            # Save individual scene WAV
            scene_wav = audio_dir / f"scene_{scene_num:02d}.wav"
            sf.write(str(scene_wav), s_samples, sample_rate, subtype="PCM_16")

            # Update scene duration in object/dict
            if hasattr(scene, "duration_seconds"):
                scene.duration_seconds = round(final_scene_dur, 3)
            elif isinstance(scene, dict):
                scene["duration_seconds"] = round(final_scene_dur, 3)

            all_scene_samples.append(s_samples)
            cumulative_time_offset += final_scene_dur

        master_samples = np.concatenate(all_scene_samples) if all_scene_samples else np.zeros(24000, dtype=np.float32)
        total_audio_dur = float(len(master_samples)) / float(sample_rate)

        master_wav_path = audio_dir / "narration.wav"
        words_json_path = audio_dir / "words.json"
        sf.write(str(master_wav_path), master_samples, sample_rate, subtype="PCM_16")

        timestamp_dicts = [w.model_dump() for w in global_word_timestamps]
        with open(words_json_path, "w", encoding="utf-8") as f:
            json.dump({
                "job_id": job_id,
                "engine": "edge-tts-multilingual",
                "voice": selected_voice,
                "language": language,
                "lang_code": lang_code,
                "target_total_duration": target_total_duration,
                "actual_duration_seconds": round(total_audio_dur, 3),
                "scene_count": len(scenes),
                "timestamps": timestamp_dicts
            }, f, indent=2, ensure_ascii=False)

        # Update Job Manifest & DB if job_id provided
        if job_id:
            manifest = JobService.load_manifest(job_id)
            if manifest:
                manifest.audio_file = str(master_wav_path)
                manifest.word_timestamps = global_word_timestamps
                manifest.render_metadata["total_duration_seconds"] = round(total_audio_dur, 3)
                manifest.render_metadata["audio_sample_rate"] = sample_rate
                manifest.render_metadata["voice"] = selected_voice
                manifest.render_metadata["language"] = language
                manifest.render_metadata["tts_engine"] = "edge-tts-multilingual"
                manifest.status = JobStatus.AUDIO_READY
                manifest.scenes = scenes
                JobService.save_manifest(manifest)
                JobService.set_scenes(job_id, scenes)

            JobRepository.update_job_status(job_id, JobStatus.AUDIO_READY.value)
            JobRepository.add_log(
                job_id,
                "INFO",
                "AUDIO_SYNTHESIS",
                f"Generated 60s Multilingual Speech ({total_audio_dur:.2f}s, {len(scenes)} scenes, {len(global_word_timestamps)} words) "
                f"using voice '{selected_voice}' ({profile['label']})."
            )

        return {
            "audio_path": str(master_wav_path),
            "timestamps_path": str(words_json_path),
            "duration_seconds": round(total_audio_dur, 3),
            "sample_rate": sample_rate,
            "engine": "edge-tts-multilingual",
            "voice": selected_voice,
            "language": language,
            "timestamps": global_word_timestamps
        }
