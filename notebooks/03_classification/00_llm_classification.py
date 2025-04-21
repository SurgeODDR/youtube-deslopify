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
MAX_CONCURRENT_TASKS = 50
API_RETRY_DELAY = 5
# ---

CLASSIFICATION_PROMPT_TEMPLATE = """
Analyze the following YouTube video transcription to assess its suitability and quality for children.

**Transcription:**
```
{transcription_text}
```

**Evaluation Criteria:**

1.  **Language:** Identify the primary language. Is it English, Czech, or Dutch?
2.  **Coherence:** Does the content make sense? Does it follow a logical progression or tell a story? (Rate 1-5: 1=Nonsensical/Random, 5=Very Coherent/Structured)
3.  **Educational Value:** Does the content aim to teach something specific, encourage curiosity, or develop skills? (Rate 1-5: 1=None, 5=Highly Educational)
4.  **Engagement Quality:** How does the content engage the child? Is it through a compelling narrative/topic, or primarily through excessive bright colors, fast cuts, repetitive sounds, or sensory overload (often associated with "slop" content)? (Rate 1-5: 1=Likely Low-Quality/Hypnotic Engagement, 5=Engaging through Substance/Story)
5.  **Content Appropriateness:** Does the content focus on desirable themes? Avoid evaluating content solely focused on things like playing with slime, repetitive unboxing, or other potentially low-value/mindless activities. (Rate 1-5: 1=Likely Low-Value/"Slop" Content, 5=Appropriate & Constructive Themes)
6.  **Overall Quality:** Based on all the above, provide a holistic quality score. (Rate 1-5: 1=Very Poor Quality/Slop, 5=High Quality)

**Instructions:**

*   First, determine the language. If it is NOT English, Czech, or Dutch, stop evaluation and set all scores to 0, except `language_score` which should be 0.
*   If the language IS English, Czech, or Dutch, set `language_score` to 1 and proceed to evaluate and score all other criteria (1-5).
*   Provide a brief reasoning for your scores.
*   Output your analysis STRICTLY in the following JSON format:

```json
{{
  "language_detected": "string (e.g., 'English', 'Czech', 'Dutch', 'Other')",
  "language_score": "integer (0 or 1)",
  "coherence_score": "integer (0-5)",
  "educational_score": "integer (0-5)",
  "engagement_score": "integer (0-5)",
  "appropriateness_score": "integer (0-5)",
  "overall_score": "integer (0-5)",
  "reasoning": "string (brief explanation)"
}}
```
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
    language_detected=None, language_score=None, coherence_score=None, 
    educational_score=None, engagement_score=None, appropriateness_score=None, 
    overall_score=None, reasoning=None,
    model_used=None, processing_time=None, error_message=None
):
    """Inserts or updates classification status in DB (connects internally)."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        now = datetime.now()
        
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
            language_detected, language_score, coherence_score, educational_score,
            engagement_score, appropriateness_score, overall_score, reasoning,
            model_used, processing_time, status, error_message, now
        ))
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"DB error updating classification for {video_id}: {e}")
    finally:
        if conn:
            conn.close()

def parse_gemini_json_output(text_output: str) -> (dict | None, str | None):
    """Attempts to parse JSON from the Gemini output, handling potential markdown/extra text."""
    try:
        # Find the first '{' and the last '}' to extract the JSON block
        start_brace = text_output.find('{')
        end_brace = text_output.rfind('}')
        
        if start_brace == -1 or end_brace == -1 or end_brace < start_brace:
            return None, f"Could not find valid JSON braces in response: {text_output[:500]}"
            
        json_text = text_output[start_brace : end_brace + 1]
        
        # Parse the extracted text
        parsed_json = json.loads(json_text)
        
        # Basic validation (check for expected keys)
        expected_keys = ["language_detected", "language_score", "coherence_score", "educational_score", 
                         "engagement_score", "appropriateness_score", "overall_score", "reasoning"]
        if all(key in parsed_json for key in expected_keys):
            return parsed_json, None
        else:
            missing = [key for key in expected_keys if key not in parsed_json]
            return None, f"Parsed JSON missing expected keys: {missing}"
            
    except json.JSONDecodeError as e:
        # Include the attempted parse string in the error for debugging
        return None, f"Failed to decode JSON response: {e}. Attempted to parse: {json_text[:500]}"
    except Exception as e:
        return None, f"Unexpected error parsing JSON: {e}"

