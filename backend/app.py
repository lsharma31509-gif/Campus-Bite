import functools
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import tempfile
import time
from datetime import date, datetime, timedelta

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
import psycopg
from psycopg.rows import dict_row
from werkzeug.security import check_password_hash, generate_password_hash

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_ROOT = os.path.join(PROJECT_ROOT, "frontend")

app = Flask(
    __name__,
    template_folder=os.path.join(FRONTEND_ROOT, "templates"),
    static_folder=os.path.join(FRONTEND_ROOT, "static"),
    instance_path=os.path.join(PROJECT_ROOT, "instance"),
)
PICKUP_LOCATIONS = {
    f"Building {building} - Ground Floor" for building in range(25, 39)
}
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_VERCEL = os.environ.get("VERCEL") == "1"
SECRET_KEY = os.environ.get("SECRET_KEY")
ADMIN_REGISTRATION_ID = os.environ.get("ADMIN_REGISTRATION_ID", "").strip()

if IS_VERCEL and not SECRET_KEY:
    raise RuntimeError("Set SECRET_KEY in the Vercel project environment variables.")
if IS_VERCEL and not DATABASE_URL:
    raise RuntimeError("Set DATABASE_URL to a Supabase PostgreSQL connection string.")
if IS_VERCEL and not re.fullmatch(r"[0-9]{8}", ADMIN_REGISTRATION_ID):
    raise RuntimeError("Set ADMIN_REGISTRATION_ID to the 8-digit admin account ID.")

app.config.update(
    SECRET_KEY=SECRET_KEY or secrets.token_hex(32),
    DATABASE=os.path.join(app.instance_path, "campusbite.sqlite3"),
    DATABASE_URL=DATABASE_URL,
    ADMIN_REGISTRATION_ID=ADMIN_REGISTRATION_ID,
    ADMIN_SNAPSHOT_PATH=os.path.join(os.path.dirname(__file__), "dashboard_output.json"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)


class PostgresConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, parameters=()):
        return self.connection.execute(query.replace("?", "%s"), parameters)

    def executemany(self, query, parameters):
        return self.connection.executemany(query.replace("?", "%s"), parameters)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self.connection.__exit__(exc_type, exc_value, traceback)


