import os
import time
import sys
import sqlite3
from pathlib import Path
from datetime import datetime
import json
import random
from dotenv import load_dotenv
import asyncio
import logging
import functools

import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
from pydantic import BaseModel, Field, ValidationError, TypeAdapter
from typing import Optional

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
MODEL_NAME = "gemini-1.5-pro-latest" # Use latest Pro model
MAX_CONCURRENT_TASKS = 50
API_RETRY_DELAY = 5
# ---

# --- Pydantic Schema Definition for Classification --- (Aligns with DB table)
class ClassificationResult(BaseModel):
    language_detected: str = Field(description="Primary language detected (e.g., 'English', 'Czech', 'Dutch', 'Other')")
    language_score: int = Field(description="0 if language is Other, 1 if English/Czech/Dutch")
    coherence_score: int = Field(ge=0, le=5, description="Rating (0-5) for logical flow and sense")
    educational_score: int = Field(ge=0, le=5, description="Rating (0-5) for teaching value or skill development")
    engagement_score: int = Field(ge=0, le=5, description="Rating (0-5) assessing engagement quality (Substance vs. Sensory Overload)")
    appropriateness_score: int = Field(ge=0, le=5, description="Rating (0-5) for constructive themes vs. low-value activities")
    overall_score: int = Field(ge=0, le=5, description="Holistic quality score (0-5)")
    reasoning: str = Field(description="Brief textual explanation for the scores")

# --- Updated Prompt for JSON Mode ---
CLASSIFICATION_PROMPT_TEMPLATE = """
Analyze the following YouTube video transcription to assess its suitability and quality for children based on the criteria below. 
Return your analysis as a JSON object conforming to the provided schema.

**Transcription:**
```
{transcription_text}
```

**Evaluation Criteria & Instructions:**

1.  **Language Detection:** Identify the primary language. Set `language_detected` to the language name (e.g., 'English', 'Czech', 'Dutch', 'Other'). Set `language_score` to 1 if the detected language is English, Czech, or Dutch, otherwise set it to 0.
2.  **Stop if Not Target Language:** If `language_score` is 0, set all other score fields (`coherence_score`, `educational_score`, `engagement_score`, `appropriateness_score`, `overall_score`) to 0 and provide a brief `reasoning` indicating the language mismatch. Do not evaluate further.
3.  **Evaluate if Target Language:** If `language_score` is 1, proceed to evaluate the following criteria and assign scores from 1 to 5:
    *   `coherence_score`: Does the content make sense and follow a logical progression? (1=Nonsensical, 5=Very Coherent)
    *   `educational_score`: Does it aim to teach, encourage curiosity, or develop skills? (1=None, 5=Highly Educational)
    *   `engagement_score`: How does it engage? Through compelling narrative/topic (high score) or primarily through excessive sensory stimulation (low score)? (1=Likely Low-Quality/Hypnotic, 5=Engaging through Substance)
    *   `appropriateness_score`: Does it focus on desirable themes? Avoid rating highly content focused solely on low-value activities (e.g., slime, repetitive unboxing). (1=Likely Low-Value/"Slop", 5=Appropriate & Constructive)
    *   `overall_score`: Your holistic quality assessment based on all criteria. (1=Very Poor/Slop, 5=High Quality)
4.  **Reasoning:** Provide a concise `reasoning` explaining your scores.
"""

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

def get_pending_transcriptions(conn):
    """Gets video_id and transcription_text for ALL videos that haven't been successfully classified."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 
            t.video_id,
            t.id as transcription_id,
            t.transcription_text
        FROM transcriptions t
        LEFT JOIN classifications c ON t.video_id = c.video_id
        WHERE t.status = 'success'
          AND t.transcription_text IS NOT NULL 
          AND t.transcription_text != ''
          AND (c.video_id IS NULL OR c.status != 'success')
    """)
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]

