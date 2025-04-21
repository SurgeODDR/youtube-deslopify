import asyncio
import logging
import sqlite3
import sys
import os
import time # Added for duration calculation
from pathlib import Path
from datetime import datetime # Added for timestamping
from typing import Optional # Added for type hinting
from dotenv import load_dotenv

# Ensure src is in path BEFORE other imports
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

load_dotenv(project_root / ".env") # Load .env from project root

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(), # Log to console
        # Optional: Add FileHandler here
        # logging.FileHandler("library_enhancer.log")
    ]
)
logger = logging.getLogger(__name__)

# Local imports after path modification
try:
    from src.db_utils import get_db_connection, initialize_database
    from src.perplexity_client import deep_research
    from src.llm_utils import flash_normalise_channel_list
except ImportError as e:
    logger.exception(f"Failed to import necessary modules. Ensure src is in PYTHONPATH or script is run from project root. Error: {e}")
    sys.exit(1)

# --- Configuration ---
# Limit the number of Perplexity API calls per run
MAX_REQUESTS_PER_RUN = 20
# Concurrency limit for async tasks (both Perplexity and Gemini calls)
MAX_CONCURRENT_API_CALLS = 5
# Prompt template for Perplexity
PERPLEXITY_PROMPT_TEMPLATE = ("""
Give me a concise list of high-quality, educational YouTube channels that are similar in theme and target audience 
to the channel '{channel_name}' (URL: {channel_url}). 
Focus on channels suitable for children if the context implies it. 
For each suggested channel, provide its name and its full, canonical YouTube channel URL (e.g., starting with https://www.youtube.com/channel/...). 
Present the list clearly.
""")
# ---

# --- Database Helper Functions ---

def log_perplexity_request_start(conn: sqlite3.Connection, seed_url: str, prompt: str) -> int:
    """Logs the start of a Perplexity request and returns the new row ID."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO perplexity_requests (seed_channel_url, prompt_text, status, requested_at) VALUES (?, ?, ?, ?)",
            (seed_url, prompt, 'pending', datetime.now())
        )
        conn.commit()
        request_id = cursor.lastrowid
        logger.debug(f"Logged perplexity request start for seed {seed_url}, ID: {request_id}")
        return request_id
    except sqlite3.Error as e:
        logger.error(f"DB error logging perplexity request start for {seed_url}: {e}")
        conn.rollback() # Rollback if initial insert fails
        raise # Re-raise to signal failure in the calling function

def update_perplexity_request_end(
    conn: sqlite3.Connection, 
    request_id: int, 
    status: str, 
    duration: float, 
    response: Optional[str] = None, 
    error: Optional[str] = None
):
    """Updates the perplexity request record with the final status and result."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE perplexity_requests SET status = ?, duration_sec = ?, response_text = ?, error_message = ? WHERE id = ?",
            (status, duration, response, error, request_id)
        )
        conn.commit()
        logger.debug(f"Updated perplexity request ID {request_id} with status {status}")
    except sqlite3.Error as e:
        logger.error(f"DB error updating perplexity request {request_id}: {e}")
        conn.rollback()
        # Don't re-raise here, as the main task might have completed, just log the update failure

