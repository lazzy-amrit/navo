# Navo

Navo is a small database hosting platform. Each account gets one hosted database, a query console, and server-enforced plan limits.

See the rest of the source in this repository.

## Architecture

- FastAPI, server-rendered HTML (no SPA)
- Navo application data: SQLite + Peewee in Database/
- User-hosted databases are separate and go through Turso/
- Every database operation resolves authenticated user, then that user's database, then the operation

## Plans

| Plan | Storage | Reads / month | Writes / month | Databases |
| --- | --- | --- | --- | --- |
| Free | 60 MB | 5,000,000 | 75,000 | 1 |
| Pro | 200 MB | 10,000,000 | 400,000 | 1 |

Pro is activated manually.

## Run

```bash
pip install -r requirements.txt
cp .env.example .env
python3 -m uvicorn main:app --host 0.0.0.0 --port 8080
```

Admin: /admin/login with ADMIN_PASSWORD.
