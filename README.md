# LabPurchaseInventory

Lab purchasing and inventory project. LabPurchaseBot receives package photos in
Slack, reads labels through the signed-in Codex account using Luna, saves a
receipt draft, syncs package observations to the lab Google Sheet, and replies in
the original thread. Semantically interpreted forwarded order emails populate
Orders; exact order/tracking evidence can link receipts. Stock updates are not
implemented yet.

## Slack development on Windows

- Workspace: Lin Lab@JHU (`T03NHETTM2P`)
- App: LabPurchaseBot (`A0C2VCWLLAH`)
- Channel: #lab-order-request (`C04A5S6B7GX`, public)
- Bot scopes: `chat:write`, `channels:history`, `files:read`; no user token scopes.
- Socket Mode: enabled; app-level token scope: `connections:write`.
- Bot event subscription: `message.channels`.
- Invite LabPurchaseBot to the channel.

Create the local environment from PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

If `.env` already exists, edit it rather than replacing it. Set `SLACK_BOT_TOKEN`
to the bot's `xoxb-...` token from OAuth & Permissions. Set `SLACK_APP_TOKEN` to an
`xapp-...` token with `connections:write` from Basic Information > App-Level
Tokens. `.env` is ignored by Git. Keep tokens on this machine and out of chat,
screenshots, logs, and commits.

```powershell
.\.venv\Scripts\python.exe slack_bot.py --check
.\.venv\Scripts\python.exe slack_bot.py
```

The check validates the bot identity and channel history access without sending
messages. The listener then validates the Socket Mode connection. While it is
running, send `ping` or `test` in the channel or a thread. LabPurchaseBot replies
in that thread. New human messages (including thread replies and file shares)
are received without @mentions. Photo messages are saved under `.local/intake/`
with their caption, author ID, channel/message/thread timestamps, and original
image bytes. Regular logs contain timestamps and status, not message bodies.
No @mention is required. Events from other channels, workspaces, apps, bots,
message edits, deletions, and membership changes are ignored.

Set `LABEL_READER_ENABLED=true` in `.env` to enable the label worker (false leaves
capture and connection tests running). Install Codex and sign in with ChatGPT
under the Windows account running the task. The reader is pinned to
`gpt-5.6-luna` with reasoning effort `none`; no OpenAI API key is required or used.
It shares the account's Codex allowance, including limits shared with Astra.
When usage is exhausted, photos remain queued and the reader checks again after
30 minutes. Missing login also pauses analysis. There is no API-key fallback.
Keep the workstation awake and online.

It does not automatically recover Slack messages missed while disconnected
or update stock. Text-only discussion is
received but does not trigger AI replies yet. Clear label photos receive a
summary without further questions. Personnel photograph each received package:
clear package counts and per-package contents support inferred receipt quantities.
Ambiguous quantities and unstated final storage locations stay unknown; only
unreadable labels prompt a clearer-photo request.
Duplicate message deliveries are suppressed within the running process. Photo
downloads are also reused across restarts after their hashes are verified. Keep the
process running to receive events; use Ctrl+C to stop it. An OS lock prevents
multiple listeners/analysis commands from running at once.

## Automatic startup and recovery on Windows

This workstation uses the Windows scheduled task `LabPurchaseBot-Standalone`.
It starts silently 30 seconds after `LinLab_Workstation4` signs in, including
after a reboot, and does not require the Codex desktop app to be open. It runs
under that account with ordinary permissions and its saved Codex login. It
does **not** run at the pre-login screen or while the workstation is asleep/off.
No Windows password is stored by the project and power settings are unchanged.

The task runs `supervise_bot.py`, which restarts an exited listener after 30
seconds, increasing the delay up to five minutes after repeated short failures.
This also retries when startup precedes network availability. Task Scheduler
retries an unexpectedly failed supervisor up to three times at one-minute
intervals. There is no scheduled runtime limit. A Windows job object cleans up
the bot's process tree if the supervisor stops, and locks prevent duplicate
supervisors/listeners. The reader discovers the installed Codex binary even without the desktop app's PATH.

To install/update the task on this workstation (registration does not start it):

```powershell
.\scripts\install_startup.ps1
Start-ScheduledTask -TaskName 'LabPurchaseBot-Standalone'
```

Stop the managed process before foreground debugging or one-off analysis:

```powershell
Stop-ScheduledTask -TaskName 'LabPurchaseBot-Standalone'
```

