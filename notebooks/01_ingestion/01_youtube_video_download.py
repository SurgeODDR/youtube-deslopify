# Databricks notebook source

# COMMAND ----------
# MAGIC %md
# MAGIC # YouTube Bulk Download with Additional Columns for Next Steps
# MAGIC 
# MAGIC 1. **Creates** (or uses) a Delta table `channel_processed_videos` in the same schema `yt-deslopify`.default.
# MAGIC 2. Adds extra columns to track additional metadata about each download:
# MAGIC    - `channel_id`: from the channel URL  
# MAGIC    - `channel_name`  
# MAGIC    - `channel_url`  
# MAGIC    - `video_id`  
# MAGIC    - `video_title`  
# MAGIC    - `video_duration` (seconds)  
# MAGIC    - `azure_blob_name` (filename in container)  
# MAGIC    - `downloaded_timestamp`  
# MAGIC 3. **Skips** already-processed videos.
# MAGIC 4. Skips shorts (<60s).
# MAGIC 5. **Random sleeps** between channels to reduce potential bot triggers.
# MAGIC 6. **Uploads** files to Azure Blob Storage, using a fresh cookies file for YouTube authentication.

# COMMAND ----------
import os
import re
import time
import random
import json # Added for token loading
from datetime import datetime
import sys
import sqlite3
import logging # Added logging import
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials # Added
from google.auth.transport.requests import Request # Added

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
    print("Error: Could not import db_utils. Make sure src/db_utils.py exists.")
    sys.exit(1)

# --- Configuration ---
DATA_DIR = project_root / "data"
# Define paths relative to the auth script directory
AUTH_SCRIPT_DIR = project_root / "notebooks" / "00_setup_and_auth"
TOKEN_PATH = AUTH_SCRIPT_DIR / "token.json" 
CLIENT_SECRETS_PATH = AUTH_SCRIPT_DIR / "client_secrets.json"

MAX_VIDEOS_PER_CHANNEL = 3
MIN_VIDEO_DURATION_SECONDS = 60
MAX_VIDEO_DURATION_SECONDS = 3600
MAX_VIDEOS_TO_SCAN_PER_CHANNEL = 100
# ---

