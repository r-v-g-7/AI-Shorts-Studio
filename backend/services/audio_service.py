import re
import time
import json
import asyncio
import logging
import subprocess
import tempfile
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


def is_edge_voice(voice: Optional[str]) -> bool:
    return bool(voice and _EDGE_VOICE_RE.match(voice))


class AudioService:
    _kokoro_instance: Optional[Kokoro] = None

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

        # Step 1: Pre-process text into discrete words
        raw_words = text.strip().split()
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
    # edge-tts (Microsoft neural voices, native Hindi, word boundaries)
    # ------------------------------------------------------------------
    @staticmethod
    def _speed_to_rate(speed: float) -> str:
        pct = int(round((float(speed) - 1.0) * 100))
        return f"{pct:+d}%"

    @staticmethod
    async def _edge_stream(text: str, voice: str, rate: str, mp3_path: Path) -> List[Dict[str, Any]]:
        import edge_tts

        # edge-tts >= 7 defaults to SentenceBoundary; word-level timing must be requested explicitly.
        communicate = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
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
    def _edge_stream_blocking(cls, text: str, voice: str, rate: str, mp3_path: Path) -> List[Dict[str, Any]]:
        """Runs the async edge-tts stream on a private event loop (safe from any calling thread)."""
        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                with ThreadPoolExecutor(max_workers=1) as ex:
                    return ex.submit(asyncio.run, cls._edge_stream(text, voice, rate, mp3_path)).result()
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
        otherwise uses the spoken tokens reported by the service.
        """
        tokens = text.split()
        spoken = [e for e in events if str(e.get("text", "")).strip()]

        if not spoken:
            # Service returned audio but no boundaries: distribute by character weight (flagged as estimated).
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
            source = "edge-tts-WordBoundary"
        else:
            labels = [str(e["text"]).strip() for e in spoken]
            source = "edge-tts-WordBoundary(spoken-tokens)"
            logger.info(f"edge-tts returned {len(spoken)} boundaries for {len(tokens)} script tokens; using spoken tokens.")

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
        cls, text: str, voice: str, speed: float
    ) -> Tuple[np.ndarray, int, List[WordTimestamp], str]:
        rate = cls._speed_to_rate(speed)
        with tempfile.TemporaryDirectory() as tmp:
            mp3_path = Path(tmp) / "edge.mp3"
            wav_tmp = Path(tmp) / "edge.wav"
            events = cls._edge_stream_blocking(text, voice, rate, mp3_path)
            cls._mp3_to_wav(mp3_path, wav_tmp)
            samples, sample_rate = sf.read(str(wav_tmp), dtype="float32")
        total_duration = float(len(samples)) / float(sample_rate)
        timestamps, source = cls._events_to_word_timestamps(text, events, total_duration)
        return samples, sample_rate, timestamps, source

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
        Synthesizes narration with exact word-level timestamps.
        - Neural voices (e.g. hi-IN-MadhurNeural) -> edge-tts, native Hindi/Hinglish pronunciation.
        - Kokoro voices (e.g. af_bella) -> local CPU ONNX Runtime (0 MB VRAM), English.
        """
        if not text or not text.strip():
            raise ValueError("Narration text cannot be empty.")
        text = text.strip()

        selected_voice = voice or settings.DEFAULT_VOICE
        start_time = time.time()
        engine = "edge-tts" if is_edge_voice(selected_voice) else "kokoro"

        if engine == "edge-tts":
            selected_speed = float(speed or settings.TTS_SPEED)
            try:
                samples, sample_rate, word_timestamps, timing_source = cls._run_edge(text, selected_voice, selected_speed)
            except Exception as e:
                if not settings.TTS_ALLOW_KOKORO_FALLBACK:
                    raise RuntimeError(f"edge-tts voice '{selected_voice}' failed: {e}") from e
                logger.warning(f"edge-tts failed ({e}); falling back to Kokoro (English phonemes!).")
                engine, selected_voice = "kokoro (fallback)", settings.KOKORO_VOICE
                selected_speed = float(settings.KOKORO_SPEED)
                samples, sample_rate, word_timestamps, timing_source = cls._run_kokoro(text, selected_voice, selected_speed)
        else:
            selected_speed = float(speed or settings.KOKORO_SPEED)
            samples, sample_rate, word_timestamps, timing_source = cls._run_kokoro(text, selected_voice, selected_speed)

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
                manifest.render_metadata["word_timing_source"] = timing_source
                manifest.status = JobStatus.AUDIO_READY
                JobService.save_manifest(manifest)

            JobRepository.update_job_status(job_id, JobStatus.AUDIO_READY.value)
            JobRepository.add_log(
                job_id,
                "INFO",
                "AUDIO_SYNTHESIS",
                f"Generated narration WAV ({total_audio_duration:.2f}s, {len(word_timestamps)} words) in "
                f"{inference_duration:.2f}s using {engine} voice '{selected_voice}' [{timing_source}]"
            )

        return {
            "audio_path": str(wav_path),
            "timestamps_path": str(json_path),
            "duration_seconds": round(total_audio_duration, 3),
            "sample_rate": sample_rate,
            "engine": engine,
            "voice": selected_voice,
            "speed": selected_speed,
            "timing_source": timing_source,
            "word_count": len(word_timestamps),
            "inference_time_seconds": round(inference_duration, 3),
            "timestamps": word_timestamps
        }