def get_db():
    if "db" not in g:
        if app.config["DATABASE_URL"]:
            connection = psycopg.connect(
                app.config["DATABASE_URL"],
                row_factory=dict_row,
                prepare_threshold=None,
            )
            g.db = PostgresConnection(connection)
        else:
            g.db = sqlite3.connect(app.config["DATABASE"])
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def init_db():
    if app.config["DATABASE_URL"]:
        with get_postgres_connection() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    registration_id TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS login_attempts (
                    id BIGSERIAL PRIMARY KEY,
                    registration_id TEXT NOT NULL,
                    remote_addr TEXT NOT NULL,
                    attempted_at BIGINT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS login_attempts_lookup
                ON login_attempts (registration_id, remote_addr, attempted_at)
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS login_events (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id),
                    logged_in_at BIGINT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS login_events_recent
                ON login_events (logged_in_at DESC)
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS carts (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL UNIQUE REFERENCES users(id),
                    updated_at BIGINT NOT NULL,
                    pickup_location TEXT NOT NULL DEFAULT ''
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS cart_items (
                    id BIGSERIAL PRIMARY KEY,
                    cart_id BIGINT NOT NULL REFERENCES carts(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    base_price DOUBLE PRECISION NOT NULL,
                    portion TEXT NOT NULL CHECK (portion IN ('full', 'half')),
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    UNIQUE (cart_id, name, portion)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id),
                    pickup_location TEXT NOT NULL,
                    total DOUBLE PRECISION NOT NULL,
                    created_at BIGINT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS order_items (
                    id BIGSERIAL PRIMARY KEY,
                    order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    base_price DOUBLE PRECISION NOT NULL,
                    portion TEXT NOT NULL CHECK (portion IN ('full', 'half')),
                    quantity INTEGER NOT NULL CHECK (quantity > 0)
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS orders_recent ON orders (created_at DESC)"
            )
        return

    os.makedirs(app.instance_path, exist_ok=True)
    with sqlite3.connect(app.config["DATABASE"]) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                registration_id TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY,
                registration_id TEXT NOT NULL,
                remote_addr TEXT NOT NULL,
                attempted_at INTEGER NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS login_attempts_lookup
            ON login_attempts (registration_id, remote_addr, attempted_at)
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS login_events (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                logged_in_at INTEGER NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS login_events_recent
            ON login_events (logged_in_at DESC)
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS carts (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
                updated_at INTEGER NOT NULL,
                pickup_location TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cart_columns = {
            column[1] for column in db.execute("PRAGMA table_info(carts)")
        }
        if "pickup_location" not in cart_columns:
            db.execute(
                "ALTER TABLE carts ADD COLUMN pickup_location TEXT NOT NULL DEFAULT ''"
            )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS cart_items (
                id INTEGER PRIMARY KEY,
                cart_id INTEGER NOT NULL REFERENCES carts(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                base_price REAL NOT NULL,
                portion TEXT NOT NULL CHECK (portion IN ('full', 'half')),
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                UNIQUE (cart_id, name, portion)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                pickup_location TEXT NOT NULL,
                total REAL NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                base_price REAL NOT NULL,
                portion TEXT NOT NULL CHECK (portion IN ('full', 'half')),
                quantity INTEGER NOT NULL CHECK (quantity > 0)
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS orders_recent ON orders (created_at DESC)"
        )


def get_postgres_connection():
    connection = psycopg.connect(
        app.config["DATABASE_URL"],
        row_factory=dict_row,
        prepare_threshold=None,
    )
    return PostgresConnection(connection)


def format_local_timestamp(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def get_daily_order_totals(db, start_timestamp):
    daily_totals = {}
    for row in db.execute(
        "SELECT created_at, total FROM orders WHERE created_at >= ?",
        (start_timestamp,),
    ).fetchall():
        order_day = datetime.fromtimestamp(row["created_at"]).date().isoformat()
        totals = daily_totals.setdefault(
            order_day, {"order_count": 0, "revenue": 0.0}
        )
        totals["order_count"] += 1
        totals["revenue"] += float(row["total"])
    return daily_totals


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def csrf_token():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_urlsafe(32)
    return session["_csrf_token"]


def require_csrf_token():
    token = session.get("_csrf_token", "")
    submitted = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    if not token or not hmac.compare_digest(token, submitted):
        abort(400, description="The form expired. Reload the page and try again.")


def render_auth(mode, error=None, registration_id=""):
    return render_template(
        "auth.html",
        mode=mode,
        error=error,
        registration_id=registration_id,
        csrf_token=csrf_token(),
    )


def login_required(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        user = get_db().execute(
            "SELECT registration_id FROM users WHERE id = ?",
            (session["user_id"],),
        ).fetchone()
        if user is None:
            session.clear()
            return redirect(url_for("login"))
        g.registration_id = user["registration_id"]
        return view(**kwargs)

    return wrapped_view


def login_is_rate_limited(registration_id, remote_addr, now):
    attempts = get_db().execute(
        """
        SELECT COUNT(*) AS count FROM login_attempts
        WHERE registration_id = ? AND remote_addr = ? AND attempted_at > ?
        """,
        (registration_id, remote_addr, now - 600),
    ).fetchone()["count"]
    return attempts >= 5


def is_admin(registration_id):
    admin_registration_id = app.config["ADMIN_REGISTRATION_ID"]
    return bool(
        re.fullmatch(r"[0-9]{8}", admin_registration_id)
        and hmac.compare_digest(registration_id, admin_registration_id)
    )


def parse_cart_items(payload):
    if not isinstance(payload, list) or len(payload) > 100:
        raise ValueError("Cart can contain at most 100 item rows.")

    items_by_key = {}
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Cart item has an invalid format.")

        name = item.get("name")
        category = item.get("category")
        portion = item.get("portion")
        price = item.get("price")
        quantity = item.get("quantity")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("Each item needs a valid name.")
        if category not in ("meals", "snacks", "beverages"):
            raise ValueError("Each item needs a valid category.")
        if portion not in ("full", "half"):
            raise ValueError("Each item needs a valid portion.")
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            raise ValueError("Each item needs a valid price.")
        if price <= 0 or price > 100000 or not math.isfinite(price):
            raise ValueError("Item price must be greater than ₹0 and at most ₹100,000.")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 99:
            raise ValueError("Item quantity must be between 1 and 99.")

        key = (name.strip(), portion)
        if key in items_by_key:
            raise ValueError("Cart contains a duplicate item row.")
        items_by_key[key] = {
            "name": name.strip(),
            "category": category,
            "base_price": round(float(price), 2),
            "portion": portion,
            "quantity": quantity,
        }

    return list(items_by_key.values())


def item_unit_price(item):
    return item["base_price"] / 2 if item["portion"] == "half" else item["base_price"]


def item_total(item):
    return item_unit_price(item) * item["quantity"]


def authenticated_json_body():
    if not request.is_json:
        return None, (jsonify(error="Send the request as JSON."), 415)
    payload = request.get_json()
    if not isinstance(payload, dict):
        return None, (jsonify(error="Request body must be a JSON object."), 400)
    return payload, None


@app.route("/")
@login_required
def home():
    return render_template(
        "index.html",
        registration_id=g.registration_id,
        csrf_token=csrf_token(),
        is_admin=is_admin(g.registration_id),
    )


@app.route("/admin")
@login_required
def admin_dashboard():
    if not is_admin(g.registration_id):
        abort(403)

    db = get_db()
    stats = db.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM users) AS user_count,
            (SELECT COUNT(*) FROM login_events) AS login_count,
            (SELECT COUNT(DISTINCT user_id) FROM login_events) AS logged_in_users,
            (SELECT COUNT(*) FROM orders) AS order_count,
            (SELECT COUNT(*) FROM carts c
             WHERE EXISTS (SELECT 1 FROM cart_items ci WHERE ci.cart_id = c.id))
                AS active_cart_count,
            (SELECT COALESCE(SUM(total), 0) FROM orders) AS revenue
        """
    ).fetchone()

    today = date.today()
    chart_start = today - timedelta(days=6)
    start_timestamp = int(datetime.combine(chart_start, datetime.min.time()).timestamp())
    daily_by_date = get_daily_order_totals(db, start_timestamp)
    chart_days = []
    for day_offset in range(7):
        chart_date = chart_start + timedelta(days=day_offset)
        row = daily_by_date.get(chart_date.isoformat())
        chart_days.append({
            "label": chart_date.strftime("%a"),
            "date": chart_date.strftime("%b %d"),
            "orders": row["order_count"] if row else 0,
            "revenue": round(row["revenue"], 2) if row else 0,
        })
    max_daily_orders = max((day["orders"] for day in chart_days), default=0)
    max_daily_revenue = max((day["revenue"] for day in chart_days), default=0)
    for day in chart_days:
        day["orders_height"] = (
            round(day["orders"] / max_daily_orders * 100) if max_daily_orders else 0
        )
        day["revenue_height"] = (
            round(day["revenue"] / max_daily_revenue * 100) if max_daily_revenue else 0
        )

    logins = db.execute(
        """
        SELECT u.registration_id,
               le.logged_in_at
        FROM login_events le
        JOIN users u ON u.id = le.user_id
        ORDER BY le.logged_in_at DESC, le.id DESC
        LIMIT 30
        """
    ).fetchall()
    logins = [
        {**dict(row), "logged_in_at": format_local_timestamp(row["logged_in_at"])}
        for row in logins
    ]

    carts = db.execute(
        """
        SELECT c.id, u.registration_id, c.pickup_location,
               c.updated_at
        FROM carts c
        JOIN users u ON u.id = c.user_id
        WHERE EXISTS (SELECT 1 FROM cart_items ci WHERE ci.cart_id = c.id)
        ORDER BY c.updated_at DESC
        LIMIT 100
        """
    ).fetchall()
    carts = [
        {**dict(row), "updated_at": format_local_timestamp(row["updated_at"])}
        for row in carts
    ]
    cart_items_by_id = {}
    if carts:
        cart_ids = [cart["id"] for cart in carts]
        placeholders = ",".join("?" for _ in cart_ids)
        for item in db.execute(
            f"""
            SELECT cart_id, name, category, base_price, portion, quantity
            FROM cart_items
            WHERE cart_id IN ({placeholders})
            ORDER BY name
            """,
            cart_ids,
        ).fetchall():
            cart_items_by_id.setdefault(item["cart_id"], []).append(dict(item))
    active_carts = [
        {**dict(cart), "items": cart_items_by_id.get(cart["id"], [])}
        for cart in carts
    ]

    orders = db.execute(
        """
        SELECT o.id, u.registration_id, o.pickup_location, o.total,
               o.created_at
        FROM orders o
        JOIN users u ON u.id = o.user_id
        ORDER BY o.created_at DESC, o.id DESC
        LIMIT 100
        """
    ).fetchall()
    orders = [
        {**dict(row), "created_at": format_local_timestamp(row["created_at"])}
        for row in orders
    ]
    order_items_by_id = {}
    if orders:
        order_ids = [order["id"] for order in orders]
        placeholders = ",".join("?" for _ in order_ids)
        for item in db.execute(
            f"""
            SELECT order_id, name, category, base_price, portion, quantity
            FROM order_items
            WHERE order_id IN ({placeholders})
            ORDER BY name
            """,
            order_ids,
        ).fetchall():
            order_items_by_id.setdefault(item["order_id"], []).append(dict(item))
    recent_orders = [
        {**dict(order), "items": order_items_by_id.get(order["id"], [])}
        for order in orders
    ]

    return render_template(
        "admin.html",
        stats=stats,
        chart_days=chart_days,
        logins=logins,
        active_carts=active_carts,
        orders=recent_orders,
        admin_registration_id=g.registration_id,
        csrf_token=csrf_token(),
    )


@app.route("/api/admin/summary")
def admin_summary_api():
    if "user_id" not in session:
        return jsonify(error="Admin login required. Sign in, then open this API URL."), 401

    user = get_db().execute(
        "SELECT registration_id FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()
    if user is None:
        session.clear()
        return jsonify(error="Your session expired. Sign in again."), 401
    if not is_admin(user["registration_id"]):
        return jsonify(error="Admin access required."), 403

    db = get_db()
    totals = db.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM users) AS registered_accounts,
            (SELECT COUNT(*) FROM login_events) AS successful_sign_ins,
            (SELECT COUNT(DISTINCT user_id) FROM login_events) AS unique_signed_in_users,
            (SELECT COUNT(*) FROM orders) AS total_orders,
            (SELECT COUNT(*) FROM carts c
             WHERE EXISTS (SELECT 1 FROM cart_items ci WHERE ci.cart_id = c.id))
                AS active_carts,
            (SELECT COALESCE(SUM(total), 0) FROM orders) AS all_time_order_value
        """
    ).fetchone()

    today = date.today()
    chart_start = today - timedelta(days=6)
    start_timestamp = int(datetime.combine(chart_start, datetime.min.time()).timestamp())
    daily_by_date = get_daily_order_totals(db, start_timestamp)
    last_seven_days = []
    for day_offset in range(7):
        chart_date = chart_start + timedelta(days=day_offset)
        row = daily_by_date.get(chart_date.isoformat())
        last_seven_days.append({
            "date": chart_date.isoformat(),
            "label": chart_date.strftime("%a"),
            "orders": row["order_count"] if row else 0,
            "order_value": round(row["revenue"], 2) if row else 0,
        })

    cart_rows = db.execute(
        """
        SELECT c.id, u.registration_id, c.pickup_location,
               c.updated_at,
               ci.name, ci.category, ci.base_price, ci.portion, ci.quantity
        FROM carts c
        JOIN users u ON u.id = c.user_id
        JOIN cart_items ci ON ci.cart_id = c.id
        ORDER BY c.updated_at DESC, ci.name
        """
    ).fetchall()
    active_carts = {}
    for row in cart_rows:
        cart = active_carts.setdefault(row["id"], {
            "registration_id": row["registration_id"],
            "pickup_location": row["pickup_location"],
            "updated_at": format_local_timestamp(row["updated_at"]),
            "items": [],
            "total": 0,
        })
        unit_price = row["base_price"] / 2 if row["portion"] == "half" else row["base_price"]
        line_total = round(unit_price * row["quantity"], 2)
        cart["items"].append({
            "name": row["name"],
            "category": row["category"],
            "portion": row["portion"],
            "quantity": row["quantity"],
            "unit_price": round(unit_price, 2),
            "line_total": line_total,
        })
        cart["total"] = round(cart["total"] + line_total, 2)

    order_rows = db.execute(
        """
        WITH recent AS (
            SELECT o.id, u.registration_id, o.pickup_location, o.total,
                   o.created_at
            FROM orders o
            JOIN users u ON u.id = o.user_id
            ORDER BY o.created_at DESC, o.id DESC
            LIMIT 100
        )
        SELECT recent.id, recent.registration_id, recent.pickup_location,
               recent.total, recent.created_at,
               oi.name, oi.category, oi.portion, oi.quantity, oi.base_price
        FROM recent
        LEFT JOIN order_items oi ON oi.order_id = recent.id
        ORDER BY recent.created_at DESC, recent.id DESC, oi.name
        """
    ).fetchall()
    recent_orders = {}
    for row in order_rows:
        order = recent_orders.setdefault(row["id"], {
            "id": row["id"],
            "registration_id": row["registration_id"],
            "pickup_location": row["pickup_location"],
            "total": round(row["total"], 2),
            "created_at": format_local_timestamp(row["created_at"]),
            "items": [],
        })
        if row["name"] is not None:
            order["items"].append({
                "name": row["name"],
                "category": row["category"],
                "portion": row["portion"],
                "quantity": row["quantity"],
                "base_price": round(row["base_price"], 2),
            })

    recent_sign_ins = [
        {
            "registration_id": row["registration_id"],
            "signed_in_at": format_local_timestamp(row["signed_in_at"]),
        }
        for row in db.execute(
            """
            SELECT u.registration_id,
                   le.logged_in_at AS signed_in_at
            FROM login_events le
            JOIN users u ON u.id = le.user_id
            ORDER BY le.logged_in_at DESC, le.id DESC
            LIMIT 30
            """
        ).fetchall()
    ]

    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "totals": {key: value for key, value in dict(totals).items()},
        "last_7_days": last_seven_days,
        "active_carts": list(active_carts.values()),
        "recent_orders": list(recent_orders.values()),
        "recent_sign_ins": recent_sign_ins,
    }

    if not app.config["DATABASE_URL"]:
        snapshot_path = app.config["ADMIN_SNAPSHOT_PATH"]
        snapshot_dir = os.path.dirname(snapshot_path)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=snapshot_dir,
                prefix=".dashboard_output.",
                suffix=".tmp",
                delete=False,
            ) as snapshot_file:
                temporary_path = snapshot_file.name
                json.dump(
                    payload,
                    snapshot_file,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                )
                snapshot_file.write("\n")
            os.replace(temporary_path, snapshot_path)
        except OSError:
            app.logger.exception("Unable to write the admin dashboard JSON snapshot")
            if temporary_path and os.path.exists(temporary_path):
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    app.logger.exception(
                        "Unable to remove the temporary dashboard JSON file"
                    )
            return (
                jsonify(
                    error="Could not save backend/dashboard_output.json. "
                    "Check folder permissions."
                ),
                500,
            )

    return jsonify(payload)


@app.route("/api/cart", methods=["GET", "POST"])
@login_required
def user_cart():
    db = get_db()
    if request.method == "GET":
        cart = db.execute(
            "SELECT id, pickup_location FROM carts WHERE user_id = ?",
            (session["user_id"],)
        ).fetchone()
        if cart is None:
            return jsonify(items=[], pickup_location="")
        items = db.execute(
            """
            SELECT name, category, base_price AS price, portion, quantity
            FROM cart_items
            WHERE cart_id = ?
            ORDER BY id
            """,
            (cart["id"],),
        ).fetchall()
        return jsonify(items=[dict(item) for item in items], pickup_location=cart["pickup_location"])

    require_csrf_token()
    payload, error = authenticated_json_body()
    if error:
        return error
    try:
        items = parse_cart_items(payload.get("items"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    pickup_location = payload.get("pickup_location", "")
    if not isinstance(pickup_location, str) or (
        pickup_location and pickup_location not in PICKUP_LOCATIONS
    ):
        return jsonify(error="Select a valid canteen pickup location."), 400

    now = int(time.time())
    with db:
        cart = db.execute(
            "SELECT id FROM carts WHERE user_id = ?", (session["user_id"],)
        ).fetchone()
        if cart is None:
            cursor = db.execute(
                """
                INSERT INTO carts (user_id, updated_at, pickup_location)
                VALUES (?, ?, ?) RETURNING id
                """,
                (session["user_id"], now, pickup_location),
            )
            cart_id = cursor.fetchone()["id"]
        else:
            cart_id = cart["id"]
            db.execute(
                "UPDATE carts SET updated_at = ?, pickup_location = ? WHERE id = ?",
                (now, pickup_location, cart_id),
            )
            db.execute("DELETE FROM cart_items WHERE cart_id = ?", (cart_id,))
        db.executemany(
            """
            INSERT INTO cart_items
            (cart_id, name, category, base_price, portion, quantity)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (cart_id, item["name"], item["category"], item["base_price"],
                 item["portion"], item["quantity"])
                for item in items
            ],
        )
    return jsonify(ok=True, item_count=sum(item["quantity"] for item in items))


@app.route("/api/orders", methods=["POST"])
@login_required
def create_order():
    require_csrf_token()
    payload, error = authenticated_json_body()
    if error:
        return error

    try:
        items = parse_cart_items(payload.get("items"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if not items:
        return jsonify(error="Add at least one item before placing an order."), 400

    pickup_location = payload.get("pickup_location")
    if not isinstance(pickup_location, str) or pickup_location not in PICKUP_LOCATIONS:
        return jsonify(error="Select a valid canteen pickup location."), 400

    total = round(sum(item_total(item) for item in items), 2)
    now = int(time.time())
    db = get_db()
    with db:
        cursor = db.execute(
            """
            INSERT INTO orders (user_id, pickup_location, total, created_at)
            VALUES (?, ?, ?, ?) RETURNING id
            """,
            (session["user_id"], pickup_location, total, now),
        )
        order_id = cursor.fetchone()["id"]
        db.executemany(
            """
            INSERT INTO order_items
            (order_id, name, category, base_price, portion, quantity)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (order_id, item["name"], item["category"], item["base_price"],
                 item["portion"], item["quantity"])
                for item in items
            ],
        )
        cart = db.execute(
            "SELECT id FROM carts WHERE user_id = ?", (session["user_id"],)
        ).fetchone()
        if cart is not None:
            db.execute("DELETE FROM cart_items WHERE cart_id = ?", (cart["id"],))
            db.execute("UPDATE carts SET updated_at = ? WHERE id = ?", (now, cart["id"]))

    return jsonify(ok=True, order_id=order_id, total=total), 201


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("home"))

    error = None
    registration_id = ""
    if request.method == "POST":
        require_csrf_token()
        registration_id = request.form.get("registration_id", "").strip()
        password = request.form.get("password", "")

        if request.form.get("website", "").strip():
            return render_auth("register", "Unable to complete registration.")
        if not re.fullmatch(r"[0-9]{8}", registration_id):
            error = "Enter your 8-digit university registration ID."
        elif len(password) < 8 or len(password) > 128:
            error = "Choose a password between 8 and 128 characters."
        else:
            try:
                cursor = get_db().execute(
                    """
                    INSERT INTO users (registration_id, password_hash)
                    VALUES (?, ?) RETURNING id
                    """,
                    (registration_id, generate_password_hash(password)),
                )
                user_id = cursor.fetchone()["id"]
                get_db().commit()
            except (sqlite3.IntegrityError, psycopg.IntegrityError):
                error = "That registration ID already has an account. Try logging in."
            else:
                get_db().execute(
                    "INSERT INTO login_events (user_id, logged_in_at) VALUES (?, ?)",
                    (user_id, int(time.time())),
                )
                get_db().commit()
                session.clear()
                session["user_id"] = user_id
                return redirect(url_for("home"))

    return render_auth("register", error, registration_id)


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("home"))

    error = None
    registration_id = ""
    if request.method == "POST":
        require_csrf_token()
        registration_id = request.form.get("registration_id", "").strip()
        password = request.form.get("password", "")

        if request.form.get("website", "").strip():
            return render_auth("login", "Unable to complete login.")
        if not re.fullmatch(r"[0-9]{8}", registration_id):
            error = "Enter your 8-digit university registration ID."
        else:
            db = get_db()
            now = int(time.time())
            remote_addr = request.remote_addr or "unknown"
            if login_is_rate_limited(registration_id, remote_addr, now):
                error = "Too many attempts. Wait 10 minutes, then try again."
            else:
                user = db.execute(
                    "SELECT id, password_hash FROM users WHERE registration_id = ?",
                    (registration_id,),
                ).fetchone()
                if user and check_password_hash(user["password_hash"], password):
                    db.execute(
                        """
                        DELETE FROM login_attempts
                        WHERE registration_id = ? AND remote_addr = ?
                        """,
                        (registration_id, remote_addr),
                    )
                    db.execute(
                        "INSERT INTO login_events (user_id, logged_in_at) VALUES (?, ?)",
                        (user["id"], now),
                    )
                    db.commit()
                    session.clear()
                    session["user_id"] = user["id"]
                    return redirect(url_for("home"))

                db.execute(
                    """
                    INSERT INTO login_attempts
                    (registration_id, remote_addr, attempted_at)
                    VALUES (?, ?, ?)
                    """,
                    (registration_id, remote_addr, now),
                )
                db.execute(
                    "DELETE FROM login_attempts WHERE attempted_at < ?",
                    (now - 86400,),
                )
                db.commit()
                error = "Registration ID or password is incorrect."

    return render_auth("login", error, registration_id)


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    require_csrf_token()
    session.clear()
    flash("You have been logged out.")
    return redirect(url_for("login"))


init_db()
