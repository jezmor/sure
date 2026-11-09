import json
import os
import tempfile
import unittest
from datetime import date
from decimal import Decimal as D

import monarch_to_sure as m

BALANCES = """Date,Balance,Account
2024-01-01,100.00,Checking
2024-01-02,90.00,Checking
2024-01-03,95.00,Checking
2024-01-04,95.00,Checking
2024-01-01,-50.00,Card
2024-01-02,-70.00,Card
2024-01-03,-70.00,Card
2024-01-04,-70.00,Card
2024-01-01,10.00,Old Savings
"""

TRANSACTIONS = """Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner,Reviewed,Reviewed At,Reviewed By,Pending,Id
2024-01-02,Grocer,Groceries,Checking,GROCER #12,weekly shop,-10.00,Food,Me,true,,Me,false,1
2024-01-04,Employer,Paychecks,Checking,PAYROLL,,7.00,,Me,false,,,false,2
2024-01-02,Cafe,Restaurants,Card,CAFE,,-20.00,"Food, Treats",Me,false,,,false,3
2024-01-04,Payment,Credit Card Payment,Checking,CC PMT,,-1.00,,Me,false,,,false,4
2024-01-04,Later,Groceries,Checking,LATER,,-3.00,,Me,false,,,true,5
"""


def days(*pairs):
    return {date.fromisoformat(d): D(v) for d, v in pairs}


class BalanceAnchorsTest(unittest.TestCase):
    def test_no_anchor_when_transactions_explain_the_change(self):
        anchors = m.balance_anchors(
            days(("2024-01-01", "100"), ("2024-01-02", "90")),
            days(("2024-01-02", "-10")),
        )
        self.assertEqual([(date(2024, 1, 1), D("100"))], anchors)

    def test_anchor_where_balance_drifts_from_transactions(self):
        anchors = m.balance_anchors(
            days(("2024-01-01", "100"), ("2024-01-02", "90"), ("2024-01-03", "95")),
            days(("2024-01-02", "-10")),
        )
        self.assertEqual(date(2024, 1, 3), anchors[-1][0])
        self.assertEqual(D("95"), anchors[-1][1])

    def test_opening_balance_precedes_earlier_transactions(self):
        history = days(("2024-01-05", "100"))
        flows = days(("2024-01-02", "-10"), ("2024-01-04", "5"))
        anchors = m.balance_anchors(history, flows)

        self.assertEqual((date(2024, 1, 1), D("105")), anchors[0])
        self.assertEqual(D("100"), m.replay(anchors, flows, end=max(history))[date(2024, 1, 5)])

    def test_close_after_history_zeroes_balance(self):
        anchors = m.balance_anchors(days(("2024-01-01", "10")), {}, close_after=True)
        self.assertEqual((date(2024, 1, 2), D("0")), anchors[-1])

    def test_replay_reproduces_every_history_day(self):
        history = days(("2024-01-01", "100"), ("2024-01-02", "90"), ("2024-01-03", "95"), ("2024-01-06", "80"))
        flows = days(("2024-01-02", "-10"), ("2024-01-05", "-15"))
        replayed = m.replay(m.balance_anchors(history, flows), flows, end=max(history))
        for day, value in history.items():
            self.assertEqual(value, replayed[day], day)


class ConvertTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.balances = self.write("balances.csv", BALANCES)
        self.transactions = self.write("transactions.csv", TRANSACTIONS)
        self.config_path = os.path.join(self.dir.name, "accounts.json")
        m.init_config(self.balances, self.transactions, self.config_path)
        self.config = m.load_config(self.config_path)

    def tearDown(self):
        self.dir.cleanup()

    def write(self, name, content):
        path = os.path.join(self.dir.name, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def records(self, record_type, **kwargs):
        records, _ = m.convert(self.balances, self.transactions, self.config, **kwargs)
        return {r["data"]["id"]: r["data"] for r in records if r["type"] == record_type}

    def test_init_config_guesses_types(self):
        types = {name: a["type"] for name, a in self.config["accounts"].items()}
        self.assertEqual({"Checking": "Depository", "Card": "CreditCard", "Old Savings": "Investment"}, types)

    def test_transactions_flip_sign_and_link_references(self):
        tx = self.records("Transaction")["monarch-transaction:1"]
        self.assertEqual("10.00", tx["amount"])
        self.assertEqual("Grocer", tx["name"])
        self.assertEqual("weekly shop", tx["notes"])
        self.assertEqual("monarch-account:Checking", tx["account_id"])
        self.assertEqual("monarch-category:Groceries", tx["category_id"])
        self.assertEqual("monarch-merchant:Grocer", tx["merchant_id"])
        self.assertEqual(["monarch-tag:Food"], tx["tag_ids"])
        self.assertEqual("-7.00", self.records("Transaction")["monarch-transaction:2"]["amount"])

    def test_multiple_tags_and_transfer_kinds(self):
        transactions = self.records("Transaction")
        self.assertEqual(["monarch-tag:Food", "monarch-tag:Treats"], transactions["monarch-transaction:3"]["tag_ids"])
        payment = transactions["monarch-transaction:4"]
        self.assertEqual("cc_payment", payment["kind"])
        self.assertNotIn("category_id", payment)

    def test_pending_transactions_are_optional(self):
        self.assertNotIn("monarch-transaction:5", self.records("Transaction"))
        self.assertIn("monarch-transaction:5", self.records("Transaction", include_pending=True))

    def test_liability_balances_are_positive_in_sure(self):
        valuations = self.records("Valuation")
        self.assertEqual("50.00", valuations["monarch-valuation:Card:2024-01-01"]["amount"])
        self.assertEqual("opening_anchor", valuations["monarch-valuation:Card:2024-01-01"]["kind"])
        self.assertEqual("70.00", self.records("Account")["monarch-account:Card"]["balance"])

    def test_accounts_skip_server_opening_balance(self):
        account = self.records("Account")["monarch-account:Checking"]
        self.assertEqual("Depository", account["accountable_type"])
        self.assertTrue(account["skip_opening_balance_import"])
        self.assertEqual("95.00", account["balance"])

    def test_history_that_ends_early_is_not_assumed_closed(self):
        self.assertNotIn("monarch-valuation:Old Savings:2024-01-02", self.records("Valuation"))
        self.assertEqual("10.00", self.records("Account")["monarch-account:Old Savings"]["balance"])

    def test_closed_accounts_drop_to_zero(self):
        self.config["accounts"]["Old Savings"]["closed"] = True
        valuations = self.records("Valuation")
        self.assertEqual("0.00", valuations["monarch-valuation:Old Savings:2024-01-02"]["amount"])
        self.assertEqual("0.00", self.records("Account")["monarch-account:Old Savings"]["balance"])

    def test_status_is_passed_to_sure(self):
        self.config["accounts"]["Card"]["status"] = "disabled"
        self.assertEqual("disabled", self.records("Account")["monarch-account:Card"]["status"])
        self.assertEqual("active", self.records("Account")["monarch-account:Checking"]["status"])

    def test_closed_must_be_a_boolean(self):
        self.config["accounts"]["Card"]["closed"] = "yes"
        with open(self.config_path, "w") as f:
            json.dump(self.config, f)
        with self.assertRaises(ValueError):
            m.load_config(self.config_path)

    def test_invalid_status_is_rejected(self):
        self.config["accounts"]["Card"]["status"] = "closed"
        with open(self.config_path, "w") as f:
            json.dump(self.config, f)
        with self.assertRaises(ValueError):
            m.load_config(self.config_path)

    def test_excluded_accounts_are_left_out(self):
        self.config["accounts"]["Card"]["include"] = False
        self.assertNotIn("monarch-account:Card", self.records("Account"))
        self.assertNotIn("monarch-transaction:3", self.records("Transaction"))

    def test_existing_categories_keep_their_look(self):
        existing = {"categories": {"Groceries": {"color": "#123456", "lucide_icon": "carrot"}}, "merchants": set()}
        categories = self.records("Category", existing=existing)
        self.assertEqual("#123456", categories["monarch-category:Groceries"]["color"])
        self.assertEqual("carrot", categories["monarch-category:Groceries"]["lucide_icon"])
        self.assertNotIn("color", categories["monarch-category:Restaurants"])

    def test_existing_merchants_are_not_sent(self):
        existing = {"categories": {}, "merchants": {"Grocer"}}
        self.assertNotIn("monarch-merchant:Grocer", self.records("Merchant", existing=existing))
        tx = self.records("Transaction", existing=existing)["monarch-transaction:1"]
        self.assertNotIn("merchant_id", tx)
        self.assertEqual("Grocer", tx["name"])

    def test_records_are_in_dependency_order(self):
        records, _ = m.convert(self.balances, self.transactions, self.config)
        order = [m.RECORD_ORDER.index(r["type"]) for r in records]
        self.assertEqual(sorted(order), order)


class AccountReviewTest(unittest.TestCase):
    def test_last_activity_ignores_unchanged_balances(self):
        history = days(("2024-01-01", "5"), ("2024-01-02", "6"), ("2024-01-03", "6"), ("2024-06-01", "6"))
        self.assertEqual(date(2024, 1, 2), m.last_activity(history, set()))
        self.assertEqual(date(2024, 2, 1), m.last_activity(history, {date(2024, 2, 1)}))

    def test_stale_accounts_are_only_suggested_closed(self):
        end = date(2024, 12, 31)
        old = m.guess_account("Old", days(("2024-01-01", "5")), set(), end)
        self.assertTrue(old["_suggest_closed"])
        self.assertEqual((False, "active"), (old["closed"], old["status"]))
        self.assertFalse(m.guess_account("New", days(("2024-12-01", "5")), set(), end)["_suggest_closed"])

    def test_selection_parsing(self):
        self.assertEqual({1, 4, 5, 6}, m.parse_selection("1, 4-6", 6))
        self.assertEqual("1,4-6", m.format_selection({1, 4, 5, 6}))
        with self.assertRaises(ValueError):
            m.parse_selection("7", 6)

    def accounts(self):
        end = date(2024, 12, 31)
        return {
            "Recent": m.guess_account("Recent", days(("2024-12-30", "5")), set(), end),
            "Old": m.guess_account("Old", days(("2023-01-01", "5")), set(), end),
        }, end

    def review(self, *answers):
        accounts, end = self.accounts()
        replies = iter(answers)
        m.review_accounts(accounts, end, ask=lambda _: next(replies), out=lambda *_: None)
        return accounts

    def test_enter_accepts_suggestions(self):
        accounts = self.review("", "")
        recent, old = accounts["Recent"], accounts["Old"]
        self.assertEqual((False, "active", True), (recent["closed"], recent["status"], recent["include"]))
        self.assertEqual((True, "disabled", True), (old["closed"], old["status"], old["include"]))

    def test_answers_override_suggestions(self):
        # Accounts are listed newest first: 1 is Recent, 2 is Old.
        accounts = self.review("1", "2")
        self.assertTrue(accounts["Recent"]["closed"])
        self.assertFalse(accounts["Old"]["closed"])
        self.assertFalse(accounts["Old"]["include"])

    def test_invalid_answer_asks_again(self):
        accounts = self.review("9", "none", "")
        self.assertFalse(accounts["Old"]["closed"])
        self.assertEqual("active", accounts["Old"]["status"])


class ChunkTest(unittest.TestCase):
    def test_chunks_respect_size_and_keep_every_line(self):
        lines = [json.dumps({"type": "Tag", "data": {"id": str(i)}}) + "\n" for i in range(50)]
        chunks = list(m.chunk_lines(lines, max_bytes=200))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c.encode()) <= 200 for c in chunks))
        self.assertEqual("".join(lines), "".join(chunks))


