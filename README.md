<!-- coderabbit-ai-generated:v1 -->

# SwiftPay Global

SwiftPay is a Flask demo for a currency calculator, a multi-currency wallet, simulated transfers and simulated identity checks. Use sample names, email addresses and identity details. The email screen identifies a demo profile; it does not authenticate anyone.

## Run from a fresh checkout

Use Python 3.11 or newer, with SQLite 3.24 or newer. Open a terminal at the root of this repository:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m flask --app main run
```

On Windows, use `py -3 -m venv .venv` for the first command and `.venv\Scripts\python.exe` as the executable for the next two. If your system uses `python3`, use it for the first command.

Open <http://127.0.0.1:5000>. Enter a sample email such as `demo@example.com`, then select **Add Funds** and **Add Demo Funds**. Balances and deposit history are saved in the local SQLite database. Simulated transfers, recipients and KYC state are held in the current browser session and reset on reload. The calculator starts with built-in exchange rates and can update them from an external feed when available.

No environment file, payment credentials, Node.js or access to CSS and icon CDNs is needed to start the demo. SQLite is created automatically at `instance/swiftpay.db`. To select a different SQLite file, set `SWIFTPAY_DB_PATH` in your environment or in a local `.env`; relative paths are resolved from the project directory. Keep local env files and database files out of version control.

## Optional Flutterwave test checkout

1. Copy `.env.example` to `.env` and set `SWIFTPAY_MODE=flutterwave-test`, your Flutterwave test secret key as `FLW_SECRET_KEY`, and your test webhook hash as `FLW_SECRET_HASH`.
2. Set `PUBLIC_BASE_URL` to the public HTTPS origin serving the test app, without a path. In your Flutterwave test dashboard, configure that origin followed by `/api/webhook/flutterwave` as the webhook URL. Use [Flutterwave’s test data](https://developer.flutterwave.com/docs/testing) at checkout.
3. Restart the app. **Add Funds** now opens Flutterwave’s [hosted checkout](https://developer.flutterwave.com/docs/collections-inflow/flutterwave-standard). The balance is credited after a [webhook](https://developer.flutterwave.com/docs/webhooks) and [server-side verification](https://developer.flutterwave.com/docs/transaction-verification). Test checkout defaults to `instance/swiftpay-test.db`; keep it separate from the demo database.

This project does not support live payments, authentication, delivery of funds or identity verification. Flask’s development server is for local development.

## Tests and frontend assets

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The tests use temporary databases and synthetic Flutterwave responses. To rebuild the checked-in frontend assets after changing template classes or icons, use Node.js and npm:

```bash
npm ci
npm run build
```

Include the regenerated files in `static/` with the source change. Third-party license notices are in that directory.
