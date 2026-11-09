import sqlite3
from pathlib import Path
from contextlib import contextmanager
from backend.config import settings

DB_FILE = settings.STORAGE_DIR / "studio.sqlite3"


def get_db_connection() -> sqlite3.Connection:
    """Creates a connection to the SQLite database with Row factory."""
    conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def get_db():
    """Context manager yielding a SQLite connection and safely committing/closing."""
    conn = get_db_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Initializes tables for jobs, scenes, and logs."""
    settings.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        cursor = conn.cursor()

        # Project Jobs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS project_jobs (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                topic TEXT NOT NULL,
                visual_style TEXT NOT NULL DEFAULT 'tall_tale_3d',
                status TEXT NOT NULL DEFAULT 'DRAFT',
                error_message TEXT,
                manifest_path TEXT NOT NULL,
                output_video_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        # Scenes table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scenes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                scene_index INTEGER NOT NULL,
                narration_text TEXT NOT NULL,
                visual_prompt TEXT NOT NULL,
                duration_seconds REAL NOT NULL DEFAULT 5.0,
                clip_path TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                FOREIGN KEY (job_id) REFERENCES project_jobs(id) ON DELETE CASCADE
            );
        """)

        # Logs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS job_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                level TEXT NOT NULL DEFAULT 'INFO',
                stage TEXT NOT NULL DEFAULT 'GENERAL',
                message TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES project_jobs(id) ON DELETE CASCADE
            );
        """)

        # Indexes for fast lookup
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON project_jobs(status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_scenes_job_id ON scenes(job_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_logs_job_id ON job_logs(job_id);")