def update_classification_status(
    video_id, transcription_id, status, 
    classification_data: Optional[dict] = None, # Pass the dict here
    model_used=None, processing_time=None, error_message=None
):
    """Inserts or updates classification status in DB (connects internally)."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        now = datetime.now()

        # Prepare data for insertion/update
        # Start with default None or 0 values matching the schema
        data_to_insert = {
            'language_detected': None,
            'language_score': 0,
            'coherence_score': 0,
            'educational_score': 0,
            'engagement_score': 0,
            'appropriateness_score': 0,
            'overall_score': 0,
            'reasoning': None
        }
        if classification_data: # If valid data was parsed
            data_to_insert.update(classification_data)

        cursor.execute("""
            INSERT INTO classifications (
                video_id, transcription_id, 
                language_detected, language_score, coherence_score, educational_score, 
                engagement_score, appropriateness_score, overall_score, reasoning, 
                model_used, processing_time_sec, status, error_message, processed_timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                transcription_id = excluded.transcription_id,
                language_detected = excluded.language_detected,
                language_score = excluded.language_score,
                coherence_score = excluded.coherence_score,
                educational_score = excluded.educational_score,
                engagement_score = excluded.engagement_score,
                appropriateness_score = excluded.appropriateness_score,
                overall_score = excluded.overall_score,
                reasoning = excluded.reasoning,
                model_used = excluded.model_used,
                processing_time_sec = excluded.processing_time_sec,
                status = excluded.status,
                error_message = excluded.error_message,
                processed_timestamp = excluded.processed_timestamp
        """, (
            video_id,
            transcription_id,
            data_to_insert['language_detected'], data_to_insert['language_score'], data_to_insert['coherence_score'], data_to_insert['educational_score'],
            data_to_insert['engagement_score'], data_to_insert['appropriateness_score'], data_to_insert['overall_score'], data_to_insert['reasoning'],
            model_used, processing_time, status, error_message, now
        ))
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"DB error updating classification for {video_id}: {e}")
    finally:
        if conn:
            conn.close()

async def classify_transcription_gemini_async(transcription_text: str):
    """Classifies a transcription using the Gemini API with JSON mode (async)."""
    start_time = time.time()
    if not transcription_text:
        return None, "Transcription text is empty.", 0
    
    classification_result = None # Initialize
    error_message = None
    processing_time = 0

    try:
        prompt = CLASSIFICATION_PROMPT_TEMPLATE.format(transcription_text=transcription_text)
        logger.info(f"Generating classification...")
        model = genai.GenerativeModel(MODEL_NAME)
        response = await model.generate_content_async(
            prompt,
            generation_config=genai.types.GenerationConfig(
                response_schema=ClassificationResult,
                response_mime_type="application/json",
            )
        )
        processing_time = time.time() - start_time
        logger.debug(f"Raw Gemini classification response object: {response}")

        if not response.candidates:
             if response.prompt_feedback.block_reason:
                  block_reason = response.prompt_feedback.block_reason.name
                  error_message = f"Response blocked. Reason: {block_reason}"
                  logger.warning(f"Classification response blocked. Reason: {block_reason}")
             else:
                  error_message = f"Response missing candidates. Feedback: {response.prompt_feedback}"
                  logger.warning(f"Classification response missing candidates. Feedback: {response.prompt_feedback}")
             return None, error_message, processing_time

        # Access the response text which should contain the JSON string
        response_text = response.text
        logger.debug(f"Gemini classification response text (expecting JSON): {response_text}")
        
        # Parse and validate using the Pydantic model
        validated_result = ClassificationResult.model_validate_json(response_text)
        # Convert the validated Pydantic model to a dictionary
        classification_result = validated_result.model_dump()
        logger.info(f"Classification successful and validated in {processing_time:.2f}s.")

    except json.JSONDecodeError as e:
         processing_time = time.time() - start_time
         error_message = f"Failed to decode JSON response: {e}. Response text: {response_text[:500]}"
         logger.error(error_message)
    except ValidationError as e:
        processing_time = time.time() - start_time
        error_message = f"Pydantic validation failed for classification: {e}. Response text: {response_text[:500]}"
        logger.error(error_message)
    except ValueError as e:
         # Catch potential errors if response.text is accessed when blocked
         processing_time = time.time() - start_time
         error_message = f"Value error accessing response (likely blocked): {e}"
         logger.warning(f"{error_message}. Feedback: {getattr(response, 'prompt_feedback', 'N/A')}")
    except google_exceptions.GoogleAPIError as e:
         processing_time = time.time() - start_time
         error_code = getattr(e, 'code', None)
         if error_code == 429 or "resource exhausted" in str(e).lower() or "rate limit" in str(e).lower():
              error_message = "RATE_LIMIT_ERROR"
              logger.warning(f"Rate limit hit during classification.")
         else:
            error_message = f"Gemini API error: {e}"
            logger.error(f"Gemini API classification error: {e}")
    except Exception as e:
        processing_time = time.time() - start_time
        error_message = f"Unexpected error: {e}"
        logger.error(f"Unexpected error during classification: {e}", exc_info=True)
        
    return classification_result, error_message, processing_time

async def process_classification_task_async(trans_info: dict, semaphore: asyncio.Semaphore):
    """Async worker task for classifying one transcription."""
    video_id = trans_info['video_id']
    transcription_id = trans_info['transcription_id']
    transcription_text = trans_info.get('transcription_text')
    loop = asyncio.get_running_loop()

    async with semaphore:
        logger.info(f"Starting classification task for video: {video_id} (Trans ID: {transcription_id})")
        classification_data, error, proc_time = await classify_transcription_gemini_async(transcription_text)
        status = 'unknown'

        # Handle results and update DB
        if error == "RATE_LIMIT_ERROR":
            logger.warning(f"Rate limit hit for classification task {video_id}. Sleeping.")
            await asyncio.sleep(API_RETRY_DELAY * 2)
            status = 'rate_limit'
            # Optionally update DB status to rate_limit here if needed
            # db_update_func = functools.partial(
            #     update_classification_status, video_id, transcription_id, status, 
            #     model_used=MODEL_NAME, processing_time=proc_time, error_message=error
            # )
            # await loop.run_in_executor(None, db_update_func)
        elif error:
            logger.error(f"Failed to classify {video_id}: {error}")
            status = 'error'
            db_update_func = functools.partial(
                update_classification_status, video_id, transcription_id, status, 
                model_used=MODEL_NAME, processing_time=proc_time, error_message=str(error)[:500]
            )
            await loop.run_in_executor(None, db_update_func)
        elif classification_data: # Ensure data is not None
            logger.info(f"Successfully classified {video_id}.")
            status = 'success'
            db_update_func = functools.partial(
                update_classification_status, video_id, transcription_id, status,
                classification_data=classification_data, # Pass parsed dict
                model_used=MODEL_NAME, processing_time=proc_time
            )
            await loop.run_in_executor(None, db_update_func)
        else:
            # This case should ideally be covered by specific error checks above,
            # but as a fallback if classification_data is None without a specific error captured.
            logger.error(f"Classification returned no data and no specific error for {video_id}. Marking as error.")
            status = 'error'
            db_update_func = functools.partial(
                update_classification_status, video_id, transcription_id, status,
                model_used=MODEL_NAME, processing_time=proc_time, error_message="Gemini returned no data or error"
            )
            await loop.run_in_executor(None, db_update_func)

        await asyncio.sleep(random.uniform(0.2, 0.8)) # Shorter delay for classification potentially

    return video_id, status

async def main():
    logger.info("Starting transcription classification process - ASYNCIO...")

    if not configure_gemini():
        sys.exit(1)

    logger.info("Initializing database...")
    initialize_database()

    conn = None
    transcriptions_to_classify = []
    try:
        conn = get_db_connection()
        transcriptions_to_classify = get_pending_transcriptions(conn)
    except sqlite3.Error as e:
        logger.error(f"Failed to get pending transcriptions from DB: {e}")
        return
    finally:
        if conn:
            conn.close()
        
    if not transcriptions_to_classify:
        logger.info("No new transcriptions found to classify.")
        return
            
    logger.info(f"Found {len(transcriptions_to_classify)} transcriptions to classify. Starting async processing with concurrency limit {MAX_CONCURRENT_TASKS}...")

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
    tasks = []
    for trans_info in transcriptions_to_classify:
        tasks.append(process_classification_task_async(trans_info, semaphore))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Process results summary
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

    logger.info("Async classification processing finished.")
    if rate_limit_hits > 0:
         logger.warning(f"Encountered {rate_limit_hits} rate limit errors during classification. Some videos may need reprocessing.")
            
    logger.info(f"\nClassification process finished.")
    logger.info(f"Successfully classified: {total_processed_count}")
    logger.info(f"Errors: {total_error_count}")
    logger.info(f"Rate Limit Hits (optional DB update): {rate_limit_hits}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Process interrupted by user.")
