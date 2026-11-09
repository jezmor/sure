import http.client
import json
import threading
import time
import unittest

import monarch_to_sure as m
import wizard as w
from test_monarch_to_sure import BALANCES, TRANSACTIONS, FakeClient


class WizardClient(FakeClient):
    def __init__(self, url="http://sure.test", api_key="key", **kwargs):
        super().__init__(**kwargs)
        self.url, self.api_key = url, api_key

    def account_names(self):
        if self.api_key == "bad":
            raise RuntimeError("GET /api/v1/accounts failed with HTTP 401: {}")
        return super().account_names()


def choices_from(files, **overrides):
    choices = {a["name"]: {"type": a["type"], "closed": a["suggest_closed"], "include": True} for a in files["accounts"]}
    for name, change in overrides.items():
        choices[name].update(change)
    return choices


class WizardTest(unittest.TestCase):
    def setUp(self):
        self.client_kwargs = {}
        self.wizard = w.Wizard(client_factory=self.make_client, poll_seconds=0)

    def make_client(self, url, api_key):
        self.client = WizardClient(url, api_key, **self.client_kwargs)
        return self.client

    def connected(self, **client_kwargs):
        self.client_kwargs = client_kwargs
        self.wizard.connect("http://sure.test/", "key")
        return self.wizard.load_files(BALANCES, TRANSACTIONS)

    def wait_for_import(self):
        for _ in range(200):
            status = self.wizard.status()
            if status["state"] != "running":
                return status
            time.sleep(0.01)
        self.fail("import didn't finish")

    def test_connect_checks_url_and_key(self):
        with self.assertRaisesRegex(ValueError, "Sure address"):
            self.wizard.connect("sure.test", "key")
        with self.assertRaisesRegex(ValueError, "rejected the API key"):
            self.wizard.connect("http://sure.test", "bad")
        self.assertEqual("http://sure.test", self.wizard.connect("http://sure.test/", "key")["url"])

    def test_files_list_accounts_with_suggestions(self):
        files = self.connected()
        names = [a["name"] for a in files["accounts"]]
        self.assertEqual({"Checking", "Card", "Old Savings"}, set(names))
        self.assertEqual("Old Savings", names[-1])  # least recently active last
        self.assertFalse(files["balances_only"])
        by_name = {a["name"]: a for a in files["accounts"]}
        self.assertEqual("CreditCard", by_name["Card"]["type"])
        self.assertEqual(4, by_name["Checking"]["transactions"])  # rows, including pending, not days

    def test_files_without_transactions_are_balances_only(self):
        self.assertTrue(self.wizard.load_files(BALANCES)["balances_only"])

    def test_wrong_file_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "balances file is missing"):
            self.wizard.load_files(TRANSACTIONS)
        with self.assertRaisesRegex(ValueError, "transactions file is missing"):
            self.wizard.load_files(BALANCES, BALANCES)

    def test_choices_set_type_closure_and_skips(self):
        files = self.connected()
        config = self.wizard.config_from(choices_from(
            files, Card={"type": "Loan", "closed": True}, **{"Old Savings": {"include": False}}
        ))
        card = config["accounts"]["Card"]
        self.assertEqual(("Loan", True, "disabled"), (card["type"], card["closed"], card["status"]))
        self.assertFalse(config["accounts"]["Old Savings"]["include"])

    def test_choices_set_transactions_and_balance(self):
        files = self.connected()
        config = self.wizard.config_from(choices_from(
            files, Checking={"transactions": False, "balance": " 120 "}, Card={"balance": ""}
        ))
        self.assertEqual((False, "120"), (config["accounts"]["Checking"]["transactions"], config["accounts"]["Checking"]["current_balance"]))
        self.assertEqual((True, None), (config["accounts"]["Card"]["transactions"], config["accounts"]["Card"]["current_balance"]))
        with self.assertRaisesRegex(ValueError, "Checking"):
            self.wizard.config_from(choices_from(files, Checking={"balance": "lots"}))

    def test_invalid_type_is_rejected(self):
        files = self.connected()
        with self.assertRaises(ValueError):
            self.wizard.config_from(choices_from(files, Card={"type": "Boat"}))

    def test_preview_counts_records_and_flags_existing_accounts(self):
        files = self.connected(existing_names={"Checking"})
        preview = self.wizard.preview(choices_from(files, **{"Old Savings": {"include": False}}))
        self.assertEqual(2, preview["stats"]["Account"])
        self.assertEqual(4, preview["stats"]["Transaction"])  # the pending one is left out
        self.assertEqual(["Checking"], preview["existing_account_names"])
        self.assertEqual(["Old Savings"], preview["skipped_accounts"])

    def test_import_uploads_the_previewed_records(self):
        files = self.connected()
        self.wizard.preview(choices_from(files))
        self.wizard.start_import()
        status = self.wait_for_import()
        self.assertEqual("complete", status["state"])
        chunk = next(body for method, path, body in self.client.calls if path.endswith("/chunks"))
        self.assertEqual(m.ndjson_lines(self.wizard.records), chunk["raw_file_content"].splitlines(keepends=True))

    def test_import_reports_refusals(self):
        files = self.connected(existing_names={"Checking"})
        self.wizard.preview(choices_from(files))
        self.wizard.start_import()
        status = self.wait_for_import()
        self.assertEqual("failed", status["state"])
        self.assertIn("Checking", status["error"])

    def test_import_needs_a_preview(self):
        with self.assertRaises(ValueError):
            self.wizard.start_import()


class WizardServerTest(unittest.TestCase):
    def setUp(self):
        self.server = w.http.server.ThreadingHTTPServer(("127.0.0.1", 0), w.make_handler(w.Wizard(), "secret"))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data

    def test_serves_the_page(self):
        status, body = self.request("GET", "/")
        self.assertEqual(200, status)
        self.assertIn(b"Monarch to Sure", body)

    def test_api_needs_the_token(self):
        self.assertEqual(403, self.request("POST", "/api/status")[0])
        status, body = self.request("POST", "/api/status", {"X-Wizard-Token": "secret"})
        self.assertEqual((200, {"state": "idle"}), (status, json.loads(body)))

    def test_other_hosts_are_refused(self):
        headers = {"Host": "evil.test", "X-Wizard-Token": "secret"}
        self.assertEqual(403, self.request("GET", "/", headers)[0])
        self.assertEqual(403, self.request("POST", "/api/status", headers)[0])

    def test_errors_come_back_as_messages(self):
        status, body = self.request("POST", "/api/import", {"X-Wizard-Token": "secret"}, b"{}")
        self.assertEqual(400, status)
        self.assertIn("review the import first", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
