"""Sync collector state and output files with a Google Drive folder.

Credentials are supplied through GOOGLE_DRIVE_CREDENTIALS_JSON and are never
written to the repository. Both OAuth authorized-user credentials and service
account credentials are supported. Service accounts should target a Shared
Drive; OAuth credentials can target a user's My Drive.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive"]
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FILES = (Path("data/state.json"), Path("data/collect/collect.jsonl"))


def credentials_from_environment():
    raw = os.environ.get("GOOGLE_DRIVE_CREDENTIALS_JSON", "").strip()
    if not raw:
        raise SystemExit("GOOGLE_DRIVE_CREDENTIALS_JSON is not set")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit("GOOGLE_DRIVE_CREDENTIALS_JSON is not valid JSON") from exc
    if info.get("type") == "service_account":
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    # google-auth's Credentials.to_json() may omit the type marker even though
    # the payload is a valid authorized-user refresh-token credential.
    if info.get("type") == "authorized_user" or (info.get("refresh_token") and info.get("client_id")):
        return Credentials.from_authorized_user_info(info, scopes=SCOPES)
    raise SystemExit("Credentials must be a service-account JSON or authorized-user OAuth JSON")


def drive_service():
    return build("drive", "v3", credentials=credentials_from_environment(), cache_discovery=False)


def drive_options():
    shared_drive = os.environ.get("GOOGLE_DRIVE_SHARED_DRIVE_ID", "").strip()
    options = {"supportsAllDrives": True}
    if shared_drive:
        options.update({"includeItemsFromAllDrives": True, "corpora": "drive", "driveId": shared_drive})
    return shared_drive, options


def find_file(service, folder_id: str, name: str):
    shared_drive, options = drive_options()
    query = f"name = {json.dumps(name)} and '{folder_id}' in parents and trashed = false"
    request = service.files().list(q=query, spaces="drive", fields="files(id,name,size,modifiedTime)",
                                   **options)
    files = request.execute().get("files", [])
    if len(files) > 1:
        print(f"warning: found {len(files)} Drive files named {name}; selecting the largest/latest")
    # A prior interrupted upload can leave a small stale duplicate. Prefer the
    # largest copy, then the newest copy, so a complete JSONL wins over a
    # truncated upload.
    files.sort(key=lambda item: (int(item.get("size") or 0), item.get("modifiedTime") or ""), reverse=True)
    return files[0] if files else None


def pull(service, folder_id: str, relative: Path):
    target = ROOT / relative
    found = find_file(service, folder_id, relative.name)
    if not found:
        print(f"not found: {relative}")
        return
    print(f"using Drive file {found['id']} ({found.get('size', '?')} bytes) for {relative}")
    target.parent.mkdir(parents=True, exist_ok=True)
    request = service.files().get(fileId=found["id"], alt="media", supportsAllDrives=True)
    with target.open("wb") as handle:
        downloader = MediaIoBaseDownload(handle, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    print(f"downloaded: {relative}")


def push(service, folder_id: str, relative: Path):
    source = ROOT / relative
    if not source.is_file():
        print(f"skip missing: {relative}")
        return
    media = MediaFileUpload(str(source), mimetype=mimetypes.guess_type(source.name)[0] or "application/octet-stream",
                            resumable=True)
    found = find_file(service, folder_id, relative.name)
    shared_drive, options = drive_options()
    if found:
        service.files().update(fileId=found["id"], media_body=media, fields="id,name,modifiedTime",
                               **options).execute()
        print(f"uploaded: {relative}")
    else:
        body = {"name": relative.name, "parents": [folder_id]}
        service.files().create(body=body, media_body=media, fields="id,name,modifiedTime",
                               **options).execute()
        print(f"created: {relative}")


def files_to_sync():
    return [*DEFAULT_FILES, Path("data/collect/collect.xlsx")]


def main():
    parser = argparse.ArgumentParser(description="Sync NMC collector files with Google Drive")
    parser.add_argument("action", choices=("pull", "push"))
    parser.add_argument("--only", nargs="+", metavar="FILE",
                        help="sync only these filenames (matched against basename)")
    args = parser.parse_args()
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    if not folder_id:
        raise SystemExit("GOOGLE_DRIVE_FOLDER_ID is not set")
    service = drive_service()
    targets = files_to_sync()
    if args.only:
        allowed = set(args.only)
        targets = [f for f in targets if f.name in allowed]
    for relative in targets:
        (pull if args.action == "pull" else push)(service, folder_id, relative)


if __name__ == "__main__":
    main()
