import httpx
import os
import tenacity
import logging
from dotenv import load_dotenv

load_dotenv() # Load environment variables from .env

logger = logging.getLogger(__name__)
API_URL = "https://api.perplexity.ai/chat/completions"
MODEL = "sonar-deep-research"
PERPLEXITY_API_KEY = os.getenv('PERPLEXITY_API_KEY')

if not PERPLEXITY_API_KEY:
    logger.warning("PERPLEXITY_API_KEY not found in environment variables.")
    # You might want to raise an error or handle this case more robustly
    # raise ValueError("PERPLEXITY_API_KEY not set")

HEADERS = {
    "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json", # Added Accept header
}

# Helper to decide which exceptions should trigger a retry
def _is_retryable_http_error(exc: Exception) -> bool:  # pragma: no cover
    """Return True if *exc* is an httpx.HTTPStatusError with a retry‑worthy status.

    We currently retry on 429 (rate‑limit) and the most common transient 5xx errors.
    This helper can easily be extended with additional logic (e.g. inspect
    ``Retry‑After`` headers) without touching the tenacity decorator below.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response is not None and exc.response.status_code in {429, 500, 502, 503, 504}
    return False

@tenacity.retry(
    # Retry on HTTP status errors that are considered transient as well as timeouts
    retry=(
        tenacity.retry_if_exception(_is_retryable_http_error)
        | tenacity.retry_if_exception_type(httpx.TimeoutException)
    ),
    # Apply exponential back‑off with full jitter to prevent thundering‑herd
    wait=tenacity.wait_random_exponential(multiplier=2, max=60),
    # Give it a couple more chances before giving up
    stop=tenacity.stop_after_attempt(8),
    # Log each retry
    before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def deep_research(prompt: str, max_tokens=8000) -> str | None:
    """
    Call Perplexity's Deep Research API and return the assistant's raw text content.

    Args:
        prompt: The user prompt for the research.
        max_tokens: The maximum number of tokens for the completion.

    Returns:
        The raw text content from the assistant, or None if the API key is missing or an error occurs.
    """
    if not PERPLEXITY_API_KEY:
        logger.error("Cannot call Perplexity API: API key is missing.")
        return None

    data = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    try:
        async with httpx.AsyncClient(timeout=600.0) as client: # Increased timeout
            logger.debug(f"Sending request to Perplexity: {data}")
            resp = await client.post(API_URL, headers=HEADERS, json=data)
            logger.debug(f"Received response from Perplexity: {resp.status_code}")
            resp.raise_for_status() # Raise exception for 4xx or 5xx errors
            
            response_json = resp.json()
            logger.debug(f"Perplexity JSON response: {response_json}")

            if response_json.get("choices") and len(response_json["choices"]) > 0:
                content = response_json["choices"][0].get("message", {}).get("content")
                if content:
                    logger.info(f"Successfully received content from Perplexity for prompt starting with: '{prompt[:50]}...'" )
                    return content
                else:
                    logger.warning(f"Perplexity response missing content for prompt: '{prompt[:50]}...'. Response: {response_json}")
                    return None # Or handle empty content case differently
            else:
                 logger.warning(f"Perplexity response missing 'choices' or choices is empty for prompt: '{prompt[:50]}...'. Response: {response_json}")
                 return None # Or handle no choices case

    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code if e.response else None
        logger.warning(
            "HTTP error %s from Perplexity for prompt '%s…': %s",
            status_code if status_code is not None else "<no‑response>",
            prompt[:50],
            e,
            exc_info=True,
        )

        # If this status code is considered transient, bubble the error up so that
        # the retry decorator can kick in. Otherwise, treat it as permanent and
        # return None so the caller can handle the failure gracefully.
        if _is_retryable_http_error(e):
            raise  # Retryable – hand control back to tenacity
        else:
            if status_code == 401:
                logger.error("Perplexity API key might be invalid or expired.")
            return None
    except httpx.TimeoutException as e:
        logger.warning(
            "Timeout when calling Perplexity for prompt '%s…': %s",
            prompt[:50],
            e,
            exc_info=True,
        )
        raise  # Hand control back to tenacity
    except Exception as e:
        logger.error(f"Unexpected error calling Perplexity API for prompt '{prompt[:50]}...': {e}", exc_info=True)
        return None # Signal failure 