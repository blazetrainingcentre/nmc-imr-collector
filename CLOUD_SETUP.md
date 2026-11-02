# GitHub Actions + Google Drive setup

This repository includes `.github/workflows/nmc-collect.yml`. It restores the
collector state from Google Drive, runs `collect`, and uploads the updated
`state.json`, `collect.jsonl`, and generated Excel files.

## 1. Create the Drive folder

Create a private folder in Google Drive, for example `nmc-collector`, and copy
the folder ID from its URL:

`https://drive.google.com/drive/folders/FOLDER_ID`

Do not make this folder public. The `collect` output contains personal fields.

## 2. Choose authentication

### Recommended for Google Workspace: service account + Shared Drive

1. In Google Cloud Console, create a project and enable the Google Drive API.
2. Create a service account and download its JSON key.
3. Create or choose a Workspace Shared Drive.
4. Add the service-account email as a member with write access.
5. Put the folder inside that Shared Drive and copy its folder ID.
6. Store the complete service-account JSON as the GitHub secret
   `GOOGLE_DRIVE_CREDENTIALS_JSON`.
7. Store the Shared Drive ID as `GOOGLE_DRIVE_SHARED_DRIVE_ID`.

Service accounts do not have personal Drive storage quota; Shared Drive is the
appropriate destination for them.

### Personal Google Drive: OAuth user credentials

Use a Google Cloud OAuth client of type **Desktop app**, enable the Drive API,
and authorize the account that owns the folder. Store the resulting
authorized-user JSON (it must contain `type: authorized_user` and a refresh
token) as `GOOGLE_DRIVE_CREDENTIALS_JSON`. Leave
`GOOGLE_DRIVE_SHARED_DRIVE_ID` unset.

From the repository root, the included helper can create that JSON after you
download the OAuth Desktop client file:

```bash
python -m pip install -r requirements.txt
python scripts/authorize_drive.py client_secret_*.json --out drive_token.json
```

Complete the browser authorization, then copy the entire contents of
`drive_token.json` into the GitHub secret. The helper and `.gitignore` protect
the local credential files from accidental commits.

Keep this JSON only in GitHub Secrets. Never commit it to the repository.

## 3. Add GitHub secrets

In the repository, open **Settings → Secrets and variables → Actions → New
repository secret** and add:

- `GOOGLE_DRIVE_CREDENTIALS_JSON`: the complete credential JSON on one secret.
- `GOOGLE_DRIVE_FOLDER_ID`: the Drive folder ID.
- `GOOGLE_DRIVE_SHARED_DRIVE_ID`: only for a Shared Drive; omit for personal Drive.

GitHub encrypts repository secrets and exposes them to a workflow only when the
workflow explicitly references them.

## 4. Run it

Open **Actions → NMC collect → Run workflow**. You may optionally provide a
council fragment and information-year range. Scheduled runs use the full
available range and resume from the saved Drive state.

The workflow is protected by a concurrency lock so two collector runs do not
overwrite each other's progress.

For the fixed ordered run of Tamil Nadu, Kerala/Trivandrum, Andhra Pradesh,
Telangana, Karnataka, Maharashtra, Uttar Pradesh, and Delhi from 1960 through
2026, use the separate **NMC selected states 1960-2026** workflow in the GitHub
Actions tab. It runs each state sequentially in the order defined in
`.github/workflows/nmc-selected-states.yml`.

To export the collected data, use the **NMC export collected data** workflow.
It restores `collect.jsonl` from Drive, creates one combined Excel workbook with
separate sheets for each council, then uploads that workbook to both Google
Drive and the GitHub Actions run artifacts.
That workflow checkpoints the Drive files after every information year, so a
GitHub Actions timeout normally loses at most the year currently in progress.

## 5. Verify the files

After a successful run, the Drive folder should contain:

- `state.json` — resume/checkpoint state
- `collect.jsonl` — full collected records
- any generated `.xlsx` exports in the collect directory

The workflow intentionally does not commit collected data back to GitHub.
