import sqlite3
import os
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DB_DIR / "youtube_deslopify.db"

def initialize_database():
    """Initializes the SQLite database and creates tables if they don't exist."""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # Enable Foreign Key support
    conn.execute("PRAGMA foreign_keys = ON")
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
        channel_id TEXT, -- Extracted from channel_url if possible
        channel_name TEXT,
        channel_url TEXT,
        video_title TEXT,
        video_duration INTEGER,  -- seconds
        local_video_path TEXT, -- Currently unused, kept for schema consistency
        downloaded_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, -- Represents metadata fetch time
        FOREIGN KEY (channel_url) REFERENCES channels(url) ON DELETE SET NULL ON UPDATE CASCADE
    )
    """)

    # Table for audio snippets - Schema defined but functionally unused
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
        FOREIGN KEY (video_id) REFERENCES processed_videos(video_id) ON DELETE CASCADE ON UPDATE CASCADE
    )
    """)

    # Table for transcriptions
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS transcriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id TEXT NOT NULL UNIQUE, -- Assuming one transcription per video/snippet
        audio_snippet_path TEXT, -- Currently unused
        transcription_text TEXT,
        detected_language TEXT,
        processing_time_sec REAL,
        processed_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'pending', -- e.g., 'pending', 'success', 'error', 'rate_limit', 'blocked'
        error_message TEXT,
        FOREIGN KEY (video_id) REFERENCES processed_videos(video_id) ON DELETE CASCADE ON UPDATE CASCADE
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
        status TEXT DEFAULT 'pending', -- e.g., 'pending', 'success', 'error', 'rate_limit', 'blocked', 'parse_error', 'validation_error'
        error_message TEXT,
        FOREIGN KEY (video_id) REFERENCES processed_videos(video_id) ON DELETE CASCADE ON UPDATE CASCADE,
        FOREIGN KEY (transcription_id) REFERENCES transcriptions(id) ON DELETE SET NULL ON UPDATE CASCADE
    )
    """)

    # --- NEW TABLES for Library Enhancer --- 

    # Table for Perplexity API requests and responses
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS perplexity_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        seed_channel_url TEXT NOT NULL,
        prompt_text TEXT,
        response_text TEXT,
        status TEXT NOT NULL DEFAULT 'pending', -- e.g., pending, success, api_error, timeout, no_response
        error_message TEXT,
        requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        duration_sec REAL,
        FOREIGN KEY (seed_channel_url) REFERENCES channels(url) ON DELETE CASCADE ON UPDATE CASCADE
    )
    """)

    # Table for Gemini normalization API requests and responses
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS gemini_normalization_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        perplexity_request_id INTEGER NOT NULL,
        input_text_preview TEXT, -- Store first few chars of perplexity response as context
        prompt_text TEXT,
        response_text TEXT, -- Raw JSON response from Gemini
        parsed_channels_count INTEGER,
        status TEXT NOT NULL DEFAULT 'pending', -- e.g., pending, success, api_error, blocked, parse_error, validation_error, empty_response
        error_message TEXT,
        requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        duration_sec REAL,
        FOREIGN KEY (perplexity_request_id) REFERENCES perplexity_requests(id) ON DELETE CASCADE ON UPDATE CASCADE
    )
    """)

    # --- Indices --- 
    # Index for faster lookup of processed videos by channel
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_processed_videos_channel_url ON processed_videos(channel_url)")
    # Index for faster lookup of classifications by video_id
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_classifications_video_id ON classifications(video_id)")
    # Index for faster lookup of transcriptions by video_id
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_transcriptions_video_id ON transcriptions(video_id)")
    # Index for faster lookup of perplexity requests by seed channel
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_perplexity_requests_seed_url ON perplexity_requests(seed_channel_url)")
    # Index for faster lookup of gemini normalization requests by perplexity request id
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_gemini_normalization_perplexity_id ON gemini_normalization_requests(perplexity_request_id)")

    conn.commit()
    conn.close()
    print(f"Database initialized/verified at: {DB_PATH}")

def get_db_connection():
    """Returns a connection object to the SQLite database with Foreign Key support enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

if __name__ == '__main__':
    initialize_database() 