import google.generativeai as genai
import google.api_core.exceptions
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
# Use the model name set by the user previously
MODEL_NAME = "gemini-2.5-flash-preview-04-17" 

if not GEMINI_API_KEY:
    logger.warning("GEMINI_API_KEY not found in environment variables for llm_utils.")
else:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        logger.info(f"Gemini API configured successfully in llm_utils for model: {MODEL_NAME}")
    except Exception as e:
        logger.error(f"Error configuring Gemini API in llm_utils: {e}")

# --- Pydantic Schema Definition (still used for validation) ---
class ChannelInfo(BaseModel):
    channel_name: str = Field(..., description="The name of the YouTube channel")
    channel_url: str = Field(..., description="The full canonical URL of the YouTube channel (e.g., https://www.youtube.com/channel/UC... or https://www.youtube.com/@handle)")

ChannelListAdapter = TypeAdapter(List[ChannelInfo])

# --- Prompt with Schema embedded for JSON output ---
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
4.  If you cannot find any valid channels based on the text, return an empty JSON list `[]`.
5.  **Output your findings STRICTLY as a JSON list of objects, with no text before or after the list.** Each object must conform to this schema:
    ```json
    {{
      "channel_name": "string (The name of the YouTube channel)",
      "channel_url": "string (The full canonical URL of the YouTube channel)"
    }}
    ```
    Example Output:
    ```json
    [
      {{"channel_name": "Example Channel 1", "channel_url": "https://www.youtube.com/channel/UCexample1"}},
      {{"channel_name": "Example Channel 2", "channel_url": "https://www.youtube.com/@example2"}}
    ]
    ```
"""

async def flash_normalise_channel_list(deep_research_text: str) -> Tuple[List[dict], Optional[str], Optional[str]]:
    """
    Uses Gemini Flash to extract a list of channel dictionaries from raw text by including schema in the prompt.

    Args:
        deep_research_text: The raw text output from Perplexity Deep Research.

    Returns:
        A tuple containing:
        - list[dict]: List of successfully parsed channel data. Empty if none found or error.
        - Optional[str]: The raw response text received from the Gemini API.
        - Optional[str]: An error message if any part of the process failed.
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
        # Remove generation_config for JSON mode, rely on prompt instructions
        response = await model.generate_content_async(prompt)
        logger.debug(f"Raw Gemini response object: {response}")

        if not response.candidates:
             if response.prompt_feedback.block_reason:
                  block_reason = response.prompt_feedback.block_reason.name
                  error_message = f"Response blocked. Reason: {block_reason}"
                  logger.warning(f"Gemini response blocked during normalization. Reason: {block_reason}")
             else:
                  error_message = f"Response missing candidates. Feedback: {response.prompt_feedback}"
                  logger.warning(f"Gemini response missing candidates during normalization. Feedback: {response.prompt_feedback}")
             return [], None, error_message
        
        try:
            response_text = response.text
            logger.debug(f"Gemini response text (expecting JSON list): {response_text}")
        except ValueError as e:
            error_message = f"Value error accessing response text (likely blocked): {e}"
            logger.warning(f"{error_message}. Feedback: {getattr(response, 'prompt_feedback', 'N/A')}")
            return [], None, error_message

        # Parse and validate the JSON list string using the TypeAdapter
        try:
            # Attempt to find the JSON list within the response text, in case of minor extraneous text
            start_index = response_text.find('[')
            end_index = response_text.rfind(']')
            if start_index != -1 and end_index != -1 and end_index >= start_index:
                json_str = response_text[start_index : end_index + 1]
                validated_channels = ChannelListAdapter.validate_json(json_str)
                parsed_channel_list = [channel.model_dump() for channel in validated_channels]
                logger.info(f"Successfully parsed and validated {len(parsed_channel_list)} channels from response text.")
            else:
                error_message = "Could not find JSON list structure `[...]` in response."
                logger.warning(f"{error_message} Response text: {response_text[:500]}")
        except json.JSONDecodeError as e:
            error_message = f"Failed to decode JSON list response: {e}. Response text: {response_text[:500]}"
            logger.error(error_message)
        except ValidationError as e:
            error_message = f"Pydantic validation failed for channel list: {e}. Response text: {response_text[:500]}"
            logger.error(error_message)

    # Catch API errors 
    except google.api_core.exceptions.GoogleAPIError as e:
        error_message = f"Gemini API error during normalization call: {e}"
        logger.error(error_message, exc_info=True)
    except Exception as e:
        error_message = f"Unexpected error during Gemini normalization call: {e}"
        logger.error(error_message, exc_info=True)

    return parsed_channel_list, response_text, error_message 