To keep it stopped across later sign-ins, also run
`Disable-ScheduledTask -TaskName 'LabPurchaseBot-Standalone'`. Re-enable with
`Enable-ScheduledTask` and start with `Start-ScheduledTask` using the same name.
Do not stop only the child Python process for maintenance: the supervisor
will restart it.

Machine-local runtime files in the ignored `.local/` directory include:

- `slack-bot.pid`, `slack-bot.stdout.log`, `slack-bot.stderr.log`
- `slack-supervisor.pid`, `slack-supervisor.log`, `slack-supervisor-state.json`

The state file records process lifecycle, not Slack readiness; confirm a fresh
"Starting to receive messages" line in the bot log as well as a running task.
Supervisor logs rotate at 1 MiB (three backups). Bot logs rotate at launch once
they exceed 5 MiB, retaining one previous log. Capture and analysis records remain
intact across restarts, and already-sent receipt replies are not reposted.

## Photo intake

Each photo message has a `record.json` alongside its images. This is the original
capture record; the worker writes its separate `analysis.json` with extracted
fields, usage, attempts, status and Slack reply timestamp. With the reader enabled,
the images and caption are sent through Codex with the saved ChatGPT login.
The queue handles one message at a time, including captured pending photos on
startup. Printed pack size stays separate from received quantity; intended
storage stays separate from confirmed placement. Inventory changes remain false.

The reader runs in an empty temporary directory with user configuration ignored,
a read-only sandbox, forced ChatGPT authentication, and tools/plugins disabled.
Slack/API keys are excluded from its environment. Schema validation and tool-event
checks reject unexpected output. Replies use Slack plain text. The API reader
and setup helper remain unused development code; the bot cannot select them.

Ordinary analysis failures retry at most three times before a failure notice.
Usage-limit errors instead pause the entire analysis queue for 30 minutes,
including across restarts, without consuming those retries or posting failures.
Capturing new photos continues. Successful results are cached. An interrupted
Slack send is held as `delivery_uncertain` for manual checking, preventing blind
reposts. Stop the bot before manually repairing records.

Download limits are 20 MiB per image, 40 million pixels, and 20 files per message.
Decoded JPEG, PNG, WebP, GIF, and TIFF images can be captured; reading depends on
Codex image-format support. Other formats, including HEIC, are unsupported.
Non-image files are skipped. `partial` captures summarize readable attachments;
`download_failed` captures need manual download retry.

Downloads require authenticated HTTPS from `files.slack.com`; redirects are
rejected to avoid forwarding credentials to another host.

To capture or retry photos from one existing channel message without posting:

```powershell
.\.venv\Scripts\python.exe slack_bot.py --capture-message '1789771882.188909'
.\.venv\Scripts\python.exe slack_bot.py --analyze-message '1789771882.188909'
```

Always quote a Slack timestamp in PowerShell; an unquoted number can be rounded.
Stop the background bot before running these one-off commands. `--analyze-message`
uses the shared Codex allowance and saves a ready result without posting; starting the enabled
listener subsequently delivers that result. Already-sent results are reused.
This is an explicit one-message import, not a historical channel scan. Queued
images and captions stay in the ignored `.local` folder on this workstation.
They are retained until manually removed; automatic retention is not configured.

