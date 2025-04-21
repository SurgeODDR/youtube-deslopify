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

@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=1, min=2, max=30),
    stop=tenacity.stop_after_attempt(6),
    retry=tenacity.retry_if_exception_type((httpx.HTTPStatusError, httpx.TimeoutException)), # Retry on server errors and timeouts
    reraise=True
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
        logger.error(f"HTTP error calling Perplexity API for prompt '{prompt[:50]}...': {e.response.status_code} - {e.response.text}", exc_info=True)
        # Specific handling for 401 Unauthorized
        if e.response.status_code == 401:
             logger.error("Perplexity API key might be invalid or expired.")
        # Specific handling for 429 Too Many Requests (though tenacity should handle retries)
        elif e.response.status_code == 429:
             logger.warning("Perplexity API rate limit hit (status 429).")
        # Re-raise after logging might be handled by tenacity depending on config
        # For now, return None to signal failure after retries
        return None
    except httpx.TimeoutException as e:
        logger.error(f"Timeout calling Perplexity API for prompt '{prompt[:50]}...': {e}", exc_info=True)
        return None # Signal failure after retries
    except Exception as e:
        logger.error(f"Unexpected error calling Perplexity API for prompt '{prompt[:50]}...': {e}", exc_info=True)
        return None # Signal failure 