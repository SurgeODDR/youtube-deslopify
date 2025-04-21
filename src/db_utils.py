import sqlite3
import os
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DB_DIR / "youtube_deslopify.db"

def initialize_database():
    """Initializes the SQLite database and creates tables if they don't exist."""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Table for channels
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        url TEXT UNIQUE NOT NULL,
        views INTEGER,
        fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Table for downloaded videos
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS processed_videos (
        video_id TEXT PRIMARY KEY,
        channel_id TEXT,
        channel_name TEXT,
        channel_url TEXT,
        video_title TEXT,
        video_duration INTEGER,  -- seconds
        local_video_path TEXT,
        downloaded_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (channel_url) REFERENCES channels(url)
    )
    """)

    # Table for audio snippets
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS audio_snippets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id TEXT NOT NULL UNIQUE, -- Assuming one snippet per video for now
        original_video_path TEXT,
        local_snippet_path TEXT,
        snippet_duration_sec INTEGER,
        processed_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'pending', -- e.g., 'pending', 'success', 'error'
        error_message TEXT,
        FOREIGN KEY (video_id) REFERENCES processed_videos(video_id)
    )
    """)

    # Table for transcriptions
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS transcriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id TEXT NOT NULL UNIQUE, -- Assuming one transcription per video/snippet
        audio_snippet_path TEXT,
        transcription_text TEXT,
        detected_language TEXT,
        processing_time_sec REAL,
        processed_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'pending', -- e.g., 'pending', 'success', 'error'
        error_message TEXT,
        FOREIGN KEY (video_id) REFERENCES processed_videos(video_id)
    )
    """)

    # Table for classifications
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS classifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id TEXT NOT NULL UNIQUE, -- Assuming one classification per video
        transcription_id INTEGER,
        language_detected TEXT,
        language_score INTEGER,
        coherence_score INTEGER,
        educational_score INTEGER,
        engagement_score INTEGER,
        appropriateness_score INTEGER,
        overall_score INTEGER,
        reasoning TEXT,
        model_used TEXT,
        processing_time_sec REAL,
        processed_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'pending', -- e.g., 'pending', 'success', 'error'
        error_message TEXT,
        FOREIGN KEY (video_id) REFERENCES processed_videos(video_id),
        FOREIGN KEY (transcription_id) REFERENCES transcriptions(id)
    )
    """)

    conn.commit()
    conn.close()
    print(f"Database initialized/verified at: {DB_PATH}")

def get_db_connection():
    """Returns a connection object to the SQLite database."""
    return sqlite3.connect(DB_PATH)

if __name__ == '__main__':
    initialize_database() 