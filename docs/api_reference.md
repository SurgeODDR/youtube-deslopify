# API Reference

## Authentication Module

### `youtube_auth_and_fetch.py`

#### `get_client_config()`
Retrieves client configuration from Azure Key Vault.

**Returns:**
- `dict`: OAuth client configuration including client ID and secret

#### `get_credentials() -> Optional[Credentials]`
Gets valid user credentials from storage or initiates OAuth2 flow.

**Returns:**
- `Credentials`: Google OAuth credentials object
- `None`: If authentication fails

#### `perform_oauth_flow() -> Optional[Credentials]`
Performs the OAuth flow to get new credentials.

**Returns:**
- `Credentials`: Fresh OAuth credentials
- `None`: If flow fails

#### `fetch_playlists(credentials: Credentials) -> Optional[pd.DataFrame]`
Fetches all playlists for the authenticated user.

**Parameters:**
- `credentials`: Google OAuth credentials

**Returns:**
- `DataFrame`: Contains playlist information
  - Columns: id, title, description, video_count, privacy_status, published_at
- `None`: If fetching fails

## Data Ingestion Module

### `00_youtube_ingestion.py`

#### Channel Statistics Processing
Processes YouTube channel statistics and creates Unity Catalog tables.

**Table Schema:**
- `name`: Channel name
- `url`: Channel URL
- `views`: View count

**Unity Catalog Integration:**
- Catalog: yt-deslopify
- Schema: default
- Table: channel_statistics

## Utility Modules

### `storage_utils.py`
Storage management utilities for handling data persistence.

### `ingestion_utils.py`
Utilities for YouTube data ingestion and processing.

### `classification_utils.py`
Content classification and filtering utilities.

### `transcription_utils.py`
Audio transcription and processing utilities.

## Data Structures

### Playlist Information
```python
{
    'id': str,           # Playlist ID
    'title': str,        # Playlist title
    'description': str,  # Playlist description
    'video_count': int,  # Number of videos
    'privacy_status': str,  # Privacy setting
    'published_at': str  # Publication date
}
```

### Channel Statistics
```python
{
    'name': str,    # Channel name
    'url': str,     # Channel URL
    'views': int    # View count
}
```

## Error Handling

The system implements comprehensive error handling:

1. OAuth Errors:
   - Token refresh failures
   - Authorization flow errors
   - Invalid credentials

2. API Rate Limits:
   - Automatic retry with exponential backoff
   - Rate limit monitoring

3. Data Processing:
   - Missing data handling
   - Invalid format handling
   - Storage failures

## Configuration

### OAuth Scopes
```python
SCOPES = ['https://www.googleapis.com/auth/youtube.readonly']
```

### Azure Key Vault
```python
KEY_VAULT_URL = "https://gattaca-keys.vault.azure.net/"
```

### Unity Catalog
```python
table_name = "`yt-deslopify`.default.channel_statistics"
```