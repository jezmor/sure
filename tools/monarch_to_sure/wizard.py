"""Browser wizard for monarch_to_sure.

Serves a single page on 127.0.0.1 that walks through connecting to Sure,
choosing the Monarch exports, reviewing each account, previewing the import
and uploading it. It runs the same convert and upload code as the CLI. The
exports are held in memory and never written to disk.
"""
import csv
import http.server
import io
import json
import os
import secrets
import threading
import urllib.error
import webbrowser
from datetime import date
from urllib.parse import quote, urlsplit

import monarch_to_sure as m

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wizard.html")
MAX_BODY_BYTES = 200 * 1024 * 1024

BALANCE_COLUMNS = {"Date", "Balance", "Account"}
TRANSACTION_COLUMNS = {"Date", "Merchant", "Category", "Account", "Amount", "Id"}


def csv_stream(text, required, label):
    stream = io.StringIO(text.lstrip("﻿"), newline="")
    header = set(next(csv.reader(stream), []))
    missing = sorted(required - header)
    if missing:
        raise ValueError(f"the {label} file is missing the columns {', '.join(missing)}. Is it the right export?")
    return stream


class Wizard:
    """The wizard's state and steps, kept apart from HTTP so they can be tested."""

    def __init__(self, client_factory=m.SureClient, poll_seconds=5):
        self.client_factory = client_factory
        self.poll_seconds = poll_seconds
        self.client = None
        self.balances = self.transactions = None
        self.accounts = None
        self.records = None
        self.job = None
        self.lock = threading.Lock()

    def connect(self, url, api_key):
        url, api_key = (url or "").strip().rstrip("/"), (api_key or "").strip()
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc:
            raise ValueError("enter the Sure address, such as https://sure.example.com")
        if not api_key:
            raise ValueError("enter an API key")
        client = self.client_factory(url, api_key)
        try:
            existing = client.account_names()
        except urllib.error.URLError as e:
            raise ValueError(f"couldn't reach {url}: {e.reason}") from None
        except RuntimeError as e:
            if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                raise ValueError("Sure rejected the API key. Check it has read/write access.") from None
            raise
        self.client = client
        return {"url": url, "existing_accounts": len(existing)}

    def load_files(self, balances_text, transactions_text=None):
        if not balances_text:
            raise ValueError("choose the balances export")
        balances = csv_stream(balances_text, BALANCE_COLUMNS, "balances")
        transactions = (
            csv_stream(transactions_text, TRANSACTION_COLUMNS, "transactions") if transactions_text else None
        )
        accounts, export_end = m.inspect_accounts(balances, transactions)
        self.balances, self.transactions, self.accounts = balances, transactions, accounts
        self.records = None

        names = sorted(accounts, key=lambda n: accounts[n]["_last_activity"] or "", reverse=True)
        return {
            "export_end": export_end.isoformat(),
            "balances_only": transactions is None,
            "stale_after_days": m.STALE_AFTER_DAYS,
            "today": date.today().isoformat(),
            "accounts": [
                {
                    "name": name,
                    "type": accounts[name]["type"],
                    "suggest_closed": accounts[name]["_suggest_closed"],
                    "last_activity": accounts[name]["_last_activity"],
                    "last_activity_ago": (
                        m.ago(date.fromisoformat(accounts[name]["_last_activity"]), export_end)
                        if accounts[name]["_last_activity"] else None
                    ),
                    "latest_balance": accounts[name]["_latest_balance"],
                    "transactions": accounts[name]["_transactions"],
                    "info": accounts[name]["_info"],
                }
                for name in names
            ],
        }

    def config_from(self, choices):
        if self.accounts is None:
            raise ValueError("choose the exports first")
        accounts = {}
        for name, suggestion in self.accounts.items():
            choice = choices.get(name)
            if choice is None:
                raise ValueError(f"no choice was made for {name!r}")
            closed = bool(choice.get("closed"))
            accounts[name] = {
                **suggestion,
                "type": choice.get("type"),
                "closed": closed,
                "status": "disabled" if closed else "active",
                "include": bool(choice.get("include", True)),
                "transactions": bool(choice.get("transactions", True)),
                "current_balance": (choice.get("balance") or "").strip() or None,
            }
        if not any(a["include"] for a in accounts.values()):
            raise ValueError("choose at least one account to import")
        return m.validate_config({"accounts": accounts})

    def preview(self, choices, include_pending=False):
        if self.client is None:
            raise ValueError("connect to Sure first")
        config = self.config_from(choices)
        records, stats = m.convert(
            self.balances, self.transactions, config, include_pending, self.client.existing_records()
        )
        self.records = records

        valuations = {}
        for r in records:
            if r["type"] == "Valuation":
                valuations[r["data"]["account_id"]] = valuations.get(r["data"]["account_id"], 0) + 1
        accounts = [r["data"] for r in records if r["type"] == "Account"]
        return {
            "stats": stats,
            "skipped_accounts": sorted(n for n, a in config["accounts"].items() if not a["include"]),
            "existing_account_names": sorted({a["name"] for a in accounts} & self.client.account_names()),
            "accounts": [
                {
                    "name": a["name"],
                    "type": a["accountable_type"],
                    "status": a["status"],
                    "balance": a["balance"],
                    "valuations": valuations.get(a["id"], 0),
                }
                for a in accounts
            ],
        }

    def start_import(self, allow_existing_accounts=False):
        with self.lock:
            if self.records is None:
                raise ValueError("review the import first")
            if self.job and self.job["state"] == "running":
                raise ValueError("an import is already running")
            job = {"state": "running", "log": [], "session": None, "error": None}
            self.job = job

        lines = m.ndjson_lines(self.records)

        def work():
            try:
                session = m.upload_lines(
                    lines, self.client, allow_existing_accounts, self.poll_seconds, log=job["log"].append
                )
                job["session"] = session
                job["state"] = "complete" if session["status"] == "complete" else "failed"
            except Exception as e:  # reported to the page; the server keeps running
                job["error"] = str(e)
                job["state"] = "failed"

        threading.Thread(target=work, daemon=True).start()
        return self.status()

    def status(self):
        if self.job is None:
            return {"state": "idle"}
        job = dict(self.job)
        job["log"] = list(job["log"])
        return job


