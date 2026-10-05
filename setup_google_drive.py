"""Create a Google Drive OAuth refresh token for the JNV app.

Run locally after downloading an OAuth Desktop App client JSON from Google Cloud.
The generated google_token.json is secret and is ignored by Git.
"""
import json
import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]
CLIENT_FILE = os.environ.get("JNV_GOOGLE_CLIENT_SECRET_FILE", "client_secret.json")
TOKEN_FILE = "google_token.json"

if not os.path.exists(CLIENT_FILE):
    raise SystemExit(
        f"Missing {CLIENT_FILE}. Download your Google OAuth Desktop App JSON "
        "and place it beside this script."
    )

flow = InstalledAppFlow.from_client_secrets_file(CLIENT_FILE, SCOPES)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
with open(TOKEN_FILE, "w", encoding="utf-8") as f:
    f.write(creds.to_json())

print(f"Created {TOKEN_FILE}")
print("Keep this file secret. Do NOT upload it to GitHub.")
print("For deployment, copy the complete JSON from this file into")
print("the JNV_GOOGLE_TOKEN_JSON environment variable.")