async def classify_transcription_gemini_async(transcription_text: str):
    """Classifies a transcription using the Gemini API (async)."""
    start_time = time.time()
    if not transcription_text:
        return None, "Transcription text is empty.", 0
    try:
        prompt = CLASSIFICATION_PROMPT_TEMPLATE.format(transcription_text=transcription_text)
        logger.info(f"Generating classification...")
        model = genai.GenerativeModel(MODEL_NAME)
        response = await model.generate_content_async(prompt)
        processing_time = time.time() - start_time
        
        try:
            response_text = response.text
        except ValueError as e:
            logger.warning(f"Response likely blocked. Feedback: {response.prompt_feedback}. Error: {e}")
            block_reason_msg = getattr(response.prompt_feedback, 'block_reason', 'Unknown')
            return None, f"Response likely blocked. Reason: {block_reason_msg}", processing_time
        except Exception as e:
             logger.error(f"Error accessing response text: {e}", exc_info=True)
             return None, f"Error accessing response text: {e}", processing_time

        if not response_text:
             if response.prompt_feedback.block_reason:
                  logger.warning(f"Response blocked. Reason: {response.prompt_feedback.block_reason}")
                  return None, f"Response blocked. Reason: {response.prompt_feedback.block_reason}", processing_time
             else:
                  logger.warning(f"Response empty but not blocked. Feedback: {response.prompt_feedback}")
                  return None, f"Response was empty. Feedback: {response.prompt_feedback}", processing_time

        parsed_data, parse_error = parse_gemini_json_output(response_text)
        if parse_error:
            logger.error(f"Error parsing classification JSON: {parse_error}")
            return None, parse_error, processing_time

        logger.info(f"Classification successful in {processing_time:.2f}s.")
        return parsed_data, None, processing_time

    except google_exceptions.GoogleAPIError as e:
         logger.error(f"Gemini API classification error: {e}")
         error_code = getattr(e, 'code', None)
         if error_code == 429 or "resource exhausted" in str(e).lower() or "rate limit" in str(e).lower():
              logger.warning(f"Rate limit hit during classification.")
              return None, "RATE_LIMIT_ERROR", time.time() - start_time
         return None, f"Gemini API error: {e}", time.time() - start_time
    except Exception as e:
        logger.error(f"Unexpected error during classification: {e}", exc_info=True)
        return None, f"Unexpected error: {e}", time.time() - start_time

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
            # No DB update
        elif error:
            logger.error(f"Failed to classify {video_id}: {error}")
            status = 'error'
            db_update_func = functools.partial(
                update_classification_status, video_id, transcription_id, status, 
                error_message=str(error)[:500], processing_time=proc_time, model_used=MODEL_NAME
            )
            await loop.run_in_executor(None, db_update_func)
        elif classification_data:
            logger.info(f"Successfully classified {video_id}.")
            status = 'success'
            db_update_func = functools.partial(
                update_classification_status, video_id, transcription_id, status,
                **classification_data, # Pass parsed dict as kwargs
                processing_time=proc_time, model_used=MODEL_NAME
            )
            await loop.run_in_executor(None, db_update_func)
        else:
            logger.error(f"Classification returned no data and no error for {video_id}. Marking as error.")
            status = 'error'
            db_update_func = functools.partial(
                update_classification_status, video_id, transcription_id, status,
                error_message="Gemini returned no data or error", processing_time=proc_time, model_used=MODEL_NAME
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
    logger.info(f"Rate Limit Hits (not retried in this run): {rate_limit_hits}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Process interrupted by user.")
