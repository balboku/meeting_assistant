# Security Policy

## Secrets

Do not commit real credentials, tokens, API keys, ngrok auth tokens, LINE tokens, local databases, recordings, generated meeting notes, logs, or exported documents.

Company-specific Word templates, including `4-QA-005 V01 會議紀錄.docx`, should remain local and be configured with `MEETING_DOCX_TEMPLATE_PATH`.

Use `.env.example` as the template and keep real values only in `.env`.

Required local secrets:

- `GEMINI_API_KEY`
- `APP_API_KEY`
- `LINE_CHANNEL_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `NGROK_AUTHTOKEN`
- `NGROK_API_KEY`

## Local Data

The following paths are runtime data and must stay out of Git:

- `meetings.db*`
- `temp/`
- `output/`
- `backups/`
- `logs/`

## Remote Access

The backend allows local and trusted LAN access by default. Public routes through ngrok or other tunnels should use `APP_API_KEY`. Rotate `APP_API_KEY`, ngrok keys, and LINE tokens immediately if they are exposed.

## Reporting

For a private deployment, report suspected leaks or security issues directly to the repository owner. Do not open public issues containing credentials, recordings, meeting content, customer data, or webhook secrets.