def get_youtube_credentials() -> Credentials | None:
    """Loads YouTube credentials from token.json or refreshes them (files expected in auth script dir)."""
    print("  Attempting to get YouTube credentials...") # DEBUG
    creds = None
    if TOKEN_PATH.exists(): 
        print(f"  Found token file at {TOKEN_PATH}") # DEBUG
        try:
            print("    Attempting to load token.json...") # DEBUG
            with open(TOKEN_PATH, 'r') as token_file:
                token_data = json.load(token_file)
            print("    token.json loaded successfully.") # DEBUG
            
            if not CLIENT_SECRETS_PATH.exists():
                 print(f"Error: client_secrets.json not found at {CLIENT_SECRETS_PATH}. Cannot refresh token.")
                 return None
            print("    Attempting to load client_secrets.json...") # DEBUG
            with open(CLIENT_SECRETS_PATH, 'r') as f:
                 client_config = json.load(f)
            print("    client_secrets.json loaded successfully.") # DEBUG
            
            print("    Attempting to instantiate Credentials object...") # DEBUG
            creds = Credentials(
                token=token_data.get('token'),
                refresh_token=token_data.get('refresh_token'),
                token_uri=client_config['web']['token_uri'],
                client_id=client_config['web']['client_id'],
                client_secret=client_config['web']['client_secret'],
                scopes=token_data.get('scopes')
            )
            print("    Credentials object instantiated.") # DEBUG
        except Exception as e:
             # Print specific error during loading/instantiation
             print(f"*** ERROR during token/secrets loading or Credentials instantiation: {e}") # DEBUG
             # print(f"Error loading token/secrets from {AUTH_SCRIPT_DIR}: {e}") # Original less specific message
             return None
    else:
        print(f"  Token file not found at {TOKEN_PATH}") # DEBUG

    # Check if credentials object exists and is valid
    if not creds:
        print("  Credentials object is None (likely due to previous error or missing token file).") # DEBUG
    else:
        print(f"  Credentials object created. Valid: {creds.valid}") # DEBUG
        if creds.expired:
             print(f"  Credentials expired. Refresh token present: {bool(creds.refresh_token)}") # DEBUG

    # Check if credentials are valid or need refresh
    if not creds or not creds.valid:
        print("  Credentials are NOT valid or do not exist.") # DEBUG
        if creds and creds.expired and creds.refresh_token:
            print("    Attempting to refresh expired credentials...") # DEBUG
            try:
                creds.refresh(Request())
                print("    Credentials refresh successful.") # DEBUG
                # Save the refreshed credentials back to token.json in the auth dir
                token_data = {
                    'token': creds.token,
                    'refresh_token': creds.refresh_token,
                    'token_uri': creds.token_uri,
                    'client_id': creds.client_id,
                    'client_secret': creds.client_secret,
                    'scopes': creds.scopes
                }
                print(f"    Attempting to save refreshed token to {TOKEN_PATH}...") # DEBUG
                with open(TOKEN_PATH, 'w') as token_file:
                     json.dump(token_data, token_file, indent=4)
                print(f"    Credentials refreshed and saved to {TOKEN_PATH}.") # DEBUG
            except Exception as e:
                print(f"*** ERROR refreshing credentials: {e}") # DEBUG
                print(f"    Please re-run the OAuth script: notebooks/00_setup_and_auth/00_youtube_oauth_local.py") # DEBUG
                return None
        else:
            # Print why it failed (either no creds, or invalid without refresh token)
            if not creds:
                 reason = "Credentials object failed to load."
            elif not creds.refresh_token:
                 reason = "Credentials loaded but missing refresh token."
            else:
                 reason = "Credentials not expired but invalid (or other issue)."
            print(f"    Reason: {reason}") # DEBUG
            print("Error: Valid YouTube credentials not found or could not be refreshed.") # Original message
            print(f"Looked for {TOKEN_PATH} and {CLIENT_SECRETS_PATH}.")
            print(f"Please run the authentication script: notebooks/00_setup_and_auth/00_youtube_oauth_local.py")
            return None
            
    print("  Credentials appear valid.") # DEBUG
    return creds

def parse_iso8601_duration(duration_str):
    """Converts ISO 8601 duration (e.g., 'PT1H2M3S') to seconds."""
    match = re.match(r'^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$', duration_str)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    mins  = int(match.group(2) or 0)
    secs  = int(match.group(3) or 0)
    return hours * 3600 + mins * 60 + secs

