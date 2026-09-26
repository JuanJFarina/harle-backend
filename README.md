# Harle

Harle is a Telegram-first, multi-user AI personal assistant with a local development CLI.

Product direction and current work are defined in:

- [Vision](docs/01_VISION.md)
- [Features](docs/02_FEATURES.md)
- [Software Requirements Specification](docs/03_SRS.md)
- [Project Management Plan](docs/04_PMP.md)
- [Entity Relationship Diagram](docs/05_ERD.md)

## Setup

Create a `.env` file or set the environment variables directly:

```powershell
Copy-Item .env.example .env
```

Then edit `.env`:

```env
GEMINI_API_KEY=your_api_key_here
GEMINI_MODEL=gemini-2.5-flash
```

Install the CLI in editable mode:

```powershell
python -m pip install -e .
```

On Windows, if `gemini` is not found after installation, add your Python user scripts directory to `PATH`:

```powershell
$env:Path += ";$env:APPDATA\Python\Python313\Scripts"
```

## Development

This repository includes a devcontainer for a ready-to-use Linux Python environment. In Cursor or VS Code, reopen the project in the container to install the package with development tooling and set up pre-commit hooks.

For local development without the container, install the dev extra and hooks:

```powershell
python -m pip install -e ".[dev]"
pre-commit install
```

Run the full hook suite before committing:

```powershell
pre-commit run --all-files
```

## Usage

```powershell
gemini "what is the current trend in AI ?"
```

Use another model:

```powershell
gemini --model gemini-2.5-pro "summarize today's AI news"
```

## Legacy Google Sheets expenses

Harle can update Juan's private Google Sheets expense tracker when the legacy integration is configured. Telegram access is granted only when the resolved internal user UUID equals `LEGACY_GOOGLE_SHEETS_USER_ID`. The CLI requires that UUID through `--user-id`.

The integration supports one-time expenses and refunds, installment purchases, daily and monthly queries, and transaction corrections. Writes update the configured category cells by changing the existing formula:

```text
=100 -> =100+200 for a normal expense
=100 -> =100-200 for a refund
```

Required Google Sheets environment variables:

```env
LEGACY_GOOGLE_SHEETS_USER_ID=juan_internal_user_uuid_here
EXPENSES_SPREADSHEET_ID=your_expenses_spreadsheet_id_here
EXPENSES_NEXT_YEAR_SPREADSHEET_ID=your_next_year_expenses_spreadsheet_id_here
GOOGLE_SERVICE_ACCOUNT_JSON_BASE64=base64_encoded_service_account_json_here
```

To enable writes:

1. Enable the Google Sheets API in a Google Cloud project.
2. Create a service account and download its JSON credential.
3. Share the expenses spreadsheet with the service account `client_email` as Editor.
4. Base64-encode the JSON credential and set it as `GOOGLE_SERVICE_ACCOUNT_JSON_BASE64`.

