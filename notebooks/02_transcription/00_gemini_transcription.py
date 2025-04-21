import os
import time
import sys
import sqlite3
from pathlib import Path
from datetime import datetime
import mimetypes
import random
from dotenv import load_dotenv
import asyncio
import logging
import functools

import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
from google.generativeai.types import HarmCategory, HarmBlockThreshold

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Add src directory to sys.path to find db_utils
project_root = Path(__file__).resolve().parent.parent.parent
src_path = project_root / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

try:
    from db_utils import get_db_connection, initialize_database
except ImportError:
    logger.error("Could not import db_utils. Make sure src/db_utils.py exists.")
    sys.exit(1)

# --- Configuration ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL_NAME = "gemini-2.5-flash-preview-04-17"
TRANSCRIPTION_PROMPT = "Transcribe the audio track of the following YouTube video. Provide only the transcribed text."
MAX_CONCURRENT_TASKS = 50
API_RETRY_DELAY = 5
# Define safety settings to disable blocking
SAFETY_SETTINGS = {
    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
}
# ---

def configure_gemini():
    """Configures the Gemini client library with the API key."""
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY environment variable not set.")
        return False
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        logger.info(f"Gemini API configured successfully for model: {MODEL_NAME}")
        return True
    except Exception as e:
        logger.error(f"Error configuring Gemini API: {e}")
        return False

def get_pending_videos(conn):
    """Gets videos that have status 'error', 'rate_limit', or haven't been processed yet."""
    cursor = conn.cursor()
    # Select videos that are NOT successful in transcriptions table OR don't exist there
    cursor.execute("""
        SELECT 
            v.video_id,
            'https://www.youtube.com/watch?v=' || v.video_id as youtube_url 
        FROM processed_videos v
        LEFT JOIN transcriptions t ON v.video_id = t.video_id
        WHERE t.video_id IS NULL OR t.status != 'success'
    """)
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]