def log_gemini_request_start(conn: sqlite3.Connection, perplexity_req_id: int, input_preview: str, prompt: str) -> int:
    """Logs the start of a Gemini normalization request."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO gemini_normalization_requests (perplexity_request_id, input_text_preview, prompt_text, status, requested_at) VALUES (?, ?, ?, ?, ?)",
            (perplexity_req_id, input_preview, prompt, 'pending', datetime.now())
        )
        conn.commit()
        request_id = cursor.lastrowid
        logger.debug(f"Logged gemini request start for perplexity ID {perplexity_req_id}, Gemini ID: {request_id}")
        return request_id
    except sqlite3.Error as e:
        logger.error(f"DB error logging gemini request start for perplexity ID {perplexity_req_id}: {e}")
        conn.rollback()
        raise

def update_gemini_request_end(
    conn: sqlite3.Connection, 
    request_id: int, 
    status: str, 
    duration: float, 
    response_text: Optional[str] = None, 
    parsed_count: Optional[int] = None,
    error: Optional[str] = None
):
    """Updates the gemini normalization request record."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE gemini_normalization_requests SET status = ?, duration_sec = ?, response_text = ?, parsed_channels_count = ?, error_message = ? WHERE id = ?",
            (status, duration, response_text, parsed_count, error, request_id)
        )
        conn.commit()
        logger.debug(f"Updated gemini request ID {request_id} with status {status}")
    except sqlite3.Error as e:
        logger.error(f"DB error updating gemini request {request_id}: {e}")
        conn.rollback()

# --- Main Logic ---

def get_seed_channels(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Fetches distinct high-quality channels from the classifications table."""
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT DISTINCT pv.channel_name, pv.channel_url
            FROM classifications c
            JOIN processed_videos pv ON c.video_id = pv.video_id
            WHERE c.overall_score >= 4 
              AND c.status = 'success'
              AND pv.channel_name IS NOT NULL
              AND pv.channel_url IS NOT NULL
            ORDER BY pv.channel_name -- Optional: for consistent ordering if limiting
        """)
        seeds = cursor.fetchall()
        valid_seeds = [(name, url) for name, url in seeds if name and url]
        logger.info(f"Found {len(valid_seeds)} unique, high-quality seed channels in the database.")
        return valid_seeds
    except sqlite3.Error as e:
        logger.error(f"Database error fetching seed channels: {e}")
        return []

def insert_new_channels(conn: sqlite3.Connection, channels_to_add: list[dict]):
    """Inserts new channels into the database, ignoring duplicates based on URL."""
    if not channels_to_add:
        return 0
    cursor = conn.cursor()
    inserted_count = 0
    skipped_count = 0
    error_count = 0
    sql = """INSERT INTO channels (name, url, views, fetched_at) 
             VALUES (?, ?, 0, CURRENT_TIMESTAMP) 
             ON CONFLICT(url) DO NOTHING"""
    for channel_data in channels_to_add:
        channel_name = channel_data.get('channel_name')
        channel_url = channel_data.get('channel_url')
        if not channel_name or not channel_url:
            logger.warning(f"Skipping invalid channel data: {channel_data}")
            skipped_count += 1
            continue
        try:
            # Convert HttpUrl back to string if necessary for DB
            url_str = str(channel_url) if not isinstance(channel_url, str) else channel_url
            cursor.execute(sql, (channel_name, url_str))
            if cursor.rowcount > 0:
                inserted_count += 1
                logger.debug(f"Inserted new channel: {channel_name} ({url_str})")
            else:
                logger.debug(f"Channel already exists, skipped: {channel_name} ({url_str})")
                skipped_count += 1
        except sqlite3.Error as e:
            logger.error(f"Database error inserting channel {channel_name} ({url_str}): {e}")
            error_count += 1
        except Exception as e:
            logger.error(f"Unexpected error inserting channel {channel_name}: {e}", exc_info=True)
            error_count += 1
    try:
        conn.commit()
        logger.info(f"Database commit successful. Inserted: {inserted_count}, Skipped (duplicates/invalid): {skipped_count}, Errors: {error_count}")
    except sqlite3.Error as e:
        logger.error(f"Database commit error after inserting channels: {e}")
        return 0
    return inserted_count

