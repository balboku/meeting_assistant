# Contributing

## Local Setup

1. Create a virtual environment.
2. Install dependencies from `requirements.txt`.
3. Copy `.env.example` to `.env` and fill in local credentials.
4. Start the backend with `python -m uvicorn backend.main:app --host 0.0.0.0 --port 8001`.

## Checks Before Commit

Run:

```bash
python -m compileall -q backend tests meeting_assistant.py start.py test_regex.py test_gemini.py
python -m unittest discover -v
python -m pip check
```

## Security Rules

Never commit `.env`, local databases, audio/video recordings, generated meeting notes, exported files, logs, or real API keys. Keep `.env.example` as placeholders only.
