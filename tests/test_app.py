import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

# Import with an isolated file and a known mode, even when a real .env exists.
test_dir = tempfile.TemporaryDirectory()
with patch.dict(os.environ, SWIFTPAY_DB_PATH=str(Path(test_dir.name) / "test.db"),
                SWIFTPAY_MODE="demo", FLW_SECRET_KEY="", FLW_SECRET_HASH="", PUBLIC_BASE_URL=""):
    import main


def tearDownModule():
    with main.app.app_context():
        main.db.engine.dispose()
    test_dir.cleanup()


class AppTests(unittest.TestCase):
    def setUp(self):
        with main.app.app_context():
            main.db.drop_all()
            main.db.create_all()
        self.client = main.app.test_client()
        self.profile = {"email": " Demo@Example.com ", "name": "Demo User"}
        self.deposit = {**self.profile, "currency": "KES", "amount": "12.25"}

    def test_demo_topup_persists_to_the_matching_profile(self):
        profile = self.client.post("/api/me", json=self.profile).json
        self.assertEqual(profile["email"], "demo@example.com")
        self.assertEqual(profile["wallets"], {})
        with patch("main.requests.request") as upstream:
            first = self.client.post("/api/topup/init", json=self.deposit)
            second = self.client.post("/api/topup/init", json=self.deposit)
            upstream.assert_not_called()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(first.json["tx_ref"], second.json["tx_ref"])
        profile = main.app.test_client().get("/api/me?email=demo@example.com").json
        self.assertEqual(profile["wallets"], {"KES": 24.5})
        self.assertEqual(len(profile["deposits"]), 2)
        self.assertEqual(self.client.get("/api/me?email=other@example.com").json["wallets"], {})
        self.assertEqual(self.client.post("/api/webhook/flutterwave", json={}).status_code, 404)

    def test_malformed_requests_return_json_errors(self):
        for data in ([], {**self.deposit, "amount": "NaN"}, {**self.deposit, "amount": 0.001},
                     {**self.deposit, "currency": []}, {**self.deposit, "email": []}):
            with self.subTest(data=data):
                response = self.client.post("/api/topup/init", json=data)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json)

    def test_template_uses_same_origin_routes_and_assets(self):
        page = self.client.get("/", environ_overrides={"SCRIPT_NAME": "/swift"})
        self.assertEqual(page.status_code, 200)
        for path in ("/swift/api/me", "/swift/api/topup/init", "/swift/static/app.min.css",
                     "/swift/static/icons.min.js"):
            self.assertIn(path, page.get_data(as_text=True))
        for path in ("/static/app.min.css", "/static/icons.min.js"):
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)

    def test_checkout_return_is_a_local_redirect(self):
        self.assertEqual(self.client.get("/api/topup/return?status=successful").location,
                         "/?status=successful")
        self.assertEqual(self.client.get("/api/topup/return?status=unexpected").location,
                         "/?status=received")

    @patch.multiple(main, MODE="flutterwave-test", PUBLIC_BASE_URL="https://swift.example",
                    FLW_SECRET_KEY="FLWSECK_TEST-fake", FLW_SECRET_HASH="fake-hash")
    @patch("main.requests.request")
    def test_sandbox_credits_only_one_matching_verified_payment(self, upstream):
        def result(data):
            response = Mock()
            response.json.return_value = {"status": "success", "data": data}
            return response

        link = "https://checkout.flutterwave.com/v3/hosted/pay/test"
        upstream.return_value = result({"link": link})
        checkout = self.client.post("/api/topup/init", json=self.deposit)
        self.assertEqual(checkout.status_code, 200)
        self.assertEqual(checkout.json["link"], link)
        self.assertEqual(upstream.call_args.args, ("POST", "https://api.flutterwave.com/v3/payments"))
        payload = upstream.call_args.kwargs["json"]
        self.assertEqual(payload["redirect_url"], "https://swift.example/api/topup/return")
        ref = checkout.json["tx_ref"]
        event = {"event": "charge.completed", "data": {"status": "successful", "tx_ref": ref, "id": 42}}
        webhook_url = "/api/webhook/flutterwave"
        headers = {"verif-hash": "fake-hash"}
        self.assertEqual(self.client.post(webhook_url, json=event).status_code, 403)
        self.assertEqual(upstream.call_count, 1)
        verified = {"status": "successful", "tx_ref": ref, "currency": "KES", "amount": "12.25"}
        upstream.side_effect = requests.Timeout
        self.assertEqual(self.client.post(webhook_url, json=event, headers=headers).status_code, 502)
        upstream.side_effect = None
        for mismatch in ({"amount": "1"}, {"currency": "USD"}, {"tx_ref": "different"}):
            upstream.return_value = result({**verified, **mismatch})
            response = self.client.post(webhook_url, json=event, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.client.post("/api/me", json=self.profile).json["wallets"], {})

        upstream.return_value = result(verified)
        self.assertEqual(self.client.post(webhook_url, json=event, headers=headers).json["status"], "ok")
        self.assertEqual(upstream.call_args.args,
                         ("GET", "https://api.flutterwave.com/v3/transactions/42/verify"))
        duplicate = self.client.post(webhook_url, json=event, headers=headers)
        self.assertEqual(duplicate.json["status"], "already_applied")
        profile = self.client.post("/api/me", json=self.profile).json
        self.assertEqual(profile["wallets"], {"KES": 12.25})
        self.assertEqual(len(profile["deposits"]), 1)
