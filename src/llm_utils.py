import google.generativeai as genai
import os
import asyncio
import json
import logging
from dotenv import load_dotenv
from pydantic import BaseModel, Field, HttpUrl, ValidationError, TypeAdapter
from typing import List

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

async def flash_normalise_channel_list(deep_research_text: str) -> list[dict]:
    """
    Uses Gemini Flash with JSON mode to extract a list of channel dictionaries from raw text.

    Args:
        deep_research_text: The raw text output from Perplexity Deep Research.

    Returns:
        A list of dictionaries, e.g., [{"channel_name": "...": "channel_url": "..."}],
        or an empty list if no channels are found or an error occurs.
    """
    if not deep_research_text:
        logger.warning("Input text for normalization is empty.")
        return []
    if not GEMINI_API_KEY:
        logger.error("Cannot call Gemini API: API key is missing.")
        return []

    prompt = SYSTEM_PROMPT_TEMPLATE.format(deep_research_text=deep_research_text)
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
                  logger.warning(f"Gemini response blocked during normalization. Reason: {block_reason}")
                  return []
             else:
                  logger.warning(f"Gemini response missing candidates during normalization. Feedback: {response.prompt_feedback}")
                  return []
        
        # Access the response text which should contain the JSON string
        response_text = response.text
        logger.debug(f"Gemini response text (expecting JSON): {response_text}")

        # Parse the JSON string using the Pydantic TypeAdapter
        validated_channels = ChannelListAdapter.validate_json(response_text)
        
        # Convert Pydantic models back to dictionaries for compatibility with downstream DB insert
        channel_list_dicts = [channel.model_dump() for channel in validated_channels]
        
        logger.info(f"Successfully parsed and validated {len(channel_list_dicts)} channels using JSON mode.")
        return channel_list_dicts

    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode JSON from Gemini response text: {e}. Response text: {response_text[:500]}")
        return []
    except ValidationError as e:
        logger.error(f"Pydantic validation failed for Gemini JSON response: {e}. Response text: {response_text[:500]}")
        return []
    except ValueError as e:
         # Catch potential errors if response.text is accessed when blocked
         logger.warning(f"Value error accessing Gemini response (likely blocked): {e}. Feedback: {getattr(response, 'prompt_feedback', 'N/A')}")
         return []
    except Exception as e:
        # Catch potential API errors (rate limits, etc.) or other unexpected issues
        logger.error(f"Unexpected error during Gemini normalization with JSON mode: {e}", exc_info=True)
        return [] 