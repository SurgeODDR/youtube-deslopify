import google.generativeai as genai
import os
import asyncio
import json
import logging
from dotenv import load_dotenv
from pydantic import BaseModel, Field, HttpUrl, ValidationError, TypeAdapter
from typing import List, Tuple, Optional

load_dotenv() # Load environment variables from .env

logger = logging.getLogger(__name__)

# --- Configuration ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = "gemini-1.5-flash-latest" # Use the latest Flash model available via API

if not GEMINI_API_KEY:
    logger.warning("GEMINI_API_KEY not found in environment variables for llm_utils.")
else:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        logger.info(f"Gemini API configured successfully in llm_utils for model: {MODEL_NAME}")
    except Exception as e:
        logger.error(f"Error configuring Gemini API in llm_utils: {e}")
        # Decide if this should be a fatal error

# --- Pydantic Schema Definition ---
class ChannelInfo(BaseModel):
    channel_name: str = Field(..., description="The name of the YouTube channel")
    channel_url: HttpUrl = Field(..., description="The full canonical URL of the YouTube channel (e.g., https://www.youtube.com/channel/UC... or https://www.youtube.com/@handle)")

# Define a TypeAdapter for a list of ChannelInfo objects
ChannelListAdapter = TypeAdapter(List[ChannelInfo])

# --- Prompt for JSON Mode ---
# Updated prompt to work with JSON mode (instructing the model to return the structured output)
SYSTEM_PROMPT_TEMPLATE = """
Analyze the research text provided below. Your goal is to extract high-quality YouTube channels that are relevant to the original query context (finding similar educational channels).

**Research Text:**
```
{deep_research_text}
```

**Instructions:**

1.  Focus only on channels explicitly mentioned as recommendations or relevant examples in the text.
2.  Extract the **channel name** and the **full canonical channel URL**.
3.  Ensure the URL is a valid YouTube channel URL (e.g., starting with https://www.youtube.com/channel/ or https://www.youtube.com/@).
4.  If you cannot find any valid channels based on the text, return an empty list.
5.  Provide your response as a JSON list, where each item is an object containing the channel name and URL, conforming to the provided schema.
"""

# Removed parse_gemini_json_list_output function as SDK handles parsing with response_schema

async def flash_normalise_channel_list(deep_research_text: str) -> Tuple[List[dict], Optional[str], Optional[str]]:
    """
    Uses Gemini Flash with JSON mode to extract a list of channel dictionaries from raw text.

    Args:
        deep_research_text: The raw text output from Perplexity Deep Research.

    Returns:
        A tuple containing:
        - list[dict]: List of successfully parsed channel data (e.g., [{"channel_name": "...", "channel_url": "..."}]). Empty if none found or error.
        - Optional[str]: The raw response text received from the Gemini API.
        - Optional[str]: An error message if any part of the process failed (API call, parsing, validation).
    """
    if not deep_research_text:
        logger.warning("Input text for normalization is empty.")
        return [], None, "Input text was empty."
    if not GEMINI_API_KEY:
        logger.error("Cannot call Gemini API: API key is missing.")
        return [], None, "Gemini API key is missing."

    prompt = SYSTEM_PROMPT_TEMPLATE.format(deep_research_text=deep_research_text)
    response_text: Optional[str] = None
    error_message: Optional[str] = None
    parsed_channel_list: List[dict] = []

    try:
        model = genai.GenerativeModel(MODEL_NAME)
        response = await model.generate_content_async(
            prompt,
            generation_config=genai.types.GenerationConfig(
                # Use the TypeAdapter's json_schema for the response schema
                response_schema=ChannelListAdapter.json_schema(), 
                response_mime_type="application/json",
            )
        )

        logger.debug(f"Raw Gemini response object: {response}")
        
        # Check for blocking or other issues before accessing text/parts
        if not response.candidates:
             if response.prompt_feedback.block_reason:
                  block_reason = response.prompt_feedback.block_reason.name
                  error_message = f"Response blocked. Reason: {block_reason}"
                  logger.warning(f"Gemini response blocked during normalization. Reason: {block_reason}")
             else:
                  error_message = f"Response missing candidates. Feedback: {response.prompt_feedback}"
                  logger.warning(f"Gemini response missing candidates during normalization. Feedback: {response.prompt_feedback}")
             # Return empty list, no response text, and the error
             return [], None, error_message
        
        # Try to get the response text
        try:
            response_text = response.text
            logger.debug(f"Gemini response text (expecting JSON): {response_text}")
        except ValueError as e:
            # Handle potential errors if response.text is accessed when blocked (should be caught above, but defensive)
            error_message = f"Value error accessing response text (likely blocked): {e}"
            logger.warning(f"{error_message}. Feedback: {getattr(response, 'prompt_feedback', 'N/A')}")
            return [], None, error_message # No response text available

        # Parse and validate the JSON string using the Pydantic TypeAdapter
        try:
            validated_channels = ChannelListAdapter.validate_json(response_text)
            # Convert Pydantic models back to dictionaries
            parsed_channel_list = [channel.model_dump(mode='json') for channel in validated_channels] # Use mode='json' for HttpUrl serialization
            logger.info(f"Successfully parsed and validated {len(parsed_channel_list)} channels using JSON mode.")
        except json.JSONDecodeError as e:
            error_message = f"Failed to decode JSON response: {e}. Response text: {response_text[:500]}"
            logger.error(error_message)
            # Keep response_text for logging
        except ValidationError as e:
            error_message = f"Pydantic validation failed: {e}. Response text: {response_text[:500]}"
            logger.error(error_message)
            # Keep response_text for logging

    except Exception as e:
        # Catch potential API errors (rate limits, etc.) or other unexpected issues
        error_message = f"Unexpected error during Gemini normalization: {e}"
        logger.error(error_message, exc_info=True)
        # response_text might be None if the error occurred before getting the response

    # Return the parsed list (empty if error), the raw text (if available), and any error message
    return parsed_channel_list, response_text, error_message 