async def process_single_seed_channel(
    channel_name: str, 
    channel_url: str, 
    semaphore: asyncio.Semaphore, 
    conn: sqlite3.Connection # Pass connection for DB updates
) -> list[dict]:
    """Task to perform research and normalization for one seed channel, logging results."""
    perplexity_request_id = None
    gemini_request_id = None
    start_time_perplexity = time.time()
    perplexity_status = 'api_error' # Default status
    perplexity_response_text = None
    perplexity_error = None
    normalized_channels = [] # Default empty list

    async with semaphore:
        logger.info(f"Starting processing for seed channel: {channel_name}")
        prompt = PERPLEXITY_PROMPT_TEMPLATE.format(channel_name=channel_name, channel_url=channel_url)
        
        try:
            # 1. Log Perplexity request start
            perplexity_request_id = log_perplexity_request_start(conn, channel_url, prompt)

            # 2. Call Perplexity Deep Research
            perplexity_response_text = await deep_research(prompt)
            duration_perplexity = time.time() - start_time_perplexity

            if perplexity_response_text is None:
                perplexity_status = 'no_response'
                perplexity_error = "deep_research function returned None (check logs for specific API errors/timeouts)"
                logger.warning(f"No research text received from Perplexity for seed: {channel_name}. Error: {perplexity_error}")
            else:
                perplexity_status = 'success'
                logger.info(f"Received research text for {channel_name}. Normalizing with Gemini...")
                
                # --- Gemini Normalization --- 
                start_time_gemini = time.time()
                gemini_status = 'api_error' # Default
                gemini_response_raw = None
                gemini_error_detail = None # Renamed from gemini_error to avoid conflict
                parsed_count = 0
                gemini_prompt_text = "Defined in llm_utils.py" # Placeholder
                input_preview = perplexity_response_text[:200] + "..." if perplexity_response_text else "N/A"
                
                try:
                    # 3. Log Gemini request start
                    gemini_request_id = log_gemini_request_start(conn, perplexity_request_id, input_preview, gemini_prompt_text)
                    
                    # 4. Call Gemini Normalization - Unpack the new return tuple
                    normalized_channels, gemini_response_raw, gemini_error_detail = await flash_normalise_channel_list(perplexity_response_text)
                    duration_gemini = time.time() - start_time_gemini
                    parsed_count = len(normalized_channels)
                    
                    # Determine Gemini status based on the result
                    if gemini_error_detail:
                        # Map error messages to status codes
                        if "Response blocked" in gemini_error_detail:
                            gemini_status = 'blocked'
                        elif "Failed to decode JSON" in gemini_error_detail:
                            gemini_status = 'parse_error'
                        elif "Pydantic validation failed" in gemini_error_detail:
                            gemini_status = 'validation_error'
                        elif "API key is missing" in gemini_error_detail:
                            gemini_status = 'api_error' # Or config_error?
                        else:
                            gemini_status = 'error' # General error
                        logger.warning(f"Gemini normalization for {channel_name} failed: {gemini_error_detail}")
                        normalized_channels = [] # Ensure empty list on error
                        parsed_count = 0
                    elif parsed_count > 0:
                        gemini_status = 'success'
                        logger.info(f"Normalized {parsed_count} potential new channels from research on {channel_name}.")
                    else: # No error, but list is empty
                        gemini_status = 'empty_response'
                        logger.info(f"Normalization resulted in 0 channels for {channel_name}.")

                except Exception as e_gemini: 
                    duration_gemini = time.time() - start_time_gemini
                    gemini_status = 'error' 
                    gemini_error_detail = f"Error during Gemini processing call: {e_gemini}"
                    logger.exception(f"Error during Gemini normalization call for perplexity request {perplexity_request_id}: {e_gemini}")
                    normalized_channels = []
                    parsed_count = 0
                finally:
                    # 5. Update Gemini request log
                    if gemini_request_id is not None:
                        update_gemini_request_end(
                            conn, gemini_request_id, gemini_status, duration_gemini,
                            response_text=gemini_response_raw, # Log the raw text
                            parsed_count=parsed_count,
                            error=gemini_error_detail # Log the specific error
                        )
                    else:
                         logger.error("Could not update Gemini request log as gemini_request_id is None.")
        
        except Exception as e_perplexity:
            duration_perplexity = time.time() - start_time_perplexity
            perplexity_status = 'error'
            perplexity_error = f"Error during Perplexity processing: {e_perplexity}"
            logger.exception(f"Error processing seed channel {channel_name}: {e_perplexity}")
            normalized_channels = []
        finally:
            # 6. Update Perplexity request log
            if perplexity_request_id is not None:
                update_perplexity_request_end(
                    conn, perplexity_request_id, perplexity_status, duration_perplexity,
                    response=perplexity_response_text,
                    error=perplexity_error
                )
            else:
                 logger.error("Could not update Perplexity request log as perplexity_request_id is None.")

    return normalized_channels

