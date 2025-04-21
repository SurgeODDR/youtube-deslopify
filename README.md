# YouTube Deslopify

This project analyzes YouTube watch history, fetches video metadata, transcribes videos using AI, and classifies them to help identify potentially low-quality or "slop" content unsuitable for children.

## Features

-   Processes local YouTube watch history (`Takeout` JSON format).
-   Identifies top watched channels.
-   Fetches metadata for recent, non-short videos from these channels using the YouTube Data API.
-   Transcribes video content directly from YouTube URLs using the Gemini API.
-   Classifies transcriptions based on language, coherence, educational value, engagement style, appropriateness, and overall quality using the Gemini API.
-   Stores all processed data in a local SQLite database.
-   (Optional) Enhances the channel list by using Perplexity Deep Research to find channels similar to those rated highly, then normalizes results with Gemini Flash.

## Technical Details

For a detailed explanation of the components, configuration, and execution flow, please see the [Technical Documentation](TECHNICAL_DOCUMENTATION.md).

## Setup & Usage

1.  **Clone the repository:**
    ```bash
    git clone <your-repo-url> youtube-deslopify
    cd youtube-deslopify
    ```
2.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
3.  **Configuration:**
    -   Create a `.env` file in the project root and add your Gemini API key:
        ```
        GEMINI_API_KEY='YOUR_API_KEY'
        ```
    -   **Authentication:**
        -   **Local:** Place your `client_secrets.json` (obtained from Google Cloud Console) in `notebooks/00_setup_and_auth/`. Run `python notebooks/00_setup_and_auth/00_youtube_oauth_local.py` once and follow the prompts to generate `token.json`.
        -   **Databricks:** Configure secrets (`youtube-client-id`, `youtube-client-secret`) in Azure Key Vault and ensure your Databricks cluster has access via Managed Identity (`DefaultAzureCredential`).
    -   **Watch History:** Place your YouTube watch history JSON files (e.g., from Google Takeout) in the `data/watch_history/` directory.
4.  **Run the Pipeline:** Execute the scripts sequentially (or adapt for your environment, e.g., Databricks notebooks):
    ```bash
    # Initial data loading and processing
    python notebooks/01_ingestion/00_youtube_ingestion.py
    python notebooks/01_ingestion/01_youtube_video_download.py 
    python notebooks/02_transcription/00_gemini_transcription.py
    python notebooks/03_classification/00_llm_classification.py
    
    # Optional: Discover new related channels
    python src/library_enhancer.py
    
    # If enhancer added channels, re-run processing for them:
    # python notebooks/01_ingestion/01_youtube_video_download.py 
    # python notebooks/02_transcription/00_gemini_transcription.py
    # python notebooks/03_classification/00_llm_classification.py
    ```
    *(Note: The video download script currently only fetches metadata, it doesn't download files)*

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the terms specified in the [LICENSE](LICENSE) file (if one exists).