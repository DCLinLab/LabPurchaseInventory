# Archived API option (inactive)

The user chose Codex login instead of API billing. The live bot uses Luna with
the shared Codex allowance. No API key setup is needed. The notes below document
the earlier option only; running the helper does not switch the live reader.

## Save a bot-specific key

1. Open the [OpenAI API platform](https://platform.openai.com/) and select or
   create the project for LabPurchaseBot. API billing must be enabled separately
   from the ChatGPT subscription.
2. Create a secret key from [API keys](https://platform.openai.com/api-keys).
   The bot needs permission to create Responses. Keep the key out of chat and
   screenshots. Use the platform's usage/budget controls as appropriate; a budget
   alert should not be assumed to be a hard spending cap.
3. Run the local masked setup dialog:

   ```powershell
   .\.venv\Scripts\pythonw.exe configure_api.py
   ```

4. Paste the key into that local dialog and save it. It updates only the bot's
   ignored `.env` file with `LABPURCHASE_OPENAI_API_KEY`,
   `LABPURCHASE_API_MODEL=gpt-5.6-luna`, and `LABEL_READER_ENABLED=true`.
5. The current listener does not use this key. A successful photo extraction
   must still be checked before claiming API access is working. Local unit tests
   use mocked API responses and do not establish model access or billing readiness.

No generic `OPENAI_API_KEY`, Codex auth file, or connector credentials are borrowed.
The current bot instead waits for a valid Codex ChatGPT login when needed.

## Gmail and Google Sheets

These integrations need separate Google authorization for the background service
using `linjhumse@gmail.com`, plus the target spreadsheet link. The current chat's
Google connectors identify a different account and their login cannot be handed
to a standalone Python process. Do not read that other mailbox or change its files.

The intended next setup is a Google desktop OAuth client with Gmail read access
and access to the selected spreadsheet, with credentials kept under ignored
`.local/`. Mail sending, deletion, and mailbox modification are outside the requested
scope. Email text, captions and cell contents are data, not permission to change
the bot's configuration or execute arbitrary actions. Spreadsheet integration
must inspect the actual workbook layout before choosing columns or writing data.