See [label interpretation requirements](docs/package-labels.md) and the official
[Codex automation documentation](https://learn.chatgpt.com/docs/non-interactive-mode).

Run the local routing tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Google authorization for `linjhumse@gmail.com` was completed on 2026-09-19.
Encrypted credential reload, token refresh, Gmail profile and Drive account checks
passed. Package receipt sync and forwarded order-email ingestion are implemented. The in-chat Gmail
and Google Drive connectors currently identify `odehanoid@gmail.com`; do not use
that account for the lab workflow. Connector access in this chat is not a reusable
credential for the standalone bot. Gmail should have read-only access; Sheets
writes must target an explicitly selected spreadsheet and preserve its structure.

The [Google authorization helper](docs/google-setup.md) is configured with Gmail
read-only and per-file Drive access, account verification, and Windows-encrypted
credential storage.

## Google Sheet delivery

The selected workbook and tab IDs are stored in ignored `.local/google-sheet.json`.
Set `automatic_sync_enabled` to true to run an independent sheet worker alongside
the Slack listener. It scans cached successful label results every 30 seconds and
writes the `Package receipts` tab and the enabled Inventory summary. It never calls the AI reader or posts
additional Slack messages. Previously analyzed eligible photos are imported too.
Photos and captions receive semantic delivery assessment before receipt creation.
Historical shortage results remain excluded; new captions have no keyword veto.
Inventory tracks delivery totals; consumption-adjusted stock remains unimplemented. The email worker can attach exact
order/tracking matches; catalog-only matches are explicitly labeled as possible.

Each item gets a stable receipt ID and a separate `sheet-sync.json` delivery
journal. Row reservations survive restart; timeout recovery looks for the ID
before writing, including if an operator sorted the sheet. Existing receipt IDs
and manually occupied rows are preserved. Avoid editing rows while a write is
in flight: Sheets does not provide a compare-and-set cell operation. Formula-like
label text is stored literally. Printed packaging is separate from receipt count,
and message-level quantity statements are preserved in Notes, not distributed
across items. `receipt_quantity.py` fills received quantity and unit from distinct
photographed packages times contents per package, when the visual observation and
label or compatible shipment pack size support it. The calculation is preserved
in Notes and marked inferred. Multiple views of one package count once; identical
image reuploads are suppressed. Different views reposted in separate messages
cannot yet be reliably deduplicated without unique package identification.
Mixed-item cartons and conflicting pack sizes stay unknown. Existing quantities,
including manual corrections, are not overwritten. This updates receipt counts,
not the separate stock balance. Slack times
are recorded in UTC and source links point back to the original messages.

Network/authentication failures keep the result queued with a five-minute retry;
new Slack capture and label reading continue independently. Renew expired Google
authorization with `google_auth.py`. An account mismatch, moved/renamed receipt
tab, changed headers, duplicate IDs or occupied reserved row blocks that write.
Inspect `sheet-sync.json` and the bot log for the fixed diagnostic code. After a
manual repair, the next retry uses cached analysis without additional Codex usage.

For a one-pass sync, use `.\.venv\Scripts\python.exe sheet_sync.py --once`.
The sheet worker has a separate OS lock to prevent competing passes.

## Forwarded order email sync

`order_email.py` captures messages from the configured forwarding address with
Gmail read-only access; English subject keywords and a specific forwarding prefix
are not required. `order_sync.py` polls every five minutes. The sender, earliest
receipt time and enabled state live in ignored `.local/email-config.json`.
The overlapping scan cursor and message cache prevent repeat downloads.

`email_reader.py` uses gpt-5.6-luna with low reasoning through the saved Codex
ChatGPT login, without an API key. It interprets varied suppliers, languages,
prose, tables, PDF layouts and image attachments. PDFs are rendered locally with
Poppler; the model gets both extracted text and page images, including scans.
Inline/attached images are decoded locally and passed in the same source bundle.
Limits: 20 visual pages/images, 10 MB per PDF, 20 MB per input image and 250,000
text characters per email. Encrypted/unreadable or unsupported attachments and
limits are explicit review conditions; no source is silently truncated.

The model returns typed order/line facts with field-level source quotations.
Code checks source IDs, quoted text, copied identifiers, dates, sales units and
pack contents. A failed evidence check triggers one automatic reread with precise
feedback. Optional missing information stays null. Familiar wording isn't required.
Known records can resolve omitted line identity only for an explicit matching
order ID. Unrelated mail is cached as ignored and generates no Slack reply.
Email contents cannot invoke tools, send mail, visit links or act on payments.

Confirmations, shipping updates, partial shipments, delays/backorders,
cancellations, carrier delivery, invoices and invoice notifications are supported.
Same supplier/order/catalog records merge; uniquely identified shipments and
explicit cumulative totals are reconciled without adding repeated notices.
Known pack factors can reconcile differing sales units. Unidentified shipments,
conflicting quantities, or incomplete source coverage do not acquire guessed
item totals. Carrier delivery never becomes a lab receipt. Invoice quantities
alone do not establish shipment, and payment status is never inferred.
Cancelled orders retain their original ordered amount; outstanding is unknown.

Successful extractions are cached; byte-identical attachments and identical
subject/body content reuse validated facts across forwarding IDs, while retaining
the new email link and original event timing. Older successful Fisher parses remain stable;
old template-related failures are automatically offered to the semantic reader.
Quota exhaustion pauses the email interpretation queue for 30 minutes with no
API fallback; other reader failures retry up to three times before review.
Queued counts appear in the worker status. These interpretations consume shared
Codex usage, as do image and query reading. Credentials stay in the encrypted
lab-account credential store and are not passed to the model process.

Order row reservations and last-written values are journaled in
`.local/email-intake/sheet-sync.json`. A lost response is reconciled after restart,
including after sorting. Manual cell edits are preserved and marked for review
instead of overwritten. Avoid simultaneous user edits to rows being written;
Google Sheets has no compare-and-set cell operation. Records, PDFs, parsed fields
and review codes remain under ignored `.local/email-intake/`; do not commit them.

Inspect `.local/email-worker-status.json` for last success or retry status and
`*/order.json` for extraction evidence and review diagnostics. Renew Google authorization
when its testing token expires. After adding parser support, remove only the
specific affected `order.json` cache while the worker is stopped to reparse the
preserved source. Do not remove delivery journals to force a retry.

Run `.\.venv\Scripts\python.exe order_sync.py --once` for a single enabled pass.
Its lock prevents competing order passes; receipt matches use the receipt lock.
New documents update Orders and receipt matching; they do not place purchases.
With `slack_notifications_enabled` in `.local/email-config.json`, the existing
five-minute email worker also posts order-status changes to the configured lab
channel. It reports product, catalog, supplier/order, shipped quantities/dates,
available tracking and invoice notifications, with an Open order button. Shipping
status is distinguished from physical lab receipt; prices/payment instructions
are omitted. Unsupported or conflicting order emails produce a needs-review
notice without guessing their status. Layout changes alone do not require review;
interpretation is semantic, while unsupported file types and genuinely ambiguous
facts still need review.

`.local/email-intake/slack-notifications.json` stores a durable baseline and each
delivery attempt. Initial enablement baselines already processed orders/review
emails. New forwarding IDs or routine scans do not cause duplicate status posts;
only a changed status fingerprint does. Each order row must be synced and freshly
verified before posting. Definitive Slack rejections retry; a timeout or interrupted
post remains delivery-uncertain for operator review instead of blindly resending.
Notifications use the standalone Slack bot and no additional model call. Reorder requests are a
separate unfinished workflow.

## Inventory delivery summary

`status_queries.py` sends text-only human messages (except ping/test) through
`query_semantics.py` using gpt-5.6-luna with low reasoning effort, the saved ChatGPT
login, and no API-key fallback. Both query interpretation and photo analysis use
reasoning; interpretation is not replaced by keyword filters to save usage.
The model sees the message, up to six earlier locally captured messages/replies
from the same thread, available lookup capabilities, and a live product/order
catalog. It selects a read-only action, matching record keys and an optional
receipt date interval. Schema, key, and date checks precede deterministic lookup
and calculation. Unrelated chat/shortage announcements stay silent; genuine
ambiguity gets clarification. No keyword-parser fallback exists.

"Show your inventory" lists tracked entries. "Show added inventory in the past
week" filters receipt rows, not cumulative Inventory totals. Calendar intervals,
synonyms and follow-ups are interpreted semantically; default date boundaries
and displayed receipt timestamps are UTC. Dates are Slack photo posting times,
not verified physical arrival times. Undated receipts are explicitly excluded,
unknown quantities are partial totals, and repeated source records deduplicated.
Order-date/status-history and requester-ownership filtering remain unsupported.
The model is fallible; unsupported constraints must be explained, never silently
reinterpreted as missing products. Answers list up to five products with row links.

Plans are cached against their catalog, while live quantities refresh on retry.
Invalid model results are retried at most three times, then held as needs_review.
Quota failures persist a 30-minute query-queue cooldown; requests stay queued
instead of producing keyword-based answers. Query interpretation consumes shared
Codex usage, including classifying irrelevant text messages. Photo attachments
use the photo-analysis route; simultaneous query handling for photo captions and
context before the bot captured a thread are not implemented.

Answers distinguish shipped versus lab-received, exact order links versus
provisional product totals, and cumulative deliveries versus unknown stock on
hand. Intended storage is never reported as confirmed; missing ETAs remain
unknown. Queries do not edit sheets, place orders, send mail or change settings.
Pending queries and delivery state are kept under `.local/queries`. Google
failures retry; uncertain Slack deliveries are not blindly resent. Receipt-photo
messages retain their intake route. Shortage reports containing an explicit
order question can receive a read-only answer without becoming package receipts.

When sheet sync is enabled, `receipt_reply.py` prepares package replies after
receipt delivery, quantity inference, order matching and Inventory reconciliation.
It reads live Orders/Inventory rows and uses the supplier's product name when the
product match is unambiguous. Replies include this receipt's saved quantity,
cumulative delivery totals, reconciliation status and buttons to open the exact
Inventory, receipt and order rows. Catalog/supplier matches are explicitly possible
orders; compatible order/PO/tracking evidence is required for a confirmed order.
Conflicting catalog specifications (including volume) or identifiers prevent a
confirmed match. No order is invented when none exists, and multiple candidate
orders remain visible. Matching and reconciliation run locally/through Google;
no additional model call is needed.

The reply preparation shares the sheet lock with other writers. A sheet failure
keeps the cached image result ready and retries context preparation after a minute
without another image read or an ungrounded Slack reply. Manual Inventory changes
that need review also hold the reply. A possibly delivered Slack reply is still
never automatically resent. Previously posted replies are left unchanged.

Photos, including shortage/reorder captions, reach a per-item visual receipt
assessment in the Luna label-reading call. No keyword rule rejects new photo
captions before semantic interpretation. Historical cached shortage results keep
the old exclusion unless reanalyzed; removing the old filter must not retroactively
import previously misclassified supplies. New analyses carry assessment version 4.
The model uses visible packaging/condition and caption context
to distinguish delivery, existing supply, uncertain scenes and unrelated objects.
Only a high-confidence delivery with recorded evidence can generate a Slack
package reply, a new receipt row or inferred quantity. A readable label alone
is insufficient. Uncertain/existing-supply photos stay silent and remain cached
locally; no follow-up question or inventory write is generated. Background supplies
are excluded from a mixed scene. Old extraction records lacking the assessment
cannot become new deliveries; previously recorded receipts remain intact.

Members can include a total count in the photo message, for example "Received
4 cases of these tubes; only one shown" or "Received 3 packages, all identical
to this one." The reader extracts a per-item stated quantity and exact caption
quote separately from the visible package count. A clear stated count replaces
the visible count; it is never added to it. Case/pack contents convert using the
label or compatible order pack size, and the receipt note records both sources.
An explicit individual count is kept as each. Generic identical packages use a
clearly identified photographed outer unit; cartons never automatically become
cases. If contents are unknown, preserve the reported package count/unit and
leave the individual-item total unknown. Ambiguous totals across different
products are not assigned to every item. Put counts in the same message as the
photo; later text-only replies and edited-message corrections are not implemented.

`inventory_sync.py` runs after receipt quantity inference in the existing sheet
worker when `inventory_sync_enabled` is true in `.local/google-sheet.json`.
It reads the live Orders and Package receipts rows and rebuilds totals by
supplier/catalog, converting case/pack quantities to each only when contents are
known. Inventory shows ordered, received, outstanding, reconciliation, confirmed
storage and source links. Received is cumulative delivery quantity, not current
stock: consumption and opening stock are not tracked.

Catalog-only matches remain provisional, even for a single known order.
Multiple orders are grouped without allocating receipts by guesswork. Unknown
receipt quantities leave outstanding blank; received is then explicitly a known
subtotal. Missing order conversions, over-receipts, and exact-order allocation
conflicts are marked. Intended storage never populates confirmed storage.

Stable receipt IDs, source-message links and local image hashes prevent repeat
processing and identical-image reuploads from increasing the total. Different
photos of the same physical package across separate messages can still require
a unique package identifier to distinguish them. Totals are recomputed, not
incremented, so source corrections flow through. `.local/inventory-sync.json`
journals writes before delivery and reconciles lost responses and sorted rows;
manual changes to generated Inventory rows are preserved and flagged in
`.local/inventory-worker-status.json`. Correct source rows in Package receipts
or Orders to have the summary recalculate automatically. Avoid simultaneous edits
to a row being written; Sheets does not provide compare-and-set cell writes.

Package contents also use semantic factors (`pack_contents.py`), rather than
requiring a particular printed syntax. For example, ten sleeves of fifty tubes
becomes 10 x 50 after extraction; the arithmetic is performed in code and checked
against recognized explicit pack sizes. A bottle's 500 mL is never 500 items.
Original wording remains in the source/analysis, and normalized contents support
email-to-label quantity reconciliation. Member-stated totals still replace the
visible package count; they are never added to it.