def make_handler(wizard, token):
    routes = {
        "/api/connect": lambda b: wizard.connect(b.get("url"), b.get("api_key")),
        "/api/files": lambda b: wizard.load_files(b.get("balances"), b.get("transactions")),
        "/api/preview": lambda b: wizard.preview(b.get("choices") or {}, bool(b.get("include_pending"))),
        "/api/import": lambda b: wizard.start_import(bool(b.get("allow_existing_accounts"))),
        "/api/status": lambda b: wizard.status(),
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def host_allowed(self):
            # Rejects DNS-rebinding requests that reach this port under another name.
            port = self.server.server_address[1]
            return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def send(self, status, body, content_type="application/json"):
            if not isinstance(body, bytes):
                body = json.dumps(body, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.host_allowed():
                return self.send(403, {"error": "forbidden"})
            if urlsplit(self.path).path == "/":
                with open(HTML_PATH, "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            self.send(404, {"error": "not found"})

        def do_POST(self):
            # The token comes from the link the wizard opened, and is sent as a
            # header so that other web pages can't drive the wizard.
            if not self.host_allowed() or not secrets.compare_digest(
                self.headers.get("X-Wizard-Token", ""), token
            ):
                return self.send(403, {"error": "forbidden"})
            route = routes.get(urlsplit(self.path).path)
            if route is None:
                return self.send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                return self.send(413, {"error": "the files are too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                self.send(200, route(body))
            except (ValueError, KeyError, RuntimeError, OSError) as e:
                self.send(400, {"error": str(e)})

    return Handler


def run(port=8765, open_browser=True, url=None, api_key=None):
    wizard = Wizard()
    token = secrets.token_urlsafe(24)
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(wizard, token))
    except OSError:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(wizard, token))
    link = f"http://127.0.0.1:{server.server_address[1]}/#{token}"
    if url:
        link += f"&url={quote(url, safe='')}"
    if url and api_key:
        try:
            wizard.connect(url, api_key)
            link += "&connected=1"
        except (ValueError, RuntimeError) as e:
            print(f"Couldn't connect with SURE_URL and SURE_API_KEY: {e}")

    print(f"Monarch to Sure wizard: {link}", flush=True)
    print("Keep this window open while you use it. Press Ctrl+C to stop.", flush=True)
    if open_browser:
        webbrowser.open(link)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
