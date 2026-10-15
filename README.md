# Authenova — Look Beyond the Surface

Two independent AI verification engines on one platform:

| Engine | What it does | How |
|---|---|---|
| **Verify Media** | Flags AI-generated, manipulated or deepfake images and videos | Hive AI detector; videos are checked frame by frame (6 frames captured in the browser) |
| **Investigate a Claim** | Says whether a claim is Likely Supported, Likely Contradicted or Inconclusive, with the evidence | Groq LLM plans searches → Tavily live web search → SBERT ranks passages → DeBERTa-v3 / LLM judges stance → Google Fact Check adds professional fact-checker verdicts → Groq writes the reasoning |

The engines never share data: media scores and claim verdicts are computed and shown separately.

## Run locally (Windows)
1. Copy `.env.example` to `.env` and fill in your keys.
2. Double-click `run.bat` (or run `python server.py`), then open http://127.0.0.1:5001/.
3. Optional: `check.bat` tests every key; `setup.bat` installs local SBERT + DeBERTa models.

No pip install is needed — the server uses only the Python standard library (Python 3.8+).

## Keys
| Variable | Used for | Get it |
|---|---|---|
| `HIVE_API_KEY` | Image + video detection | thehive.ai |
| `TAVILY_API_KEY` | Live web search | app.tavily.com |
| `GROQ_API_KEY` | Query planning, stance, reasoning | console.groq.com/keys |
| `FACTCHECK_API_KEY` | Professional fact-checks (optional) | Google Cloud → enable "Fact Check Tools API" |

## Deploy on Render
1. Push this repo to GitHub.
2. In Render: **New → Blueprint**, pick this repo (it uses `render.yaml`).
3. Enter the four keys when Render asks for environment variables, then deploy.

The server automatically listens on `0.0.0.0:$PORT` when Render sets `PORT`.

## API
| Endpoint | Body |
|---|---|
| `POST /api/claims/investigate` | JSON `{"claim": "...", "max_results": 4}` |
| `POST /api/media/verify-image` | multipart `file`, or JSON `{"url": "..."}` |
| `POST /api/media/verify-video` | multipart `file` (+ optional `frame` JPEGs) |

## Files
- `static/` — website (index.html, styles.css, app.js)
- `server.py` — web server + media endpoints
- `claim_engine.py` — Claim Intelligence engine
- `check_apis.py` / `check.bat` — live key check
- `render.yaml` — Render deployment blueprint
