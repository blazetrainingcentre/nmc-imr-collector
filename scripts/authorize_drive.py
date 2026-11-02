"""Create a local Google Drive OAuth refresh-token JSON for GitHub Actions.

Run this once on a trusted computer with a Google OAuth Desktop client JSON.
The printed JSON is the value for the GitHub secret
GOOGLE_DRIVE_CREDENTIALS_JSON. Do not commit either input or output file.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("client_secret", type=Path, help="OAuth Desktop client JSON downloaded from Google Cloud")
    parser.add_argument("--out", type=Path, default=Path("drive_token.json"))
    args = parser.parse_args()
    flow = InstalledAppFlow.from_client_secrets_file(str(args.client_secret), SCOPES)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    payload = json.loads(credentials.to_json())
    payload["type"] = "authorized_user"
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Saved authorized-user credentials to {args.out.resolve()}")
    print("Copy the complete JSON from that file into the GitHub secret GOOGLE_DRIVE_CREDENTIALS_JSON.")


if __name__ == "__main__":
    main()
