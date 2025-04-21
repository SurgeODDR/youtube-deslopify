import asyncio
import logging
import sqlite3
import sys
import os
from pathlib import Path
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
        # Ensure we have tuples of (name, url)
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
            cursor.execute(sql, (channel_name, channel_url))
            if cursor.rowcount > 0:
                inserted_count += 1
                logger.debug(f"Inserted new channel: {channel_name} ({channel_url})")
            else:
                logger.debug(f"Channel already exists, skipped: {channel_name} ({channel_url})")
                skipped_count += 1
        except sqlite3.Error as e:
            logger.error(f"Database error inserting channel {channel_name} ({channel_url}): {e}")
            error_count += 1
        except Exception as e:
            logger.error(f"Unexpected error inserting channel {channel_name}: {e}", exc_info=True)
            error_count += 1

    try:
        conn.commit()
        logger.info(f"Database commit successful. Inserted: {inserted_count}, Skipped (duplicates/invalid): {skipped_count}, Errors: {error_count}")
    except sqlite3.Error as e:
        logger.error(f"Database commit error after inserting channels: {e}")
        # Consider rollback? conn.rollback()
        # If commit fails, the inserts might not be persisted
        return 0 # Indicate failure or zero insertions effectively
        
    return inserted_count

async def process_single_seed_channel(channel_name: str, channel_url: str, semaphore: asyncio.Semaphore) -> list[dict]:
    """Task to perform research and normalization for one seed channel."""
    async with semaphore:
        logger.info(f"Starting processing for seed channel: {channel_name}")
        # 1. Call Perplexity Deep Research
        prompt = PERPLEXITY_PROMPT_TEMPLATE.format(channel_name=channel_name, channel_url=channel_url)
        research_text = await deep_research(prompt)

        if not research_text:
            logger.warning(f"No research text received from Perplexity for seed: {channel_name}. Skipping normalization.")
            return [] # Return empty list if research fails

        logger.info(f"Received research text for {channel_name}. Normalizing with Gemini...")
        # 2. Normalize with Gemini Flash
        normalized_channels = await flash_normalise_channel_list(research_text)

        if not normalized_channels:
            logger.warning(f"Normalization yielded no channels for seed: {channel_name}")
            return []

        logger.info(f"Normalized {len(normalized_channels)} potential new channels from research on {channel_name}.")
        return normalized_channels

async def run_library_enhancer():
    """Main orchestration function for the library enhancer process."""
    logger.info("=== Starting Library Enhancer Process ===")

    # Ensure DB exists
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

        # Apply the MAX_REQUESTS_PER_RUN limit
        seeds_to_process = seed_channels[:MAX_REQUESTS_PER_RUN]
        if len(seed_channels) > MAX_REQUESTS_PER_RUN:
            logger.warning(f"Processing limit hit: Found {len(seed_channels)} seeds, but will only process the first {MAX_REQUESTS_PER_RUN} due to MAX_REQUESTS_PER_RUN setting.")

        logger.info(f"Starting async processing for {len(seeds_to_process)} seed channels with concurrency {MAX_CONCURRENT_API_CALLS}.")
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_API_CALLS)
        tasks = []
        for name, url in seeds_to_process:
            tasks.append(process_single_seed_channel(name, url, semaphore))

        # Run tasks concurrently and gather results
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results and insert into DB
        all_new_channel_candidates = []
        successful_tasks = 0
        failed_tasks = 0
        for i, result in enumerate(results):
            seed_name = seeds_to_process[i][0] # Get corresponding seed name for logging
            if isinstance(result, Exception):
                logger.error(f"Task for seed channel '{seed_name}' failed with exception: {result}", exc_info=result)
                failed_tasks += 1
            elif isinstance(result, list):
                all_new_channel_candidates.extend(result)
                successful_tasks += 1
            else:
                logger.error(f"Task for seed channel '{seed_name}' returned unexpected result type: {type(result)}")
                failed_tasks += 1
        
        logger.info(f"Async processing complete. Successful tasks: {successful_tasks}, Failed tasks: {failed_tasks}.")
        logger.info(f"Collected {len(all_new_channel_candidates)} potential new channel candidates from all successful tasks.")

        if all_new_channel_candidates:
             logger.info("Inserting collected channel candidates into the database...")
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
    # Example of how to run it
    try:
        asyncio.run(run_library_enhancer())
    except KeyboardInterrupt:
        logger.info("Library enhancer process interrupted by user.")
    except Exception as e:
         logger.exception("Caught exception at top level execution.") 