import hmac
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import URL
from sqlalchemy.dialects.sqlite import insert

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

app = Flask(__name__)
MODE = os.getenv("SWIFTPAY_MODE", "demo")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
FLW_SECRET_KEY = os.getenv("FLW_SECRET_KEY", "")
FLW_SECRET_HASH = os.getenv("FLW_SECRET_HASH", "")
FLW_API = "https://api.flutterwave.com/v3"
CURRENCIES = {"USD", "KES", "EUR", "GBP", "NGN", "INR", "CAD", "AUD"}

if MODE not in {"demo", "flutterwave-test"}:
    raise RuntimeError("SWIFTPAY_MODE must be demo or flutterwave-test.")
if MODE == "flutterwave-test":
    origin = urlsplit(PUBLIC_BASE_URL)
    if not (
        FLW_SECRET_KEY.startswith("FLWSECK_TEST-")
        and FLW_SECRET_HASH
        and origin.scheme == "https"
        and origin.hostname
        and not (origin.username or origin.password or origin.path or origin.query or origin.fragment)
    ):
        raise RuntimeError("Flutterwave test mode needs a test key, webhook hash and public HTTPS origin.")

default_db = "instance/swiftpay.db" if MODE == "demo" else "instance/swiftpay-test.db"
db_path = Path(os.getenv("SWIFTPAY_DB_PATH") or default_db).expanduser()
db_path = (ROOT / db_path).resolve()
db_path.parent.mkdir(parents=True, exist_ok=True)
app.config["SQLALCHEMY_DATABASE_URI"] = URL.create("sqlite", database=str(db_path))
db = SQLAlchemy(app)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), default="")
    wallets = db.relationship("Wallet", backref="user", lazy=True)


class Wallet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    balance = db.Column(db.Numeric(18, 2), nullable=False, default=0)
    __table_args__ = (db.UniqueConstraint("user_id", "currency"),)


class LedgerEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    amount = db.Column(db.Numeric(18, 2), nullable=False)
    kind = db.Column(db.String(24), nullable=False)
    ref = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=db.func.now())


class PendingTopUp(db.Model):
    ref = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    amount = db.Column(db.Numeric(18, 2), nullable=False)


def request_data():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def get_profile(data):
    email, name = data.get("email"), data.get("name", "")
    if not isinstance(email, str) or not isinstance(name, str) or len(name.strip()) > 120:
        return None
    email = email.strip().lower()
    if len(email) <= 255 and re.fullmatch(r"[^@\s]+@[^@\s]+", email):
        return email, name.strip()
    return None


def get_user(email, name):
    query = db.select(User).filter_by(email=email)
    user = db.session.scalar(query)
    if user is None:
        db.session.execute(
            insert(User).values(email=email, name=name or email.split("@")[0][:120])
            .on_conflict_do_nothing(index_elements=["email"])
        )
        db.session.commit()
        user = db.session.scalar(query)
    return user


def apply_deposit(user_id, currency, amount, ref):
    # Record the deposit and update its balance within the same transaction.
    entry = db.session.execute(
        insert(LedgerEntry).values(user_id=user_id, currency=currency, amount=amount,
                                   kind="deposit", ref=ref)
        .on_conflict_do_nothing(index_elements=["ref"])
    )
    if not entry.rowcount:
        db.session.rollback()
        return "already_applied"
    db.session.execute(
        insert(Wallet).values(user_id=user_id, currency=currency, balance=amount)
        .on_conflict_do_update(index_elements=["user_id", "currency"],
                               set_={"balance": Wallet.balance + amount})
    )
    db.session.commit()
    return "ok"


def parse_amount(raw):
    try:
        value = Decimal(str(raw))
        if value.is_finite() and 0 < value <= Decimal("999999999.99"):
            return value
    except InvalidOperation:
        pass
    return None


def flutterwave(method, path, **kwargs):
    try:
        response = requests.request(method, f"{FLW_API}/{path}",
                                    headers={"Authorization": f"Bearer {FLW_SECRET_KEY}"},
                                    timeout=20, **kwargs)
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError):
        app.logger.warning("Flutterwave request failed for %s", method)
        return None
    if not isinstance(body, dict) or body.get("status") != "success":
        return None
    data = body.get("data")
    return data if isinstance(data, dict) else None


