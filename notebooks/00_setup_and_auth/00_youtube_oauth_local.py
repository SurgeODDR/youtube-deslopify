from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import os
import json
from typing import Optional
from pathlib import Path

# Get the directory where the script is located
SCRIPT_DIR = Path(__file__).resolve().parent
CLIENT_SECRETS_PATH = SCRIPT_DIR / 'client_secrets.json'
TOKEN_PATH = SCRIPT_DIR / 'token.json'

# OAuth 2.0 scopes that we'll need for accessing playlists
SCOPES = ['https://www.googleapis.com/auth/youtube.readonly']

def get_client_config():
    """Load client configuration from client_secrets.json in the script's directory."""
    if not CLIENT_SECRETS_PATH.is_file():
        print(f"Error: client_secrets.json not found at {CLIENT_SECRETS_PATH}")
        raise FileNotFoundError(f"Client secrets file not found: {CLIENT_SECRETS_PATH}")
    try:
        with open(CLIENT_SECRETS_PATH, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading {CLIENT_SECRETS_PATH}")
        print(f"Error details: {str(e)}")
        raise

def get_credentials() -> Optional[Credentials]:
    """Gets valid user credentials from storage or initiates OAuth2 flow."""
    credentials = None
    client_config = None

    # 1. Try to get client config first
    try:
        client_config = get_client_config()
    except Exception as e:
        # Error already printed in get_client_config or file not found
        print(f"Failed to load client configuration: {e}")
        return None # Cannot proceed without client config

    # 2. Try to load/refresh token using the loaded client_config
    try:
        if TOKEN_PATH.exists():
            print(f"Loading existing token from {TOKEN_PATH}...")
            with open(TOKEN_PATH, 'r') as token_file:
                token_data = json.load(token_file)
            credentials = Credentials(
                token=token_data.get('token'),
                refresh_token=token_data.get('refresh_token'),
                token_uri=client_config['web']['token_uri'],
                client_id=client_config['web']['client_id'],
                client_secret=client_config['web']['client_secret'],
                scopes=token_data.get('scopes')
            )

            if credentials and credentials.valid:
                print("Existing credentials are valid")
                return credentials

            if credentials and credentials.expired and credentials.refresh_token:
                print("Refreshing expired credentials...")
                # Need the requests transport adapter for refresh
                from google.auth.transport.requests import Request 
                credentials.refresh(Request())
                save_credentials(credentials) # Save refreshed token
                print("Credentials refreshed successfully.")
                return credentials

        # If token doesn't exist, is invalid without refresh token, or refresh failed implicitly above
        print("No valid token found or refresh needed/failed. Starting new OAuth flow...")
        # Pass client_config explicitly to perform_oauth_flow
        return perform_oauth_flow(client_config)

    except Exception as e:
        print(f"Error during token loading/refresh: {str(e)}")
        print("Attempting OAuth flow as fallback...")
        # We know client_config exists if we got here without the first except block triggering
        # Pass client_config explicitly to perform_oauth_flow
        return perform_oauth_flow(client_config)

# Modified perform_oauth_flow to accept client_config
def perform_oauth_flow(client_config: dict) -> Optional[Credentials]:
    """Performs the OAuth flow using the provided client configuration."""
    if not client_config:
        print("Error: Client configuration is missing, cannot perform OAuth flow.")
        return None
    try:
        print("Starting OAuth flow...")
        
        # Use the passed client_config
        flow = Flow.from_client_config(
            client_config,
            scopes=SCOPES,
            redirect_uri="http://localhost:8080"
        )
        
        # Generate authorization URL
        auth_url, _ = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            prompt='consent'
        )
        
        print("\nPlease follow these steps:")
        print("1. Visit this URL to authorize the application:")
        print(auth_url)
        print("\n2. After authorization, you'll be redirected to localhost:8080 (it might show an error - that's okay)")
        print("3. Copy the ENTIRE URL from your browser's address bar (starting with http://localhost:8080/...) and paste it below")
        
        # Get the full redirect URL from user
        redirect_response = input("\nEnter the full redirect URL: ").strip()
        print("\nProcessing redirect URL...")
        
        # Exchange the authorization response for credentials
        flow.fetch_token(authorization_response=redirect_response)
        credentials = flow.credentials
        
        # Save the credentials
        save_credentials(credentials)
        
        return credentials
        
    except Exception as e:
        print(f"Error during OAuth flow: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

def save_credentials(credentials: Credentials):
    """Save credentials to token.json in the script's directory."""
    token_data = {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes
    }
    
    try:
        with open(TOKEN_PATH, 'w') as token_file:
            json.dump(token_data, token_file, indent=4)
        print(f"Credentials saved to {TOKEN_PATH}")
    except Exception as e:
         print(f"Error saving credentials to {TOKEN_PATH}: {e}")

if __name__ == "__main__":
    # For testing OAuth locally
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
    
    print("Starting local OAuth test...")
    credentials = get_credentials()
    
    if credentials:
        print("\nAuthentication successful!")
        print(f"Access token exists: {'Yes' if credentials.token else 'No'}")
        print(f"Refresh token exists: {'Yes' if credentials.refresh_token else 'No'}")
        print(f"Token expiry: {credentials.expiry}")
    else:
        print("\nAuthentication failed!") 