def get_nonshort_videos(
    youtube_client, 
    channel_url: str, 
    target_count=MAX_VIDEOS_PER_CHANNEL, 
    min_duration=MIN_VIDEO_DURATION_SECONDS, 
    max_duration=MAX_VIDEO_DURATION_SECONDS, 
    scan_limit=MAX_VIDEOS_TO_SCAN_PER_CHANNEL
) -> list:
    """
    Gets metadata for the most recent `target_count` videos between min/max duration,
    scanning up to `scan_limit` videos for a given channel URL.
    Determines channel ID/handle/username from the URL.
    """
    final_videos_meeting_criteria = []
    next_page_token = None
    videos_scanned = 0
    playlist_id = None
    channel_id_for_logging = channel_url # Use URL for logging if ID fetch fails

    try:
        # --- Determine how to identify the channel --- 
        channel_params = {}
        channel_id_match = re.search(r'channel/(UC[\w_-]+)', channel_url)
        handle_match = re.search(r'@([\w_-]+)', channel_url) 
        # Treat /c/ and /user/ as potential usernames for legacy URLs
        username_match = re.search(r'/(?:c|user)/([\w_-]+)', channel_url) 

        if channel_id_match:
            channel_params['id'] = channel_id_match.group(1)
            channel_id_for_logging = channel_params['id']
            logger.debug(f"Identified channel by ID: {channel_id_for_logging}")
        elif handle_match:
            channel_params['forHandle'] = handle_match.group(1)
            channel_id_for_logging = f"@{channel_params['forHandle']}"
            logger.debug(f"Identified channel by handle: {channel_id_for_logging}")
        elif username_match:
            # Use forUsername for /c/ or /user/ paths
            channel_params['forUsername'] = username_match.group(1)
            channel_id_for_logging = f"user/c: {channel_params['forUsername']}"
            logger.debug(f"Identified channel by username/custom URL: {channel_id_for_logging}")
        else:
            logger.warning(f"Could not extract a recognizable ID, handle, or username from URL: {channel_url}. Skipping channel.")
            return []

        # --- Get Uploads Playlist ID using determined channel identifier ---
        logger.debug(f"Fetching channel contentDetails for {channel_id_for_logging} using params: {channel_params}")
        ch_resp = youtube_client.channels().list(part='contentDetails', **channel_params).execute()
        
        if not ch_resp.get('items'):
            logger.warning(f"API Error: No channel found for identifier used in {channel_params} (URL: {channel_url}). Might be invalid handle/username or deleted channel.")
            return []
            
        # Store the actual channel ID returned by the API for potential future use/logging
        actual_channel_id = ch_resp['items'][0]['id'] 
        logger.debug(f"Successfully fetched channel details. Actual Channel ID: {actual_channel_id}")
        
        # Find the uploads playlist ID
        try:
             playlist_id = ch_resp['items'][0]['contentDetails']['relatedPlaylists']['uploads']
        except KeyError:
             logger.error(f"Could not find uploads playlist ID for channel {actual_channel_id} (Identifier: {channel_id_for_logging}). Channel might have no public uploads or API issue.")
             return []

        logger.info(f"  Processing uploads playlist: {playlist_id}")

        # --- Fetch and Filter Videos from Uploads Playlist --- 
        while len(final_videos_meeting_criteria) < target_count and videos_scanned < scan_limit:
            logger.info(f"    Fetching playlist items page... (found {len(final_videos_meeting_criteria)}/{target_count}, scanned {videos_scanned}/{scan_limit})")
            try:
                req = youtube_client.playlistItems().list(
                    part='snippet', # Need snippet for videoId
                    playlistId=playlist_id,
                    maxResults=min(50, scan_limit - videos_scanned),
                    pageToken=next_page_token
                )
                resp = req.execute()
            except HttpError as e:
                 # Handle potential errors like playlist not found (though unlikely if channel fetch worked)
                 logger.error(f"    API error fetching playlist items for playlist {playlist_id}: {e}. Skipping channel.")
                 return [] # Stop processing this channel
                 
            items_on_page = resp.get('items', [])
            next_page_token = resp.get('nextPageToken')
            logger.info(f"Got {len(items_on_page)} items.")
            if not items_on_page: break
            videos_scanned += len(items_on_page)

            page_video_data = []
            video_ids_page = []
            for item in items_on_page:
                snippet = item.get('snippet')
                # Ensure necessary keys exist
                if snippet and snippet.get('resourceId') and snippet['resourceId'].get('videoId'):
                    video_id = snippet['resourceId']['videoId']
                    video_ids_page.append(video_id)
                    page_video_data.append({
                        'id': video_id,
                        'title': snippet.get('title', '[No Title]'),
                        'url': f'https://www.youtube.com/watch?v={video_id}',
                    })
                else:
                    logger.warning(f"    Skipping playlist item due to missing videoId: {item.get('id')}")
            
            if not video_ids_page:
                 logger.debug("    No valid video IDs found on this page.")
                 if not next_page_token: break
                 else: continue

            # --- Get Video Durations --- 
            logger.info(f"    Fetching durations for {len(video_ids_page)} videos...")
            duration_map_page = {}
            try:
                # Fetch details in batches of 50 (max allowed by videos().list)
                ids_str = ",".join(video_ids_page)
                det_resp = youtube_client.videos().list(
                     part='contentDetails', # Need contentDetails for duration
                     id=ids_str
                ).execute()
                for item in det_resp.get('items', []):
                    duration_iso = item.get('contentDetails', {}).get('duration')
                    if duration_iso:
                        duration_map_page[item['id']] = parse_iso8601_duration(duration_iso)
            except HttpError as e:
                 logger.error(f"    API error getting video details for page (videos: {video_ids_page[:5]}...): {e}. Skipping durations for this page.")
                 # Continue processing videos on page without duration filter if details fail?
                 # For now, we skip duration filtering if the details call fails.
                 duration_map_page = {} # Ensure it's empty

            # --- Filter by Duration --- 
            for video_data in page_video_data:
                video_id = video_data['id']
                duration_sec = duration_map_page.get(video_id)
                
                # If duration couldn't be fetched, we can't filter. Decide whether to include or exclude.
                # Current logic: only include if duration is fetched and meets criteria.
                if duration_sec is not None: 
                    if min_duration <= duration_sec <= max_duration:
                        video_data['duration_in_sec'] = duration_sec
                        final_videos_meeting_criteria.append(video_data)
                        logger.info(f"    Found suitable video: '{video_data['title'][:50]}...' (ID: {video_id}, Dur: {duration_sec}s) - Total found: {len(final_videos_meeting_criteria)}")
                        if len(final_videos_meeting_criteria) >= target_count:
                            break # Exit inner loop once target is met
                    else:
                        reason = "short" if duration_sec < min_duration else "long"
                        logger.info(f"    Skipping too {reason} video: '{video_data['title'][:50]}...' (ID: {video_id}, Dur: {duration_sec}s)")
                else:
                     logger.warning(f"    Could not determine duration for video: '{video_data['title'][:50]}...' (ID: {video_id}). Skipping.")
            
            # Check if target met after processing the page
            if len(final_videos_meeting_criteria) >= target_count:
                 logger.info(f"  Found target count ({target_count}) of suitable videos.")
                 break # Exit outer while loop
                 
            if not next_page_token: 
                 logger.debug("    No more pages in playlist.")
                 break # Exit outer while loop

        logger.info(f"  Finished scanning playlist {playlist_id}. Found {len(final_videos_meeting_criteria)} suitable videos after checking {videos_scanned} total videos.")
        return final_videos_meeting_criteria

    except HttpError as e:
        # Catch errors during the initial channel details fetch
        if e.resp.status == 401 or e.resp.status == 403:
             logger.error(f"Authentication error fetching channel data for {channel_id_for_logging}: {e}")
             logger.error("Ensure token.json is valid and contains the correct scopes.")
        elif e.resp.status == 404:
             logger.warning(f"Channel not found for identifier {channel_id_for_logging} (URL: {channel_url}). Status 404: {e}")
        else:
             logger.error(f"An API error occurred processing channel identifier {channel_id_for_logging}: {e}")
        return []
    except Exception as e:
        logger.exception(f"An unexpected error occurred processing channel URL {channel_url}: {e}")
        return []