def update_transcription_status(video_id, status, transcription=None, language=None, processing_time=None, error_message=None):
    """Inserts or updates the status of a transcription in the database (connects internally)."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        now = datetime.now()
        
        cursor.execute("""
            INSERT INTO transcriptions (
                video_id, transcription_text, detected_language, processing_time_sec, status, error_message, processed_timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                transcription_text = excluded.transcription_text,
                detected_language = excluded.detected_language,
                processing_time_sec = excluded.processing_time_sec,
                status = excluded.status,
                error_message = excluded.error_message,
                processed_timestamp = excluded.processed_timestamp
        """, (
            video_id,
            transcription,
            language,
            processing_time,
            status,
            error_message,
            now
        ))
        conn.commit()
    except sqlite3.Error as e:
         logger.error(f"DB error updating status for {video_id}: {e}")
         # Optionally re-raise or return error status
    finally:
         if conn:
             conn.close()

async def transcribe_youtube_video_gemini_async(youtube_url: str):
    """Transcribes a YouTube video using its URL via the Gemini API (async)."""
    start_time = time.time()
    try:
        logger.info(f"Generating transcription for {youtube_url}...")
        model = genai.GenerativeModel(MODEL_NAME)
        video_data = {"file_data": {"mime_type": "video/mp4", "file_uri": youtube_url}}
        # Pass safety_settings to the API call
        response = await model.generate_content_async(
            [TRANSCRIPTION_PROMPT, video_data],
            safety_settings=SAFETY_SETTINGS
        )
        processing_time = time.time() - start_time
        
        try:
            if not response.parts:
                 if response.prompt_feedback.block_reason:
                      logger.warning(f"Response blocked for {youtube_url}. Reason: {response.prompt_feedback.block_reason}")
                      return None, f"Response blocked. Reason: {response.prompt_feedback.block_reason}", processing_time
                 else:
                      logger.warning(f"Response empty but not blocked for {youtube_url}. Feedback: {response.prompt_feedback}")
                      return None, f"Response was empty. Feedback: {response.prompt_feedback}", processing_time
        except ValueError as e:
             logger.warning(f"Response likely blocked for {youtube_url}. Feedback: {response.prompt_feedback}. Error: {e}")
             block_reason_msg = getattr(response.prompt_feedback, 'block_reason', 'Unknown')
             return None, f"Response likely blocked. Reason: {block_reason_msg}", processing_time

        transcription = response.text.strip()
        logger.info(f"Transcription successful for {youtube_url} in {processing_time:.2f}s.")
        return transcription, None, processing_time

    except google_exceptions.GoogleAPIError as e:
         logger.error(f"Gemini API error for {youtube_url}: {e}")
         error_code = getattr(e, 'code', None)
         # Check specific status code for explicit rate limit error if available
         # The google library might map HTTP 429 to a specific exception type or code
         # Example: if isinstance(e, google_exceptions.ResourceExhausted): ...
         if error_code == 429 or "resource exhausted" in str(e).lower() or "rate limit" in str(e).lower():
              logger.warning(f"Rate limit hit for {youtube_url}.")
              return None, "RATE_LIMIT_ERROR", time.time() - start_time
         return None, f"Gemini API error: {e}", time.time() - start_time
    except Exception as e:
        logger.error(f"Unexpected error during transcription for {youtube_url}: {e}", exc_info=True)
        return None, f"Unexpected error: {e}", time.time() - start_time

async def process_video_task_async(video_info: dict, semaphore: asyncio.Semaphore):
    """Async worker task to transcribe a single video and update its status, respects semaphore."""
    video_id = video_info['video_id']
    youtube_url = video_info.get('youtube_url')
    loop = asyncio.get_running_loop()
    
    if not youtube_url:
        logger.error(f"Missing YouTube URL for video {video_id}. Skipping.")
        # Use functools.partial for run_in_executor
        db_update_func = functools.partial(update_transcription_status, video_id, 'error', error_message="Missing YouTube URL in DB query result")
        await loop.run_in_executor(None, db_update_func)
        return video_id, 'error'
                 
    async with semaphore: 
        logger.info(f"Starting task for video: {video_id} ({youtube_url})")
        transcription, error, proc_time = await transcribe_youtube_video_gemini_async(youtube_url)
        status = 'unknown'
        
        # Handle results and update DB 
        if error == "RATE_LIMIT_ERROR":
            logger.warning(f"Task for {video_id} hit rate limit. Will sleep before releasing semaphore.")
            await asyncio.sleep(API_RETRY_DELAY * 2)
            status = 'rate_limit' 
            # No DB update for rate limit
        elif error:
            logger.error(f"Failed to transcribe {video_id}: {error}")
            status = 'error'
            db_update_func = functools.partial(update_transcription_status, video_id, status, error_message=str(error)[:500], processing_time=proc_time)
            await loop.run_in_executor(None, db_update_func)
        else:
            logger.info(f"Successfully transcribed {video_id}.")
            status = 'success'
            db_update_func = functools.partial(update_transcription_status, video_id, status, transcription=transcription, processing_time=proc_time)
            await loop.run_in_executor(None, db_update_func)
            
        # Small delay before releasing semaphore
        await asyncio.sleep(random.uniform(0.5, 1.5))
        
    return video_id, status

async def main():
    logger.info("Starting Gemini transcription process (from YouTube URLs) - ASYNCIO...")

    if not configure_gemini():
        sys.exit(1)

    logger.info("Initializing database...")
    initialize_database()

    conn = None
    videos_to_transcribe = []
    try:
        conn = get_db_connection()
        videos_to_transcribe = get_pending_videos(conn)
    except sqlite3.Error as e:
        logger.error(f"Failed to get pending videos from DB: {e}")
        return
    finally:
        if conn:
            conn.close()
        
    if not videos_to_transcribe:
        logger.info("No new videos found needing transcription.")
        return
            
    logger.info(f"Found {len(videos_to_transcribe)} videos to transcribe. Starting async processing with concurrency limit {MAX_CONCURRENT_TASKS}...")

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
    tasks = []
    for video_info in videos_to_transcribe:
        tasks.append(process_video_task_async(video_info, semaphore))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Process results
    total_processed_count = 0
    total_error_count = 0
    rate_limit_hits = 0
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"Task raised an exception: {result}", exc_info=result)
            total_error_count += 1
        elif isinstance(result, tuple) and len(result) == 2:
            _video_id, status = result
            if status == 'success':
                total_processed_count += 1
            elif status == 'rate_limit':
                rate_limit_hits += 1
            elif status == 'error':
                total_error_count += 1
        else:
             logger.error(f"Unexpected result format from task: {result}")
             total_error_count += 1
            
    logger.info("Async processing finished.")
    if rate_limit_hits > 0:
         logger.warning(f"Encountered {rate_limit_hits} rate limit errors during the run. Some videos may need reprocessing.")
            
    logger.info(f"\nTranscription process finished.")
    logger.info(f"Successfully transcribed: {total_processed_count}")
    logger.info(f"Errors: {total_error_count}")
    logger.info(f"Rate Limit Hits (not retried in this run): {rate_limit_hits}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Process interrupted by user.") 