# Google authorization for LabPurchaseBot

Authorization completed and verified on 2026-09-19 for `linjhumse@gmail.com` in
project `labpurchasebot`, using desktop client `LabPurchaseBot Workstation`.
Encrypted credential reload, offline refresh, Gmail profile, and Drive account
checks passed. No message bodies or spreadsheet contents were read. The app is
still in Testing, so its refresh token expires after seven days; this is not yet
a permanent unattended authorization.

The background bot uses its own Google Desktop OAuth client and the account
`linjhumse@gmail.com`. Browser sign-in and the in-chat Google connector are
separate. This setup does not change the connector account or require an OpenAI
API key.

1. In Google Cloud, select/create a project for LabPurchaseBot. Enable Gmail,
   Google Sheets, and Google Drive APIs.
2. Configure Google Auth Platform branding with app name LabPurchaseBot and the
   lab account as the support/contact email. A personal Gmail account needs an
   External audience. While testing, add the lab account as a test user.
3. Create an OAuth client with application type **Desktop app**. Download its
   JSON locally and save it as `.local/google-client.json`. Do not paste it in chat.
4. Run `.\.venv\Scripts\python.exe google_auth.py` under the same Windows user
   as the scheduled bot. Select the lab account and review the consent screen.
5. Run `.\.venv\Scripts\python.exe google_auth.py --check` to verify the saved
   authorization. Verification reads only the Gmail account profile.

Requested permissions are Gmail read-only and `drive.file` (files created or
explicitly opened with this app). Gmail authorization covers the mailbox, not
only purchase messages; ingestion applies the configured sender, forwarding,
date and purchase-specific filters.
No mail sending, deletion, or mailbox modification is requested. The file scope
does not expose all existing spreadsheets: an existing sheet requires explicit
file selection through Google Picker; a new app-created sheet is accessible.
The app-created workbook is configured in `.local/google-sheet.json`. Automatic
delivery of cached package observations to `Package receipts` is implemented in
`sheet_sync.py`; enable it with `automatic_sync_enabled`. `order_sync.py` uses
Gmail read-only access to capture configured forwarded order emails and update
Orders, preserving source links. Fisher shipping confirmations and invoice
notifications are supported; unknown formats are held for review. Catalog-only
receipt matches stay tentative; exact order/tracking matches can be recorded.
Inventory stock updates remain separate work.

Credentials are encrypted using Windows DPAPI for the signed-in Windows user
and stored in ignored `.local/google-token.dpapi`. They cannot be moved to another
Windows account. The code rejects a different Google account before saving its
credentials. Tokens and OAuth response bodies are never printed.

Google may expire refresh tokens after seven days for External apps in Testing
with these scopes. Production status, verification requirements, and the consent
screen must be reviewed before treating the connection as unattended long-term
access. The helper uses offline access and a loopback callback with PKCE.

References: [Google Gmail Python setup](https://developers.google.com/workspace/gmail/api/quickstart/python),
[Sheets authorization scopes](https://developers.google.com/workspace/sheets/api/scopes).
