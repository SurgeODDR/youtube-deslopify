# Project Structure Documentation

## Overview

YouTube Deslopify is a tool designed to improve the quality of YouTube viewing experience by filtering out clickbait and low-quality content. The project is organized into several key components that handle different aspects of the workflow.

## Directory Structure

```
youtube-deslopify/
├── notebooks/
│   ├── 00_setup_and_auth/      # Authentication and initial setup
│   ├── 01_ingestion/           # Data ingestion from YouTube
│   ├── 02_transcription/       # Video transcription processing
│   ├── 03_classification/      # Content classification
│   └── 04_approval_workflow/   # Parent approval system
├── src/
│   ├── storage_utils.py        # Storage-related utilities
│   ├── ingestion_utils.py      # Data ingestion utilities
│   ├── classification_utils.py  # Classification utilities
│   └── transcription_utils.py  # Transcription utilities
└── data/
    └── watch_history/          # Watch history parsing and analysis
```

## Components

### 1. Authentication and Setup (`00_setup_and_auth/`)

The authentication system uses OAuth 2.0 to interact with the YouTube API. Key components:

- `youtube_auth_and_fetch.py`: Handles OAuth authentication and playlist fetching
  - Uses Azure Key Vault for secure credential storage
  - Implements token refresh mechanism
  - Provides playlist fetching functionality

### 2. Data Ingestion (`01_ingestion/`)

Handles the ingestion of YouTube data:

- `00_youtube_ingestion.py`: Creates and manages channel statistics
  - Processes channel data from JSON
  - Creates Unity Catalog tables
  - Stores channel statistics including views and URLs

### 3. Transcription (`02_transcription/`)

Processes video content for text analysis:

- Uses Whisper for transcription
- Handles audio extraction and processing

### 4. Classification (`03_classification/`)

Implements content classification systems:

- LLM-based classification for content quality assessment
- Filtering mechanisms for content recommendations

### 5. Approval Workflow (`04_approval_workflow/`)

Manages content approval processes:

- Parent approval system for content filtering
- Content review mechanisms

## Utility Modules

The `src/` directory contains utility modules that support the main functionality:

- `storage_utils.py`: Storage management utilities
- `ingestion_utils.py`: Data ingestion helper functions
- `classification_utils.py`: Content classification utilities
- `transcription_utils.py`: Audio transcription utilities

## Data Management

The project uses multiple data storage systems:

1. Azure Key Vault for secure credential storage
2. Unity Catalog for structured data storage
3. Local file system for temporary data processing

## Integration Points

The system integrates with several external services:

1. YouTube Data API v3
2. Azure Key Vault
3. Unity Catalog
4. Whisper transcription service

## Development Workflow

1. Authentication setup using OAuth 2.0
2. Data ingestion from YouTube channels
3. Content processing and transcription
4. Classification and quality assessment
5. Approval workflow management

## Security Considerations

- Credentials are stored securely in Azure Key Vault
- OAuth 2.0 implementation with refresh token support
- Secure handling of API keys and tokens