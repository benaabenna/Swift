import os, sys, hmac, hashlib, time

# Load env vars directly (no wsgi.py dependency)
try:
    from dotenv import load_dotenv
    load_dotenv('/home/bena109/mysite/.env')
except ImportError:
    pass

from flask import Flask, request, jsonify, abort, render_template
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////home/bena109/swiftpay.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

FLW_SECRET_HASH = os.environ.get('FLW_SECRET_HASH', '')
FLW_SECRET_KEY  = os.environ.get('FLW_SECRET_KEY', '')
BASE_URL        = 'https://tinyurl.pythonanywhere.com'
# ---------- Models ----------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), default='')
    wallets = db.relationship('Wallet', backref='user', lazy=True)

class Wallet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    balance = db.Column(db.Numeric(18, 2), nullable=False, default=0)
    __table_args__ = (db.UniqueConstraint('user_id', 'currency'),)

class LedgerEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    amount = db.Column(db.Numeric(18, 2), nullable=False)
    kind = db.Column(db.String(24), nullable=False)
    ref = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=db.func.now())

# ---------- Helpers ----------
def get_user(email, name=''):
    email = email.strip().lower()
    user = User.query.filter_by(email=email).first()
    if not user:
        user = User(email=email, name=name or email.split('@')[0])
        db.session.add(user)
        db.session.commit()
    return user

def get_wallet(user, currency):
    w = Wallet.query.filter_by(user_id=user.id, currency=currency).first()
    if not w:
        w = Wallet(user_id=user.id, currency=currency, balance=0)
        db.session.add(w)
        db.session.commit()
    return w

def apply_ledger(user, currency, amount, kind, ref):
    if LedgerEntry.query.filter_by(ref=ref).first():
        return 'already_applied'
    wallet = get_wallet(user, currency)
    wallet.balance = wallet.balance + amount
    db.session.add(LedgerEntry(user_id=user.id, currency=currency,
                               amount=amount, kind=kind, ref=ref))
    db.session.commit()
    return 'ok'

# ---------- Routes ----------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/me', methods=['GET', 'POST'])
def api_me():
    data = request.get_json(force=True) or {}
    email = (data.get('email') or '').strip().lower()
    if not email or '@' not in email:
        return jsonify(error='valid email required'), 400
    user = get_user(email, data.get('name', ''))
    balances = {w.currency: float(w.balance) for w in user.wallets}
    return jsonify(email=user.email, name=user.name, wallets=balances)

@app.route('/api/topup/init', methods=['POST'])
def topup_init():
    data = request.get_json(force=True) or {}
    email = (data.get('email') or '').strip().lower()
    amount = data.get('amount')
    currency = (data.get('currency') or 'USD').upper()
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        return jsonify(error='invalid amount'), 400
    if amount <= 0 or '@' not in email:
        return jsonify(error='valid email and positive amount required'), 400

    user = get_user(email, data.get('name', ''))
    tx_ref = f'SWP-DEP-{user.id}-{int(time.time())}'

    import requests as http
    endpoint = ('https://api.flutterwave.com/v3/charges?type=mobile_money_kenya'
                if currency == 'KES'
                else 'https://api.flutterwave.com/v3/payments')
    resp = http.post(endpoint,
                     headers={'Authorization': f'Bearer {FLW_SECRET_KEY}'},
                     json={
                         'tx_ref': tx_ref,
                         'amount': amount,
                         'currency': currency,
                         'redirect_url': f'{BASE_URL}/api/topup/return',
                         'customer': {'email': email, 'name': user.name},
                     }, timeout=20)
    body = resp.json()
    if resp.status_code >= 400 or body.get('status') != 'success':
        return jsonify(error='payment init failed', detail=body.get('message')), 502
    return jsonify(status='ok', tx_ref=tx_ref, link=body['data']['link'])

@app.route('/api/webhook/flutterwave', methods=['POST'])
def flw_webhook():
    signature = request.headers.get('verif-hash', '')
    if not hmac.compare_digest(signature, FLW_SECRET_HASH):
        abort(403)

    event = request.get_json(force=True)
    if event.get('event') != 'charge.completed':
        return '', 200
    d = event.get('data', {})
    if d.get('status') != 'successful':
        return '', 200

    email = (d.get('customer', {}).get('email') or '').strip().lower()
    if not email:
        return '', 200

    import requests as http
    verify = http.get(f'https://api.flutterwave.com/v3/transactions/{d["id"]}/verify',
                      headers={'Authorization': f'Bearer {FLW_SECRET_KEY}'}, timeout=20)
    v = verify.json().get('data', {})
    if v.get('status') != 'successful' or float(v.get('amount', 0)) != float(d.get('amount', -1)):
        app.logger.warning('Verification mismatch for tx %s', d.get('id'))
        return '', 200

    user = get_user(email)
    result = apply_ledger(user, d['currency'], round(float(d['amount']), 2), 'deposit', str(d['id']))
    return jsonify(status=result), 200

@app.route('/api/topup/return', methods=['GET'])
def topup_return():
    status = request.args.get('status')
    return f'<script>window.location.href="/?status={status or "received"}";</script>'

with app.app_context():
    db.create_all()