PowerShell helper:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("service-account.json"))
```

The tool only writes to the confirmed category columns:

```text
B:I -> alquileres, servicios_esenciales, servicios_no_esenciales, hogar, transporte, salidas, shopping, otros
```

Columns for totals, moving averages, and past markers are never written by the tool.

## Notes

- This uses the official Gemini API through `google-genai`.
- Google Search grounding is enabled for every request.
- The command reads `GEMINI_API_KEY` and `GEMINI_MODEL` from the environment, a `.env` file in the current directory, or a `.env` file in this project directory.

## Telegram bot

This project also includes a small FastAPI Telegram webhook app. It reuses the Gemini request code and stores user accounts, profiles, personal history, and Telegram conversations in PostgreSQL.

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Run locally:

```powershell
uvicorn harle_api.app:harle_app --reload
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthcheck
```

Environment variables:

```env
GEMINI_API_KEY=your_api_key_here
GEMINI_MODEL=gemini-2.5-flash
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_BOT_USERNAME=your_bot_username_without_at
TELEGRAM_WEBHOOK_SECRET=your_random_webhook_secret_here
POSTGRES_DATABASE_URL=postgresql://user:password@host:5432/database?sslmode=require
POSTGRES_POOL_MIN_SIZE=1
POSTGRES_POOL_MAX_SIZE=5
GOOGLE_OAUTH_CLIENT_ID=your_google_oauth_client_id
GOOGLE_OAUTH_CLIENT_SECRET=your_google_oauth_client_secret
GOOGLE_OAUTH_REDIRECT_URI=https://app.example.com/api/auth/google/callback
FRONTEND_REDIRECT_URL=https://app.example.com/registro
FRONTEND_ORIGINS=["https://app.example.com"]
SESSION_SIGNING_SECRET=replace_with_at_least_32_random_characters
MERCADO_PAGO_ACCESS_TOKEN=your_mercado_pago_access_token
MERCADO_PAGO_WEBHOOK_SECRET=your_mercado_pago_webhook_secret
MERCADO_PAGO_TESTING=true
MERCADO_PAGO_TEST_PAYER_EMAIL=your_test_buyer_email@testuser.com
PAYMENT_CHECKOUT_RETURN_URL=https://app.example.com/suscripcion
LEGACY_GOOGLE_SHEETS_USER_ID=juan_internal_user_uuid_here
EXPENSES_SPREADSHEET_ID=your_expenses_spreadsheet_id_here
EXPENSES_NEXT_YEAR_SPREADSHEET_ID=your_next_year_expenses_spreadsheet_id_here
GOOGLE_SERVICE_ACCOUNT_JSON_BASE64=base64_encoded_service_account_json_here
```

Before deployment, apply every commercial schema script in order:

```powershell
psql "$env:POSTGRES_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/apply_multi_user_runtime.sql
psql "$env:POSTGRES_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/apply_subscription_interactions.sql
psql "$env:POSTGRES_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/apply_interaction_frequency.sql
psql "$env:POSTGRES_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/apply_internal_expenses.sql
psql "$env:POSTGRES_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/apply_internal_events.sql
psql "$env:POSTGRES_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/apply_event_notification_quotas.sql
psql "$env:POSTGRES_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/apply_telegram_dedup_ordering.sql
psql "$env:POSTGRES_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/apply_bans_quotas.sql
psql "$env:POSTGRES_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/apply_web_registration.sql
psql "$env:POSTGRES_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/apply_paid_subscriptions.sql
```

Then provision every allowed beta user:

```powershell
python scripts/provision_user.py `
  --telegram-id 123456789 `
  --display-name "User Name" `
  --plan-code basic `
  --subscription-status active `
  --preferred-name "Preferred Name" `
  --locale en-US `
  --timezone UTC `
  --assistant-display-name Harle `
  --assistant-profile-text "A concise and transparent AI personal assistant."
```

Create the bot with Telegram's `@BotFather`, then register the Render webhook:

```powershell
Invoke-RestMethod `
  -Uri "https://api.telegram.org/bot$env:TELEGRAM_BOT_TOKEN/setWebhook" `
  -Method Post `
  -Body @{
    url = "https://your-render-service.onrender.com/telegram/webhook"
    secret_token = $env:TELEGRAM_WEBHOOK_SECRET
  }
```

Render Web Service settings:

```text
Build command: python -m pip install -r requirements.txt
Start command: uvicorn harle_api.app:harle_app --host 0.0.0.0 --port $PORT
Health check path: /healthcheck
```

`POSTGRES_DATABASE_URL` is required for the Telegram runtime. If your provider requires SSL, include it in the URL, for example `?sslmode=require`. `POSTGRES_POOL_MIN_SIZE` and `POSTGRES_POOL_MAX_SIZE` are optional connection pool settings.

FastAPI validates the schema at startup but never creates or alters it. Each webhook resolves the Telegram sender to an active internal user and builds an isolated request runtime from that user's PostgreSQL profile and conversation data.

The CLI remains a local development interface with file-backed conversations. SQLite remains available only as a non-commercial compatibility adapter.