class FakeClient:
    def __init__(self, existing_names=(), status="pending", chunks_count=0, categories=None, merchants=()):
        self.existing_names = set(existing_names)
        self.status = status
        self.chunks_count = chunks_count
        self.existing = {"categories": categories or {}, "merchants": set(merchants)}
        self.calls = []

    def account_names(self):
        return self.existing_names

    def existing_records(self):
        return self.existing

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST" and path == "/api/v1/import_sessions":
            return {"data": {"id": "s1", "status": self.status, "chunks_count": self.chunks_count}}
        if method == "GET":
            return {"data": {"id": "s1", "status": "complete", "summary": {}}}
        return {"data": {}}


class UploadTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "import.ndjson")
        m.write_ndjson([
            m.record("Account", {"id": "monarch-account:Checking", "name": "Checking"}),
            m.record("Category", {"id": "monarch-category:Groceries", "name": "Groceries"}),
            m.record("Merchant", {"id": "monarch-merchant:Grocer", "name": "Grocer"}),
        ], self.path)

    def tearDown(self):
        self.dir.cleanup()

    def test_uploads_chunks_and_publishes(self):
        client = FakeClient()
        session = m.upload(self.path, client, poll_seconds=0)

        self.assertEqual("complete", session["status"])
        create = client.calls[0][2]
        self.assertEqual("SureImport", create["type"])
        self.assertTrue(create["client_session_id"].startswith("monarch-to-sure:"))
        paths = [path for _, path, _ in client.calls]
        self.assertIn("/api/v1/import_sessions/s1/chunks", paths)
        self.assertIn("/api/v1/import_sessions/s1/publish", paths)

    def test_same_file_reuses_session_id(self):
        first, second = FakeClient(), FakeClient()
        m.upload(self.path, first, poll_seconds=0)
        m.upload(self.path, second, poll_seconds=0)
        self.assertEqual(first.calls[0][2]["client_session_id"], second.calls[0][2]["client_session_id"])

    def test_completed_session_is_not_uploaded_again(self):
        client = FakeClient(status="complete")
        m.upload(self.path, client, poll_seconds=0)
        self.assertEqual(1, len(client.calls))

    def test_refuses_when_account_names_already_exist(self):
        with self.assertRaises(RuntimeError):
            m.upload(self.path, FakeClient(existing_names={"Checking"}), poll_seconds=0)
        m.upload(self.path, FakeClient(existing_names={"Checking"}), allow_existing_accounts=True, poll_seconds=0)

    def test_refuses_to_rewrite_existing_categories_or_merchants(self):
        categories = {"Groceries": {"color": "#123456", "lucide_icon": "carrot"}}
        for client in (FakeClient(categories=categories), FakeClient(merchants={"Grocer"})):
            with self.assertRaises(RuntimeError):
                m.upload(self.path, client, poll_seconds=0)
            self.assertFalse(any("/chunks" in path for _, path, _ in client.calls))

    def test_resumed_session_skips_existing_data_checks(self):
        client = FakeClient(existing_names={"Checking"}, merchants={"Grocer"}, chunks_count=1)
        self.assertEqual("complete", m.upload(self.path, client, poll_seconds=0)["status"])


if __name__ == "__main__":
    unittest.main()
