# CampusBite

## Project layout

- `frontend/templates/` contains the pages rendered by Flask.
- `frontend/static/` contains the site's CSS and static assets.
- `frontend/prototype/` keeps the original standalone, frontend-only mockup.
- `backend/app.py` contains the Flask routes, authentication, and database logic.
- `instance/campusbite.sqlite3` stores local accounts, carts, sign-ins, and orders.
- `app.py` is the single local startup entry point.

## Run locally

From the project folder, set the admin account and start the website once:

```powershell
$env:ADMIN_REGISTRATION_ID = "12345678"
.\.venv\Scripts\python.exe app.py
```

Set `ADMIN_REGISTRATION_ID` to the 8-digit registration ID of an existing
account (or register that account first). Only that account can open
`/admin`; other signed-in accounts receive a forbidden response. Keep this
environment variable private and do not commit it.

Without `DATABASE_URL`, the SQLite database is stored at
`instance/campusbite.sqlite3`. Successful sign-ins, current carts and demo
orders are recorded there. Sign-in reporting starts when the tracking tables
are created; earlier sign-ins cannot be reconstructed. The admin dashboard
shows all-time totals and daily order and order-value charts for the latest
seven calendar days. Orders are illustrative only and are not sent to canteens
or payment services.

After signing in with the configured admin account, open `/api/admin/summary`
to view the backend's database-backed JSON response. The admin dashboard also
has an **Open backend JSON** link. With local SQLite, opening the API creates
or refreshes `backend/dashboard_output.json` with the latest snapshot. Signed-out
and non-admin requests are rejected.
The snapshot contains account IDs, cart contents and order details; treat it as
private local data and do not commit it.

Keep that PowerShell window open while using the site. Flask serves both the
frontend and backend from this one process; there is no separate frontend
server to start. Use the Flask website at `http://127.0.0.1:5000/`; the
standalone prototype is only a static mockup and does not connect to SQLite.

## Deploy to Vercel with Supabase

The root `app.py` and `vercel.json` configure the Flask app as a Vercel Python
function. Vercel's filesystem is not persistent, so configure Supabase
PostgreSQL instead of relying on the local SQLite file:

1. Create a Supabase project and copy its PostgreSQL connection string. Use the
   pooled connection string for serverless deployments and keep its password
   private.
2. In Vercel Project Settings → Environment Variables, set:
   - `DATABASE_URL`: the Supabase PostgreSQL connection string with SSL enabled
     (for example, `sslmode=require`).
   - `SECRET_KEY`: a persistent random value; generate one locally with
     `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
   - `ADMIN_REGISTRATION_ID`: the 8-digit registration ID of the admin account.
3. Add these variables to every Vercel environment where the app should run,
   then redeploy. The app creates its PostgreSQL tables at startup.

Do not commit these values, the SQLite database, or the local dashboard JSON.
The Supabase database starts empty; local SQLite accounts, carts, and orders are
not copied automatically. The JSON snapshot file is only written in local
SQLite mode; `/api/admin/summary` returns the live response in either mode.