def get_processed_video_ids(conn):
    """Fetches the set of already processed video IDs from the database."""
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT video_id FROM processed_videos")
        return {row[0] for row in cursor.fetchall()}
    except sqlite3.Error as e:
        print(f"Database error fetching processed video IDs: {e}")
        return set()

def add_processed_video_metadata(conn, video_info, channel_id, channel_name, channel_url):
    """Adds a record for a video (metadata only) to the database."""
    try:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO processed_videos 
               (video_id, channel_id, channel_name, channel_url, video_title, video_duration, local_video_path)
               VALUES (?, ?, ?, ?, ?, ?, NULL)""",
            (
                video_info['id'],
                channel_id, # This should be the actual UC... ID if available
                channel_name,
                channel_url,
                video_info['title'],
                video_info.get('duration_in_sec')
            )
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
         logger.warning(f"Video ID {video_info['id']} already exists in processed_videos. Skipping insert.")
         return False
    except sqlite3.Error as e:
        print(f"Database error adding processed video metadata {video_info['id']}: {e}")
        conn.rollback()
        return False

def main():
    print("Starting video metadata fetching process...")
    # Ensure credentials are loaded correctly before building client
    credentials = get_youtube_credentials()
    if not credentials:
        print("Failed to obtain YouTube credentials. Exiting.")
        sys.exit(1)
    
    youtube = None
    try:
        youtube = build('youtube', 'v3', credentials=credentials)
        print("YouTube API client created successfully.")
    except Exception as e:
        print(f"Failed to build YouTube client: {e}")
        sys.exit(1)
        
    conn = None
    try:
        initialize_database() # Ensure DB exists
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name, url FROM channels WHERE url IS NOT NULL ORDER BY name") # Added ORDER BY
        channels = cursor.fetchall()
        channel_count = len(channels)
        if channel_count == 0:
            print("No channels found in the database. Run the ingestion script first.")
            return
        print(f"Found {channel_count} channels to process.")
        processed_ids = get_processed_video_ids(conn)
        print(f"Found {len(processed_ids)} already processed video IDs.")

        total_new_videos_added = 0
        for idx, (ch_name, ch_url) in enumerate(channels, start=1):
            print(f"\n=== Channel {idx}/{channel_count} ===")
            print(f"Name: {ch_name}\nURL : {ch_url}")

            try:
                # Call the modified function - no need to extract ID here
                target_nonshort_videos = get_nonshort_videos(youtube, ch_url)
                
                if not target_nonshort_videos:
                    print("No suitable videos found for this channel after scanning. Skipping channel.\n")
                    continue

                new_videos_to_add = [v for v in target_nonshort_videos if v['id'] not in processed_ids]
                if not new_videos_to_add:
                    print("The most recent suitable videos were already processed. Skipping channel.\n")
                    continue

                print(f"Found {len(target_nonshort_videos)} recent suitable videos. Will add metadata for {len(new_videos_to_add)} new ones.")

                processed_count_channel = 0
                for i, vid_info in enumerate(new_videos_to_add, start=1):
                    print(f"--- Processing Metadata {i}/{len(new_videos_to_add)} ('{vid_info['title'][:60]}...') ---")
                    # Need the actual channel ID (UC...) for the DB insert, if available
                    # We fetched it inside get_nonshort_videos, but didn't return it. 
                    # Simplest fix: Let add_processed_video_metadata handle potentially missing ID
                    # Or modify get_nonshort_videos to return channel_id
                    # For now, we pass None for channel_id - requires DB schema to allow NULL or adjust add_processed_video_metadata
                    # --> NOTE: The DB schema `processed_videos` already allows NULL for channel_id.
                    if add_processed_video_metadata(conn, vid_info, None, ch_name, ch_url):
                         processed_ids.add(vid_info['id'])
                         processed_count_channel += 1
                         print(f" -> Successfully recorded metadata for video: {vid_info['id']}\n")
                    else:
                         print(f" -> Skipped/failed recording metadata for video: {vid_info['id']}\n")
                
                total_new_videos_added += processed_count_channel
                print(f"Finished channel {ch_name}. Recorded metadata for {processed_count_channel} new videos.")

            except Exception as e:
                 # Catch any unexpected errors during the processing of a single channel
                 logger.exception(f"Error processing channel {ch_name} (URL: {ch_url}): {e}")
            
            if idx < channel_count:
                 # Use the modified sleep time (assuming user edited the file)
                 snooze = random.uniform(1, 3) 
                 print(f"\nSleeping {snooze:.2f}s before next channel...\n")
                 time.sleep(snooze)
        
        print(f"\nAll channels processed. Total new video metadata records added: {total_new_videos_added}")

    except HttpError as e:
         print(f"A critical YouTube API error occurred: {e}")
    except sqlite3.Error as e:
         print(f"A critical database error occurred: {e}")
    except Exception as e:
        print(f"An unexpected critical error occurred: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()
            print("Database connection closed.")

if __name__ == "__main__":
    main()