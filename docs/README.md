# Studiothree Discover — Project Setup

This guide walks you through setting up and running the Studiothree Discover backend API.

## Prerequisites

- **Python 3.12** for production, pinned in `.python-version` (the file Render reads);
  **3.9+** for local
- **PostgreSQL** – running and accessible
- **Redis** – running and accessible (sessions, OTP, and Socket.IO pub/sub)
- (Optional) **AWS SES + credentials** – for OTP and password-reset emails
- (Optional) **AWS S3** – for media uploads (local disk fallback when unset)

## 1. Clone and enter the project

```bash
cd /path/to/Server
```

## 2. Virtual environment

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
```

## 3. Install dependencies

```bash
pip install -r requirements.txt
```

## 4. Environment variables

Environment is selected by `FLASK_ENV`:

- **Development:** `FLASK_ENV=development` → loads `.env` then `.env.development`
- **Production:** `FLASK_ENV=production` → loads `.env` then `.env.production`

**Steps:**

1. Copy the example file:
   ```bash
   cp .env.example .env.development
   ```
2. Edit `.env.development` and set at least:
   - `DATABASE_URL` – PostgreSQL connection string (e.g. `postgresql://user:password@localhost:5432/flask_app_dev`)
   - `REDIS_URL` – Redis connection (e.g. `redis://localhost:6379/0`)
   - `JWT_SECRET` – secret for signing JWTs (use a strong value in production)
   - `SECRET_KEY` – Flask secret (use a strong value in production)

Optional for full features:

- **SES** – `SES_FROM_EMAIL` (verified SES sender) plus AWS credentials used by boto3 for OTP/reset email
- **S3** – `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `S3_BUCKET`, `S3_PUBLIC_BASE_URL` (all required together for real uploads)
- **Firebase** – `FIREBASE_SERVICE_ACCOUNT_JSON` or `FIREBASE_SERVICE_ACCOUNT_PATH` for push
- **CORS / Socket.IO** – `FRONTEND_URL` (primary origin, local web app `http://localhost:5173`) plus optional `CORS_ORIGINS` (comma-separated extra origins; credentials enabled). Development also allows `:5173`, `:5713`, and `:3000`.
- **Stripe** – leave `STRIPE_SECRET_KEY` unset until payment capture is implemented (confirm auto-pays in that mode)

## 5. Database

1. Create a PostgreSQL database (e.g. `flask_app_dev`).
2. Run migrations from the project root:
   ```bash
   alembic upgrade head
   ```
3. To create a new migration after changing models:
   ```bash
   alembic revision --autogenerate -m "Describe your change"
   alembic upgrade head
   ```

## 6. Redis

Ensure Redis is running and reachable at the URL set in `REDIS_URL`. The app uses Redis for:

- Sessions (login state)
- OTP storage and rate limits
- Socket.IO message fan-out across workers/instances

## 7. Run the application

**Development:**

```bash
FLASK_ENV=development python3 run.py
```

Or, with default env as development:

```bash
python run.py
```

The server listens on `0.0.0.0:PORT` (default `PORT=9000`) with Socket.IO enabled.

**Production (Gunicorn + gevent):**

```bash
FLASK_ENV=production gunicorn -k gevent -w 1 -b 0.0.0.0:9000 wsgi:app
```

Use **one** gevent worker (`-w 1`). Socket.IO requires the gevent worker class; do not use sync workers for production chat.

Set `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET`, and `SECRET_KEY` in `.env.production` or the process environment.

## 8. Verify

- **Health:** Open or curl `http://localhost:9000/`  
  Expected: `{ "message": "Studiothree Discover API running", "s3": { ... } }`
- **API:** See [API documentation](API.md) for endpoints and examples.
- **Postman:** Import [`postman/Studiothree_Discover_API.postman_collection.json`](../postman/Studiothree_Discover_API.postman_collection.json).

## Troubleshooting

| Issue | What to check |
|-------|----------------|
| `Database connection failed` | PostgreSQL is running; `DATABASE_URL` is correct; DB exists; network/firewall. |
| `Redis connection failed` | Redis is running; `REDIS_URL` is correct. |
| `ModuleNotFoundError: src` | Run commands from the **project root** (where `run.py` and `src/` are). |
| OTP / reset emails not sent | `SES_FROM_EMAIL` set; AWS credentials valid for SES; no errors in `logs/error.log`. |
| Socket.IO / chat realtime broken | Redis up; prod started with `gunicorn -k gevent -w 1`; `FRONTEND_URL` / `CORS_ORIGINS` include the web client origin. |
| `do not call blocking functions from the mainloop` | Do not use eventlet; use gevent + Python 3.12 (see `.python-version`). |

Logs are written under the `logs/` directory (e.g. `error.log`, `combined.log`).
