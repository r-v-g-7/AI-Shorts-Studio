import math
from pathlib import Path
from typing import List, Dict, Any, Optional

from backend.config import settings
from backend.schemas.job_manifest import WordTimestamp, JobStatus
from backend.services.job_service import JobService
from backend.database.repository import JobRepository


def seconds_to_ass_time(seconds: float) -> str:
    """Converts seconds float to ASS timestamp format: H:MM:SS.cs"""
    if seconds < 0:
        seconds = 0.0
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis >= 100:
        secs += 1
        centis = 0
    return f"{hrs}:{mins:02d}:{secs:02d}.{centis:02d}"


def sanitize_ass_word(word: str) -> str:
    """Removes characters that ASS interprets as override tags/newlines so Roman Hinglish renders verbatim."""
    return word.replace("\\", "").replace("{", "").replace("}", "").replace("\n", " ").strip()


class SubtitleService:
    @staticmethod
    def generate_ass_subtitles(
        word_timestamps: List[WordTimestamp],
        output_path: Optional[str] = None,
        job_id: Optional[str] = None,
        styling_override: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Generates an Advanced SubStation Alpha (.ass) subtitle file tailored for
        high-retention 9:16 mobile YouTube Shorts.
        Features dynamic yellow active-word highlighting, crisp white text, and heavy black borders.
        """
        if not word_timestamps:
            raise ValueError("No word timestamps provided to generate subtitles.")

        style = {
            "font_name": settings.SUBTITLE_FONT_NAME,
            "font_size": settings.SUBTITLE_FONT_SIZE,
            "active_color": settings.SUBTITLE_ACTIVE_COLOR,
            "inactive_color": settings.SUBTITLE_INACTIVE_COLOR,
            "outline_color": settings.SUBTITLE_OUTLINE_COLOR,
            "outline_width": settings.SUBTITLE_OUTLINE_WIDTH,
            "margin_v": settings.SUBTITLE_MARGIN_V,
            "words_per_chunk": settings.SUBTITLE_WORDS_PER_CHUNK,
        }
        if styling_override:
            style.update(styling_override)

        # ASS File Header for 1080x1920 9:16 Vertical Canvas
        header = f"""[Script Info]
Title: AI Faceless Shorts Studio Auto-Captions
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{style['font_name']},{style['font_size']},{style['inactive_color']},{style['active_color']},{style['outline_color']},&H80000000,-1,0,0,0,100,100,0,0,1,{style['outline_width']},2,2,60,60,{style['margin_v']},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

        events: List[str] = []
        chunk_size = max(1, style["words_per_chunk"])

        # Split timestamps into small sequential chunks (e.g. 2-3 words)
        total_words = len(word_timestamps)
        for i in range(0, total_words, chunk_size):
            chunk = word_timestamps[i:i + chunk_size]

            # In each chunk, generate a dialogue line for each active word
            for active_idx in range(len(chunk)):
                active_word_item = chunk[active_idx]
                start_sec = active_word_item.start_time

                # Active word duration ends when next word begins, or at active_word_item.end_time
                if active_idx + 1 < len(chunk):
                    end_sec = chunk[active_idx + 1].start_time
                elif i + chunk_size < total_words:
                    end_sec = word_timestamps[i + chunk_size].start_time
                else:
                    end_sec = active_word_item.end_time

                # Prevent overlapping or 0-duration dialogue
                if end_sec <= start_sec:
                    end_sec = start_sec + 0.15

                start_ass = seconds_to_ass_time(start_sec)
                end_ass = seconds_to_ass_time(end_sec)

                # Assemble styled words: active word in yellow uppercase, inactive in white
                line_words = []
                for idx, item in enumerate(chunk):
                    clean_word = sanitize_ass_word(item.word).upper()
                    if idx == active_idx:
                        # Highlight active word with yellow color tag
                        line_words.append(f"{{\\c{style['active_color']}}}{clean_word}{{\\c{style['inactive_color']}}}")
                    else:
                        line_words.append(clean_word)

                text_content = " ".join(line_words)
                events.append(f"Dialogue: 0,{start_ass},{end_ass},Default,,0,0,0,,{text_content}")

        ass_content = header + "\n".join(events) + "\n"

        # Determine target file destination
        if output_path:
            target_path = Path(output_path)
        elif job_id:
            job_dir = JobService.get_job_directory(job_id)
            target_path = job_dir / "subtitles" / "captions.ass"
        else:
            temp_dir = settings.STORAGE_DIR / "temp"
            temp_dir.mkdir(parents=True, exist_ok=True)
            target_path = temp_dir / "captions.ass"

        target_path.parent.mkdir(parents=True, exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(ass_content)

        # Update Job Manifest & DB if job_id provided
        if job_id:
            manifest = JobService.load_manifest(job_id)
            if manifest:
                manifest.subtitle_file = str(target_path)
                manifest.status = JobStatus.TIMESTAMPS_READY
                JobService.save_manifest(manifest)

            JobRepository.update_job_status(job_id, JobStatus.TIMESTAMPS_READY.value)
            JobRepository.add_log(
                job_id,
                "INFO",
                "SUBTITLE_GENERATION",
                f"Generated ASS subtitle file ({len(events)} dialogue frames) at '{target_path.name}'"
            )

        return str(target_path)