async def run_library_enhancer():
    """Main orchestration function for the library enhancer process."""
    logger.info("=== Starting Library Enhancer Process ===")
    try:
        initialize_database()
    except Exception as e:
         logger.exception("Failed to initialize database.")
         return

    conn = None
    total_new_channels_added = 0
    try:
        conn = get_db_connection()
        seed_channels = get_seed_channels(conn)

        if not seed_channels:
            logger.info("No high-quality seed channels found to process. Exiting.")
            return

        seeds_to_process = seed_channels[:MAX_REQUESTS_PER_RUN]
        if len(seed_channels) > MAX_REQUESTS_PER_RUN:
            logger.warning(f"Processing limit hit: Found {len(seed_channels)} seeds, but will only process the first {MAX_REQUESTS_PER_RUN} due to MAX_REQUESTS_PER_RUN setting.")

        logger.info(f"Starting async processing for {len(seeds_to_process)} seed channels with concurrency {MAX_CONCURRENT_API_CALLS}.")
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_API_CALLS)
        tasks = []
        for name, url in seeds_to_process:
            # Pass the connection object to the task
            tasks.append(process_single_seed_channel(name, url, semaphore, conn))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_new_channel_candidates = []
        successful_tasks = 0
        failed_tasks = 0
        for i, result in enumerate(results):
            seed_name = seeds_to_process[i][0]
            if isinstance(result, Exception):
                logger.error(f"Task for seed channel '{seed_name}' ultimately failed with exception: {result}", exc_info=result)
                failed_tasks += 1
            elif isinstance(result, list):
                # We still count the task as successful if it ran, even if it found 0 channels
                successful_tasks += 1
                all_new_channel_candidates.extend(result)
            else:
                logger.error(f"Task for seed channel '{seed_name}' returned unexpected result type: {type(result)}")
                failed_tasks += 1
        
        logger.info(f"Async processing complete. Successful tasks run: {successful_tasks}, Failed tasks: {failed_tasks}.")
        logger.info(f"Collected {len(all_new_channel_candidates)} potential new channel candidates from all successful tasks.")

        if all_new_channel_candidates:
             logger.info("Inserting collected channel candidates into the database...")
             # Use the same connection for insertion
             total_new_channels_added = insert_new_channels(conn, all_new_channel_candidates)
             logger.info(f"Total new unique channels inserted in this run: {total_new_channels_added}")
        else:
             logger.info("No new channel candidates to insert into the database.")

    except sqlite3.Error as e:
        logger.exception(f"A database error occurred during the main process: {e}")
    except Exception as e:
        logger.exception(f"An unexpected error occurred during the main process: {e}")
    finally:
        if conn:
            try:
                conn.close()
                logger.info("Database connection closed.")
            except sqlite3.Error as e:
                 logger.error(f"Error closing database connection: {e}")
        logger.info(f"=== Library Enhancer Process Finished. Added {total_new_channels_added} new channels. ===")

if __name__ == "__main__":
    try:
        asyncio.run(run_library_enhancer())
    except KeyboardInterrupt:
        logger.info("Library enhancer process interrupted by user.")
    except Exception as e:
         logger.exception("Caught exception at top level execution.") 