# Databricks notebook source

# COMMAND ----------
# MAGIC %md
# MAGIC # YouTube Channel Statistics Table Creation
# MAGIC This notebook reads channel statistics from a JSON file in the data volume and creates a table in Unity Catalog.
# MAGIC 
# MAGIC ## Table Details
# MAGIC - **Catalog**: yt-deslopify
# MAGIC - **Schema**: default
# MAGIC - **Table**: channel_statistics

# COMMAND ----------

import json
import sqlite3
import sys
from pathlib import Path
from collections import Counter
import logging

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
WATCH_HISTORY_DIR = project_root / "data" / "watch_history"
TOP_N_CHANNELS = 50
# ---

def process_watch_history(watch_history_dir: Path) -> Counter:
    """Processes watch history JSON files to count views per channel URL."""
    channel_counter = Counter()
    files_processed = 0
    entries_processed = 0

    if not watch_history_dir.is_dir():
        logger.error(f"Watch history directory not found: {watch_history_dir}")
        return channel_counter

    for file_path in watch_history_dir.glob("*.json"):
        logger.info(f"Processing file: {file_path.name}")
        files_processed += 1
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                history_data = json.load(f)
            
            if not isinstance(history_data, list):
                logger.warning(f"Skipping file {file_path.name}: Expected a list, got {type(history_data)}")
                continue

            for entry in history_data:
                entries_processed += 1
                if not isinstance(entry, dict):
                    continue # Skip non-dict entries
                
                # Check if 'subtitles' field exists and is a list
                subtitles = entry.get('subtitles')
                if isinstance(subtitles, list) and len(subtitles) > 0:
                    # The first item in subtitles usually contains channel info
                    channel_info = subtitles[0]
                    if isinstance(channel_info, dict):
                        channel_name = channel_info.get('name')
                        channel_url = channel_info.get('url')
                        
                        # Only count if we have a valid URL
                        if channel_url and isinstance(channel_url, str) and channel_url.startswith("http"):
                           channel_counter[channel_url] += 1
                           # Store name with URL to retrieve later - use tuple as key for Counter doesn't work well
                           # Instead, we'll store names separately keyed by URL
                        # else: logger.debug(f"Skipping entry, missing valid channel URL: {entry.get('title')}") 
                # else: logger.debug(f"Skipping entry, missing subtitles list: {entry.get('title')}")

        except json.JSONDecodeError:
            logger.warning(f"Could not decode JSON from {file_path.name}. Skipping.")
        except Exception as e:
            logger.error(f"An unexpected error occurred while reading {file_path.name}: {e}", exc_info=True)
    
    logger.info(f"Processed {files_processed} files and {entries_processed} watch history entries.")
    return channel_counter

def extract_channel_names(watch_history_dir: Path) -> dict:
    """Extracts channel names associated with channel URLs from history files."""
    url_to_name_map = {}
    if not watch_history_dir.is_dir():
        return url_to_name_map

    for file_path in watch_history_dir.glob("*.json"):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                history_data = json.load(f)
            
            if not isinstance(history_data, list):
                continue

            for entry in history_data:
                 if not isinstance(entry, dict):
                    continue
                 subtitles = entry.get('subtitles')
                 if isinstance(subtitles, list) and len(subtitles) > 0:
                    channel_info = subtitles[0]
                    if isinstance(channel_info, dict):
                        channel_name = channel_info.get('name')
                        channel_url = channel_info.get('url')
                        if channel_url and channel_name and channel_url not in url_to_name_map:
                             url_to_name_map[channel_url] = channel_name
        except Exception:
            # Ignore errors here, best effort to get names
            pass 
    return url_to_name_map

def insert_top_channels_to_db(channel_counts: Counter, channel_names: dict, top_n: int):
    """Inserts or updates the top N channels into the SQLite database."""
    if not channel_counts:
        logger.warning("No channel counts to process.")
        return

    top_channels = channel_counts.most_common(top_n)
    if not top_channels:
        logger.warning("Channel counts were non-empty, but failed to get top channels.")
        return
    
    logger.info(f"Identified top {len(top_channels)} channels based on watch frequency.")
    
    conn = None
    updated_count = 0
    inserted_count = 0
    error_count = 0

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        for url, view_count in top_channels:
            channel_name = channel_names.get(url, "Unknown") # Get name from map, default to Unknown
            
            try:
                # Use INSERT OR REPLACE to simplify logic - replaces existing row based on UNIQUE URL
                # Or use INSERT ON CONFLICT DO UPDATE for more control
                cursor.execute(
                    """INSERT INTO channels (name, url, views) VALUES (?, ?, ?) 
                       ON CONFLICT(url) DO UPDATE SET 
                       name=excluded.name, 
                       views=excluded.views, 
                       fetched_at=CURRENT_TIMESTAMP""",
                    (channel_name, url, view_count) 
                )
                # Check if insert or update happened (could check changes, but simpler to estimate)
                if cursor.rowcount > 0:
                     # Hard to tell if it was insert or update without querying first
                     # For simplicity, let's just track total successful operations
                     updated_count += 1 # Assume update/insert success
                # logger.info(f"Inserted/Updated channel: {channel_name} ({url}) with {view_count} views.")
            except sqlite3.Error as db_err:
                 logger.error(f"Database error processing channel {url}: {db_err}")
                 error_count += 1
            except Exception as e:
                logger.error(f"Error processing channel {url}: {e}")
                error_count += 1

        conn.commit()
        # The updated_count is really upsert_count here
        logger.info(f"Database update complete. Upserted: {updated_count} channels. Errors: {error_count}")

    except sqlite3.Error as e:
        logger.error(f"Database connection/commit error: {e}")
        if conn:
            conn.rollback() # Rollback changes on error
    finally:
        if conn:
            conn.close()

def main():
    logger.info("Starting channel ingestion from watch history...")
    
    # 1. Ensure database and table exist
    logger.info("Initializing database...")
    initialize_database() # Creates DB and tables if they don't exist

    # 2. Process watch history files
    logger.info(f"Processing watch history from {WATCH_HISTORY_DIR}...")
    channel_view_counts = process_watch_history(WATCH_HISTORY_DIR)
    
    if not channel_view_counts:
        logger.warning("No channel view counts generated from watch history. Exiting.")
        return
        
    # 3. Extract channel names (best effort)
    logger.info("Extracting channel names...")
    channel_name_map = extract_channel_names(WATCH_HISTORY_DIR)

    # 4. Insert top channels into the database
    logger.info(f"Inserting top {TOP_N_CHANNELS} channels into the database...")
    insert_top_channels_to_db(channel_view_counts, channel_name_map, TOP_N_CHANNELS)
    
    logger.info("Channel ingestion process finished.")

if __name__ == "__main__":
    main()