@app.route("/")
def index():
    return render_template("index.html", payment_mode=MODE)


@app.route("/api/me", methods=["GET", "POST"])
def api_me():
    data = (request.args or request_data()) if request.method == "GET" else request_data()
    profile = get_profile(data)
    if profile is None:
        return jsonify(error="Provide a valid email and an optional name of up to 120 characters."), 400
    user = get_user(*profile)
    deposits = db.session.scalars(
        db.select(LedgerEntry).filter_by(user_id=user.id, kind="deposit")
        .order_by(LedgerEntry.id.desc()).limit(20)
    )
    return jsonify(email=user.email, name=user.name,
                   wallets={wallet.currency: float(wallet.balance) for wallet in user.wallets},
                   deposits=[{"ref": entry.ref, "currency": entry.currency,
                              "amount": float(entry.amount),
                              "date": entry.created_at.strftime("%Y-%m-%d %H:%M UTC")}
                             for entry in deposits])


@app.route("/api/topup/init", methods=["POST"])
def topup_init():
    data = request_data()
    profile = get_profile(data)
    raw_currency = data.get("currency", "USD")
    currency = raw_currency.upper() if isinstance(raw_currency, str) else ""
    raw_amount = parse_amount(data.get("amount"))
    amount = raw_amount.quantize(Decimal("0.01")) if raw_amount is not None else None
    if profile is None or currency not in CURRENCIES or not amount:
        return jsonify(error="A valid email, supported currency and positive amount are required."), 400

    user = get_user(*profile)
    ref = f"SWP-DEP-{uuid4().hex}"
    if MODE == "demo":
        apply_deposit(user.id, currency, amount, ref)
        return jsonify(status="ok", tx_ref=ref)

    db.session.add(PendingTopUp(ref=ref, user_id=user.id, currency=currency, amount=amount))
    db.session.commit()
    checkout = flutterwave("POST", "payments", json={
        "tx_ref": ref,
        "amount": float(amount),
        "currency": currency,
        "redirect_url": PUBLIC_BASE_URL + url_for("topup_return"),
        "customer": {"email": profile[0], "name": user.name},
    })
    link = checkout.get("link") if checkout else None
    try:
        destination = urlsplit(link) if isinstance(link, str) else None
    except ValueError:
        destination = None
    if (destination is None or destination.scheme != "https"
            or destination.netloc != "checkout.flutterwave.com"):
        return jsonify(error="Flutterwave did not return a checkout link. Please try again."), 502
    return jsonify(status="ok", tx_ref=ref, link=link)


@app.route("/api/webhook/flutterwave", methods=["POST"])
def flw_webhook():
    if MODE != "flutterwave-test":
        abort(404)
    signature = request.headers.get("verif-hash", "").encode()
    if not hmac.compare_digest(signature, FLW_SECRET_HASH.encode()):
        abort(403)

    event = request_data()
    data = event.get("data")
    if (event.get("event") != "charge.completed" or not isinstance(data, dict)
            or data.get("status") != "successful"):
        return "", 200

    ref = data.get("tx_ref")
    tx_id = str(data.get("id", ""))
    if not isinstance(ref, str) or not re.fullmatch(r"[0-9]{1,24}", tx_id):
        return "", 200
    pending = db.session.get(PendingTopUp, ref)
    if pending is None:
        return "", 200

    verified = flutterwave("GET", f"transactions/{tx_id}/verify")
    if verified is None:
        return "", 502
    if (verified.get("status") != "successful" or verified.get("tx_ref") != pending.ref
            or verified.get("currency") != pending.currency
            or parse_amount(verified.get("amount")) != pending.amount):
        app.logger.warning("Flutterwave verification mismatch")
        return "", 200
    result = apply_deposit(pending.user_id, pending.currency, pending.amount, pending.ref)
    return jsonify(status=result)


@app.route("/api/topup/return")
def topup_return():
    status = request.args.get("status")
    if status not in {"successful", "completed", "cancelled", "failed"}:
        status = "received"
    return redirect(url_for("index", status=status))


with app.app_context():
    db.create_all()
