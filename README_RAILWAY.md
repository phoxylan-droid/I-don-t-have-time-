# Netflix Hub Bot — Railway

## Deploy (zero errors)

1. New Railway project → **Empty project**
2. **Add service** → upload this folder / connect Git
3. Variables (optional — defaults already in code):
   - `BOT_TOKEN` or `NETFLIX_CHECKER_BOT_TOKEN` = your telegram token
   - `ADMIN_IDS` = `7200716402` (comma-separated)
   - `DAILY_LINK_LIMIT` = `5`
   - `MAX_THREADS` = `12`
4. Settings:
   - **Start command:** `python netflix_cookie_checker_bot.py`
   - **Restart policy:** on failure
5. Deploy

## Important

- This is a **worker** bot (long polling). Not a web HTTP service.
- Do **not** set a public HTTP port requirement if Railway asks — use worker/start command only.
- If build fails on Python version, `runtime.txt` pins **3.11.9**.

## Files

| File | Purpose |
|------|---------|
| `netflix_cookie_checker_bot.py` | bot |
| `requirements.txt` | deps |
| `Procfile` | worker process |
| `runtime.txt` | Python 3.11 |
| `railway.toml` | start + restart |
| `nixpacks.toml` | build |
| `start.sh` | optional shell start |

## Local test

```bash
pip install -r requirements.txt
python netflix_cookie_checker_bot.py
```
