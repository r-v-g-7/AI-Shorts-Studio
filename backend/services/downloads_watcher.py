import re
import time
import shutil
import logging
import threading
import datetime
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from backend.config import settings
from backend.database.repository import JobRepository
from backend.schemas.job_manifest import JobStatus
from backend.services.job_service import JobService
from backend.services.scene_intake_service import SceneIntakeService

logger = logging.getLogger(__name__)

WATCHED_EXTENSIONS = {".mp4", ".mov"}
# Browsers append these while a download is still in progress.
PARTIAL_SUFFIXES = {".crdownload", ".part", ".partial", ".tmp", ".download", ".opdownload"}
# Job states in which the studio is waiting for visual clips to arrive.
AWAITING_CLIPS_STATUSES = (
    JobStatus.AWAITING_CLIPS.value,
    JobStatus.VISUALS_PENDING.value,
    "AWAITING_SCENES",
)
# Jobs untouched for longer than this are never chosen as the clip target (guards against stale projects).
ACTIVE_JOB_WINDOW_HOURS = 24
# Deduplication window (seconds): if two downloads appear within 10s with identical file sizes, discard the duplicate.
DUPLICATE_SIZE_WINDOW_SECONDS = 10.0


class DownloadsWatcher:
    """
    Non-blocking background watcher for the user's Downloads folder.

    Finished video downloads (.mp4 / .mov) are automatically mapped to the next missing scene
    (scene_01.mp4 -> scene_02.mp4 -> scene_03.mp4 -> scene_04.mp4) based on disk existence in
    storage/jobs/<active_job_id>/incoming_clips/, handed to SceneIntakeService, and once all 4
    scenes have valid clips, video stitching and ASS subtitle burning are triggered immediately.

    Includes strict single-clip-per-scene deduplication: tracks `current_scene_index` explicitly
    and ignores any file arriving with identical byte size within 10 seconds of an existing scene.
    """

    _instance: Optional["DownloadsWatcher"] = None
    _instance_lock = threading.Lock()

    def __init__(
        self,
        downloads_dir: Optional[Path] = None,
        poll_seconds: Optional[float] = None,
        stable_seconds: Optional[float] = None,
        pinned_job_id: Optional[str] = None,
        auto_render: bool = True,
        dedup_window_seconds: float = DUPLICATE_SIZE_WINDOW_SECONDS,
    ):
        self.downloads_dir = Path(downloads_dir or settings.DOWNLOADS_DIR)
        self.poll_seconds = float(poll_seconds if poll_seconds is not None else settings.DOWNLOADS_POLL_SECONDS)
        self.stable_seconds = float(stable_seconds if stable_seconds is not None else settings.DOWNLOADS_STABLE_SECONDS)
        self.pinned_job_id = pinned_job_id
        self.auto_render = auto_render
        self.dedup_window_seconds = float(dedup_window_seconds)
        self.current_scene_index: Optional[int] = 0           # explicit active scene index awaiting ingestion

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._baseline: Optional[set] = None                  # files that existed when the watcher started
        self._observed: Dict[str, Tuple[int, float]] = {}   # path -> (size, first time this size was seen)
        self._ignored: set = set()                            # (path, size, mtime) signatures we never retry
        self._recent_ingestions: Deque[Dict[str, Any]] = deque(maxlen=32)  # {size, timestamp, scene_index, job_id, name}
        self._render_requested: set = set()
        self._active_job_id: Optional[str] = pinned_job_id
        self._events: Deque[Dict[str, Any]] = deque(maxlen=25)

    # ------------------------------------------------------------------ lifecycle
    @classmethod
    def get_instance(cls) -> "DownloadsWatcher":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._baseline = self._snapshot()  # whatever is already in Downloads is never touched
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="DownloadsWatcher", daemon=True)
        self._thread.start()
        logger.info(f"DownloadsWatcher started, watching '{self.downloads_dir}' ({len(self._baseline)} existing files ignored).")

    def _snapshot(self) -> set:
        """Signatures (path, size, mtime) of every file currently in the folder."""
        snap = set()
        if self.downloads_dir.exists():
            for p in self.downloads_dir.iterdir():
                try:
                    if p.is_file():
                        st = p.stat()
                        snap.add((str(p), st.st_size, int(st.st_mtime)))
                except OSError:
                    continue
        return snap

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("DownloadsWatcher stopped.")

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.scan_once()
            except Exception:
                logger.exception("DownloadsWatcher scan failed")
            self._stop_event.wait(self.poll_seconds)

    # ------------------------------------------------------------------ helpers
    def _record(self, level: str, message: str, job_id: Optional[str] = None, **extra: Any) -> None:
        event = {
            "time": datetime.datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": message,
            "job_id": job_id,
            **extra,
        }
        with self._lock:
            self._events.append(event)
        logger.log(logging.WARNING if level == "WARN" else logging.INFO, f"[DownloadsWatcher] {message}")
        if job_id:
            try:
                JobRepository.add_log(job_id, level, "DOWNLOADS_WATCHER", message)
            except Exception:
                logger.exception("Could not write watcher log to job")

    def resolve_active_job(self) -> Optional[str]:
        """The job currently waiting for clips: the pinned job, else the most recently updated waiting job."""
        if self.pinned_job_id:
            return self.pinned_job_id

        from backend.services.queue_manager import ProductionQueueManager
        busy = set()
        queue_status = ProductionQueueManager.get_instance().get_status()
        if queue_status.get("active_job"):
            busy.add(queue_status["active_job"]["job_id"])
        busy.update(item["job_id"] for item in queue_status.get("queued_jobs", []))

        cutoff = (datetime.datetime.utcnow() - datetime.timedelta(hours=ACTIVE_JOB_WINDOW_HOURS)).isoformat()
        waiting = [
            j for j in JobRepository.list_jobs(limit=100)
            if j.get("status") in AWAITING_CLIPS_STATUSES
            and j["id"] not in busy
            and (j.get("updated_at") or "") >= cutoff
        ]
        if not waiting:
            return None
        waiting.sort(key=lambda j: j.get("updated_at") or "", reverse=True)
        return waiting[0]["id"]

    def _is_stable(self, path: Path) -> bool:
        try:
            st = path.stat()
        except OSError:
            return False
        key = str(path)
        now = time.time()
        prev = self._observed.get(key)
        if prev is None or prev[0] != st.st_size:
            self._observed[key] = (st.st_size, now)
            return False
        return st.st_size > 0 and (now - prev[1]) >= self.stable_seconds

    def _archive_existing(self, incoming_dir: Path, scene_index: int, keep: Path) -> None:
        """A newer download of the same scene supersedes the previous one (kept in _superseded/)."""
        for old in incoming_dir.iterdir():
            if not old.is_file() or old == keep or old.suffix.lower() not in WATCHED_EXTENSIONS:
                continue
            idx = SceneIntakeService.match_filename_to_scene_index(old.name, 99)
            if idx == scene_index:
                archive = incoming_dir / "_superseded"
                archive.mkdir(exist_ok=True)
                shutil.move(str(old), str(archive / f"{old.stem}_{int(time.time())}{old.suffix}"))

    def register_direct_scene_download(
        self,
        job_id: Optional[str],
        scene_index: int,
        file_size: int,
        target_path: Optional[Path] = None,
    ) -> None:
        """
        Called by FlowAutomationService when a scene is downloaded directly into incoming_clips/scene_0X.mp4
        so that if Chrome simultaneously drops an identical-size duplicate into Downloads/ within 5s,
        DownloadsWatcher recognizes and discards the duplicate instead of mapping it to Scene X+1.
        """
        with self._lock:
            self._recent_ingestions.append(
                {
                    "job_id": job_id,
                    "scene_index": scene_index,
                    "size": int(file_size),
                    "timestamp": time.time(),
                    "name": target_path.name if target_path else f"scene_{scene_index + 1:02d}.mp4",
                }
            )

    def _is_duplicate_download(self, path: Path, file_size: int, job_id: str, incoming_dir: Path) -> Optional[str]:
        """
        Requirement 4: Ensure the watcher ingests at most one file per active scene.
        If two downloads appear within 5 seconds (`self.dedup_window_seconds`) with identical file sizes,
        discard the duplicate rather than mapping it to the next scene index.
        Returns a human-readable reason if `path` is a duplicate, else None.
        """
        now = time.time()
        with self._lock:
            for prev in reversed(self._recent_ingestions):
                age = now - float(prev.get("timestamp", 0.0))
                if age <= self.dedup_window_seconds and int(prev.get("size", -1)) == int(file_size):
                    return (
                        f"identical size ({file_size} bytes) to '{prev.get('name')}' "
                        f"ingested {age:.2f}s ago (<= {self.dedup_window_seconds:.0f}s window)"
                    )

        # Also check existing scene clips in incoming_clips/ modified within the deduplication window
        if incoming_dir.exists():
            for existing in incoming_dir.iterdir():
                if not existing.is_file() or existing.suffix.lower() not in WATCHED_EXTENSIONS:
                    continue
                try:
                    est = existing.stat()
                    age = abs(now - est.st_mtime)
                    if est.st_size == file_size and age <= max(self.dedup_window_seconds, 15.0):
                        return (
                            f"identical size ({file_size} bytes) to existing '{existing.name}' "
                            f"in incoming_clips/ (age={age:.2f}s)"
                        )
                except OSError:
                    continue
        return None

    def sync_job_disk_state(self, job_id: str, reset_clips: bool = False) -> Dict[str, Any]:
        """
        Synchronizes in-memory watcher state, `current_scene_index`, and `manifest.json`
        strictly with actual on-disk files in `incoming_clips/` and `scenes/`.
        If `reset_clips=True`, clears existing scene clips so a fresh generation starts at Scene 1 (0/4).
        """
        incoming_dir = SceneIntakeService.get_incoming_directory(job_id)
        scenes_dir = SceneIntakeService.get_scenes_directory(job_id)

        if reset_clips:
            for folder in (incoming_dir, scenes_dir):
                if folder.exists():
                    for f in folder.iterdir():
                        if f.is_file() and f.suffix.lower() in WATCHED_EXTENSIONS:
                            try:
                                f.unlink()
                            except OSError:
                                pass
            with self._lock:
                self._recent_ingestions.clear()
                self._render_requested.discard(job_id)
                self.current_scene_index = 0

        manifest = JobService.load_manifest(job_id)
        scene_count = len(manifest.scenes) if (manifest and manifest.scenes) else 4
        if manifest and manifest.scenes:
            ingest = SceneIntakeService.scan_and_ingest(job_id)
            missing = ingest.get("missing_scenes", [])
            self.current_scene_index = missing[0] if missing else None
            return ingest

        existing, missing = SceneIntakeService.get_existing_and_missing_scenes(job_id, total_scenes=scene_count)
        self.current_scene_index = missing[0] if missing else None
        return {
            "job_id": job_id,
            "total_scenes": scene_count,
            "assigned_scenes_count": len(existing),
            "missing_scenes": missing,
            "all_scenes_ready": len(missing) == 0,
        }

    # ------------------------------------------------------------------ core
    def scan_once(self) -> List[Dict[str, Any]]:
        """Runs one scan pass. Returns the list of intake results (one per moved clip)."""
        results: List[Dict[str, Any]] = []
        if not self.downloads_dir.exists():
            return results

        if self._baseline is None:
            self._baseline = self._snapshot()
        self._active_job_id = self.resolve_active_job()
        job_id = self._active_job_id

        if job_id:
            manifest = JobService.load_manifest(job_id)
            if manifest and manifest.scenes:
                _, missing_now = SceneIntakeService.get_existing_and_missing_scenes(
                    job_id, total_scenes=len(manifest.scenes)
                )
                self.current_scene_index = missing_now[0] if missing_now else None

        for path in sorted(self.downloads_dir.iterdir()):
            if not path.is_file():
                continue
            suffixes = {s.lower() for s in path.suffixes}
            if suffixes & PARTIAL_SUFFIXES or path.suffix.lower() not in WATCHED_EXTENSIONS:
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            signature = (str(path), st.st_size, int(st.st_mtime))
            if signature in self._baseline or signature in self._ignored:
                continue  # pre-existing download (present when the watcher started) or already judged
            if not self._is_stable(path):
                continue  # still being written
            if not job_id:
                continue  # nobody is waiting for clips yet; leave the file where it is and retry later

            manifest = JobService.load_manifest(job_id)
            scene_count = len(manifest.scenes) if manifest else 0
            if scene_count == 0:
                continue

            incoming_dir = SceneIntakeService.get_incoming_directory(job_id)

            # Requirement 2: Deduplicate downloads appearing within 10 seconds with identical file sizes
            dup_reason = self._is_duplicate_download(path, st.st_size, job_id, incoming_dir)
            if dup_reason:
                self._ignored.add(signature)
                self._observed.pop(str(path), None)
                try:
                    dup_archive = incoming_dir / "_duplicates"
                    dup_archive.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(dup_archive / f"{path.stem}_dup_{int(time.time())}{path.suffix}"))
                except OSError:
                    pass
                self._record(
                    "INFO",
                    f"Discarded duplicate download '{path.name}': {dup_reason}.",
                    job_id,
                )
                continue

            # Inspect existing clips in incoming_clips/ to find missing scene indices (0..scene_count-1)
            existing_indices, missing_indices = SceneIntakeService.get_existing_and_missing_scenes(
                job_id, total_scenes=scene_count
            )
            self.current_scene_index = missing_indices[0] if missing_indices else None

            # Check if filename explicitly specifies a scene index (e.g. scene_02.mp4);
            # otherwise map to `self.current_scene_index` (the lowest missing scene index).
            explicit_idx = SceneIntakeService.match_filename_to_scene_index(path.name, scene_count)
            if explicit_idx is not None:
                # If this explicit scene already exists with the exact same byte size, discard as duplicate
                existing_target = incoming_dir / f"scene_{explicit_idx + 1:02d}.mp4"
                if existing_target.exists() and existing_target.stat().st_size == st.st_size:
                    self._ignored.add(signature)
                    self._observed.pop(str(path), None)
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    self._record(
                        "INFO",
                        f"Discarded duplicate explicit clip '{path.name}' (Scene {explicit_idx + 1} already ingested with {st.st_size} bytes).",
                        job_id,
                    )
                    continue
                scene_idx = explicit_idx
            elif self.current_scene_index is not None:
                scene_idx = self.current_scene_index
            else:
                self._ignored.add(signature)
                self._record(
                    "INFO",
                    f"Ignored '{path.name}': all {scene_count} scenes already exist in incoming_clips/.",
                    job_id,
                )
                continue

            target = incoming_dir / f"scene_{scene_idx + 1:02d}.mp4"
            try:
                self._archive_existing(incoming_dir, scene_idx, keep=target)
                if target.exists():
                    archive = incoming_dir / "_superseded"
                    archive.mkdir(exist_ok=True)
                    shutil.move(str(target), str(archive / f"{target.stem}_{int(time.time())}{target.suffix}"))
                shutil.move(str(path), str(target))
            except OSError as e:
                logger.info(f"'{path.name}' not movable yet ({e}); will retry.")
                self._observed.pop(str(path), None)
                continue

            self._observed.pop(str(path), None)
            with self._lock:
                self._recent_ingestions.append(
                    {
                        "job_id": job_id,
                        "scene_index": scene_idx,
                        "size": st.st_size,
                        "timestamp": time.time(),
                        "name": target.name,
                    }
                )
            ingest = SceneIntakeService.scan_and_ingest(job_id)
            missing_after = ingest.get("missing_scenes", [])
            self.current_scene_index = missing_after[0] if missing_after else None
            ready = ingest["assigned_scenes_count"]
            if not ingest["all_scenes_ready"]:
                JobService.update_job_status(job_id, JobStatus.AWAITING_CLIPS)
            self._record(
                "INFO",
                f"Auto-mapped '{path.name}' -> incoming_clips/{target.name} (Scene {scene_idx + 1}/{scene_count}). "
                f"{ready}/{ingest['total_scenes']} scenes ready.",
                job_id,
                scene=scene_idx + 1,
            )
            results.append(ingest)

            if ingest["all_scenes_ready"] or ready >= scene_count:
                self._maybe_auto_render(job_id)

        return results

    def _maybe_auto_render(self, job_id: str) -> None:
        if not self.auto_render or job_id in self._render_requested:
            return
        job = JobService.get_job(job_id)
        if not job or job.get("status") == JobStatus.COMPLETED.value:
            return

        from backend.services.queue_manager import ProductionQueueManager
        res = ProductionQueueManager.get_instance().enqueue_job(job_id, action="RENDER")
        if res.get("status") == "success":
            self._render_requested.add(job_id)
            self._record("INFO", "All scene clips received: queued FFmpeg render + subtitle burn + QC automatically.", job_id)
        else:
            self._record("WARN", f"Auto-render not queued: {res.get('message')}", job_id)

    # ------------------------------------------------------------------ status for the UI
    def status(self) -> Dict[str, Any]:
        job_id = self._active_job_id or self.resolve_active_job()
        title: Optional[str] = None
        clips: List[bool] = []
        if job_id:
            manifest = JobService.load_manifest(job_id)
            if manifest and manifest.scenes:
                title = manifest.title
                existing_indices, missing_indices = SceneIntakeService.get_existing_and_missing_scenes(
                    job_id, total_scenes=len(manifest.scenes)
                )
                existing_set = set(existing_indices)
                clips = [i in existing_set for i in range(len(manifest.scenes))]
                self.current_scene_index = missing_indices[0] if missing_indices else None
                # Keep manifest.scenes[i].clip_file strictly synced with actual disk existence
                dirty = False
                scenes_dir = SceneIntakeService.get_scenes_directory(job_id)
                for idx, sc in enumerate(manifest.scenes):
                    expected_path = str(scenes_dir / f"scene_{idx + 1:02d}.mp4") if idx in existing_set else None
                    if sc.clip_file != expected_path or sc.is_validated != (idx in existing_set):
                        sc.clip_file = expected_path
                        sc.is_validated = idx in existing_set
                        dirty = True
                if dirty:
                    JobService.save_manifest(manifest)
                    JobService.set_scenes(job_id, manifest.scenes)
        with self._lock:
            events = list(self._events)
        return {
            "enabled": settings.DOWNLOADS_WATCH_ENABLED,
            "running": self.running,
            "downloads_dir": str(self.downloads_dir),
            "active_job_id": job_id,
            "active_job_title": title,
            "current_scene_index": self.current_scene_index,
            "scene_clips": clips,
            "scenes_ready": sum(clips),
            "scenes_total": len(clips),
            "recent_events": events[-8:],
        }
