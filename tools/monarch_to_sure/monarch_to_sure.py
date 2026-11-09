#!/usr/bin/env python3
"""Move accounts, transactions and balance history from Monarch to Sure.

Reads Monarch's "Transactions" and "Balances" CSV exports, converts them into
Sure's NDJSON import format, and uploads that through the Sure API as one
chunked import session.

  1. init-config  inspect the exports and write an editable account config
  2. convert      build the Sure NDJSON file (review it before uploading)
  3. upload       send the NDJSON file to a Sure server

Only the Python standard library is used.
"""
import argparse
import csv
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

ASSET_TYPES = {"Depository", "Investment", "Crypto", "Property", "Vehicle", "OtherAsset"}
LIABILITY_TYPES = {"CreditCard", "Loan", "OtherLiability"}
ACCOUNT_TYPES = ASSET_TYPES | LIABILITY_TYPES

# Monarch categories that describe money moving between accounts. Sure models
# these as transaction kinds rather than categories, so they stay out of budgets.
TRANSFER_CATEGORY_KINDS = {
    "Transfer": "funds_movement",
    "Credit Card Payment": "cc_payment",
}

# Records are written in dependency order so that every chunk only references
# records from the same or an earlier chunk.
RECORD_ORDER = ["Account", "Category", "Tag", "Merchant", "Transaction", "Valuation"]

DEFAULT_CHUNK_BYTES = 4 * 1024 * 1024  # Sure accepts up to 10 MB per chunk by default


# ---------------------------------------------------------------------------
# Reading Monarch exports
# ---------------------------------------------------------------------------

def parse_decimal(value):
    raw = (value or "").replace(",", "").replace("$", "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"not a number: {value!r}")


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def read_balances(path):
    """Return {account: {date: Decimal}}, skipping blank balances."""
    balances = defaultdict(dict)
    if not path:
        return balances
    for row in read_csv(path):
        amount = parse_decimal(row["Balance"])
        if amount is not None:
            balances[row["Account"]][date.fromisoformat(row["Date"])] = amount
    return balances


def read_transactions(path, include_pending=False):
    rows = read_csv(path) if path else []
    if not include_pending:
        rows = [r for r in rows if r.get("Pending", "").strip().lower() != "true"]
    return rows


def split_tags(value):
    return [t.strip() for t in (value or "").split(",") if t.strip()]


# ---------------------------------------------------------------------------
# Account config
# ---------------------------------------------------------------------------

ACCOUNT_STATUSES = ("active", "disabled")
STALE_AFTER_DAYS = 180


def last_activity(history, tx_dates):
    """The last day an account's money moved: its latest transaction, or the
    latest day its balance changed. Monarch keeps reporting a balance for
    accounts long after they stop changing, so the last balance date alone
    says little."""
    days = sorted(history)
    changes = [d for prev, d in zip(days, days[1:]) if history[d] != history[prev]]
    candidates = changes[-1:] + days[:1] + list(tx_dates)
    return max(candidates) if candidates else None


def guess_account(name, history, tx_dates, export_end):
    values = [history[d] for d in sorted(history)]
    nonzero = [v for v in values if v != 0]
    negative_share = sum(1 for v in nonzero if v < 0) / len(nonzero) if nonzero else 0

    if negative_share > 0.5:
        account_type = "CreditCard" if tx_dates else "Loan"
    elif tx_dates:
        account_type = "Depository"
    else:
        account_type = "Investment"

    active_on = last_activity(history, tx_dates)
    stale = active_on is not None and (export_end - active_on).days >= STALE_AFTER_DAYS

    return {
        "type": account_type,
        "status": "disabled" if stale else "active",
        "subtype": None,
        "include": True,
        "currency": None,
        "close_after_history": None,
        "_last_activity": active_on.isoformat() if active_on else None,
        "_latest_balance": str(values[-1]) if values else None,
        "_transactions": len(tx_dates),
        "_info": (
            f"{len(values)} balance days"
            + (f" ({min(history)} to {max(history)})" if history else "")
        ),
    }


def parse_selection(text, count):
    """Parse "1, 4-6" into {1, 4, 5, 6}, rejecting numbers outside 1..count."""
    selected = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        low, _, high = part.partition("-")
        low, high = int(low), int(high or low)
        if not 1 <= low <= high <= count:
            raise ValueError(f"{part} is not between 1 and {count}")
        selected.update(range(low, high + 1))
    return selected


def format_selection(numbers):
    numbers, ranges = sorted(numbers), []
    for n in numbers:
        if ranges and n == ranges[-1][1] + 1:
            ranges[-1][1] = n
        else:
            ranges.append([n, n])
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in ranges)


def ago(day, today):
    days = (today - day).days
    if days < 60:
        return f"{days}d ago"
    if days < 730:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


def review_accounts(accounts, export_end, ask=input, out=print):
    """Ask which accounts to import as disabled and which to skip.

    Disabled accounts are hidden from current balances but their history still
    counts toward net worth up to the import date, which suits accounts kept
    only for the record.
    """
    names = sorted(accounts, key=lambda n: accounts[n]["_last_activity"] or "", reverse=True)
    width = max(len(n) for n in names)
    out(f"\n  #  {'Account':<{width}}  {'Type':<14}  {'Last activity':<22}  {'Latest balance':>14}  Txns")
    for i, name in enumerate(names, start=1):
        a = accounts[name]
        when = a["_last_activity"]
        when = f"{when} ({ago(date.fromisoformat(when), export_end)})" if when else "never"
        out(f"{i:>3}  {name:<{width}}  {a['type']:<14}  {when:<22}  {a['_latest_balance'] or '':>14}  {a['_transactions']}")

    def choose(prompt, default):
        while True:
            answer = ask(f"{prompt} [{format_selection(default) or 'none'}]: ").strip()
            if not answer:
                return default
            if answer.lower() == "none":
                return set()
            try:
                return parse_selection(answer, len(names))
            except ValueError as e:
                out(f"  {e}. Use numbers and ranges like 1,4-6, or none.")

    out(f"\nDisabled accounts keep their history in net worth but are hidden from current balances.")
    out(f"Suggested: accounts with no activity in the last {STALE_AFTER_DAYS} days.")
    suggested = {i for i, n in enumerate(names, start=1) if accounts[n]["status"] == "disabled"}
    disabled = choose("Accounts to import as disabled", suggested)
    skipped = choose("Accounts to skip entirely", set())

    for i, name in enumerate(names, start=1):
        accounts[name]["status"] = "disabled" if i in disabled else "active"
        accounts[name]["include"] = i not in skipped


def init_config(balances_path, transactions_path, output_path, interactive=False, ask=input, out=print):
    balances = read_balances(balances_path)
    tx_dates = defaultdict(set)
    for row in read_transactions(transactions_path, include_pending=True):
        tx_dates[row["Account"]].add(date.fromisoformat(row["Date"]))

    export_end = max([d for h in balances.values() for d in h] + [d for s in tx_dates.values() for d in s])
    names = sorted(set(balances) | set(tx_dates))
    accounts = {
        name: guess_account(name, balances.get(name, {}), tx_dates[name], export_end) for name in names
    }
    if interactive:
        review_accounts(accounts, export_end, ask=ask, out=out)

    config = {
        "_help": (
            "Review each account before converting. type is one of "
            + ", ".join(sorted(ACCOUNT_TYPES))
            + ". status is active or disabled; disabled accounts are hidden from current "
            "balances but still count toward net worth history. Set include to false to skip "
            "an account. currency defaults to the Sure family currency. close_after_history adds "
            "a zero balance the day after the last Monarch balance; null means do it only when "
            "the history ends before the export does."
        ),
        "accounts": accounts,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, default=str)
        f.write("\n")
    return config


def load_config(path):
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    for name, account in config["accounts"].items():
        if not account.get("include", True):
            continue
        if account.get("type") not in ACCOUNT_TYPES:
            raise ValueError(f"account {name!r} has invalid type {account.get('type')!r}")
        if account.get("status", "active") not in ACCOUNT_STATUSES:
            raise ValueError(f"account {name!r} has invalid status {account.get('status')!r}")
    return config


# ---------------------------------------------------------------------------
# Balance history
# ---------------------------------------------------------------------------

def balance_anchors(history, flows, close_after=None):
    """Return the valuations Sure needs to reproduce a Monarch balance history.

    Both inputs use Monarch signs: history maps date -> balance, flows maps
    date -> net transaction amount (inflows positive). Sure carries a balance
    forward by applying transactions and resets it on any valuation, so an
    anchor is only emitted where Monarch's balance differs from that running
    total. Returns [(date, balance)] with the opening anchor first.
    """
    if not history and not flows:
        return []

    first_balance_date = min(history) if history else None
    first_flow_date = min(flows) if flows else None

    anchors = []
    running = None

    if first_flow_date and (first_balance_date is None or first_flow_date < first_balance_date):
        # Transactions predate the balance history: work backwards from the
        # first known balance so earlier transactions land on the right total.
        opening_date = first_flow_date - timedelta(days=1)
        if first_balance_date:
            opening = history[first_balance_date] - sum(
                (v for d, v in flows.items() if d <= first_balance_date), Decimal(0)
            )
        else:
            opening = Decimal(0)
        anchors.append((opening_date, opening))
        running = opening

    for day in sorted(set(history) | set(flows)):
        expected = None if running is None else running + flows.get(day, Decimal(0))
        actual = history.get(day)
        if actual is not None and actual != expected:
            anchors.append((day, actual))
            running = actual
        else:
            running = expected

    if close_after and history:
        last_day = max(history)
        if history[last_day] != 0 and not any(d > last_day for d in flows):
            anchors.append((last_day + timedelta(days=1), Decimal(0)))

    return anchors


def replay(anchors, flows, end=None):
    """Return {date: balance} the way Sure's forward calculator derives it.

    A valuation sets the end-of-day balance outright; any other day adds that
    day's transactions to the previous balance. Used to report the current
    balance and to check that anchors reproduce the Monarch history.
    """
    if not anchors:
        return {}
    valuations = dict(anchors)
    start = anchors[0][0]
    end = max([start, *valuations, *flows, *([end] if end else [])])
    balances, running, day = {}, Decimal(0), start
    while day <= end:
        running = valuations[day] if day in valuations else running + flows.get(day, Decimal(0))
        balances[day] = running
        day += timedelta(days=1)
    return balances


# ---------------------------------------------------------------------------
# Conversion to Sure NDJSON
# ---------------------------------------------------------------------------

def record(record_type, data):
    return {"type": record_type, "data": data}


def account_source_id(name):
    return f"monarch-account:{name}"


def money(value):
    return str(value.quantize(Decimal("0.01")) + 0)  # + 0 turns -0.00 into 0.00


def convert(balances_path, transactions_path, config, include_pending=False, existing=None):
    """Return (records, stats) where records is a list of NDJSON envelopes.

    existing comes from SureClient.existing_records. Same-named categories keep
    their current color and icon. Same-named merchants are left out entirely,
    because the API doesn't expose the logo and color the importer would
    replace, so those transactions keep the merchant name but no merchant link.
    """
    existing = existing or {"categories": {}, "merchants": set()}
    balances = read_balances(balances_path)
    transactions = read_transactions(transactions_path, include_pending=include_pending)
    accounts = {
        name: cfg for name, cfg in config["accounts"].items() if cfg.get("include", True)
    }

    unknown = sorted({r["Account"] for r in transactions} - set(config["accounts"]))
    if unknown:
        raise ValueError(f"accounts missing from config (re-run init-config): {', '.join(unknown)}")

    export_end = max(
        [d for h in balances.values() for d in h]
        + [date.fromisoformat(r["Date"]) for r in transactions]
        or [date.today()]
    )

    records = {t: [] for t in RECORD_ORDER}
    categories, tags, merchants = {}, {}, {}
    flows = defaultdict(lambda: defaultdict(Decimal))
    stats = defaultdict(int)

    for row in transactions:
        name = row["Account"]
        if name not in accounts:
            stats["transactions_skipped_excluded_account"] += 1
            continue

        amount = parse_decimal(row["Amount"])
        if amount is None:
            stats["transactions_skipped_no_amount"] += 1
            continue
        day = date.fromisoformat(row["Date"])
        flows[name][day] += amount

        category = row.get("Category", "").strip()
        kind = TRANSFER_CATEGORY_KINDS.get(category, "standard")
        data = {
            "id": f"monarch-transaction:{row['Id']}",
            "account_id": account_source_id(name),
            "date": day.isoformat(),
            # Sure records outflows as positive amounts; Monarch as negative.
            "amount": money(-amount),
            "name": row.get("Merchant", "").strip() or row.get("Original Statement", "").strip() or "Imported transaction",
            "notes": row.get("Notes", "").strip() or None,
            "kind": kind,
        }
        if accounts[name].get("currency"):
            data["currency"] = accounts[name]["currency"]

        if category and kind == "standard":
            categories.setdefault(category, f"monarch-category:{category}")
            data["category_id"] = categories[category]

        merchant = row.get("Merchant", "").strip()
        if merchant in existing["merchants"]:
            stats["transactions_unlinked_from_existing_merchant"] += 1
        elif merchant:
            merchants.setdefault(merchant, f"monarch-merchant:{merchant}")
            data["merchant_id"] = merchants[merchant]

        tag_ids = []
        for tag in split_tags(row.get("Tags")):
            tags.setdefault(tag, f"monarch-tag:{tag}")
            tag_ids.append(tags[tag])
        if tag_ids:
            data["tag_ids"] = tag_ids

        records["Transaction"].append(record("Transaction", data))

    for name, cfg in accounts.items():
        liability = cfg["type"] in LIABILITY_TYPES
        sign = Decimal(-1) if liability else Decimal(1)  # Sure stores amounts owed as positive
        history = balances.get(name, {})

        close_after = cfg.get("close_after_history")
        if close_after is None:
            close_after = bool(history) and max(history) < export_end

        account_flows = flows.get(name, {})
        anchors = balance_anchors(history, account_flows, close_after=close_after)
        replayed = replay(anchors, account_flows)
        current = replayed[max(replayed)] if replayed else Decimal(0)

        account = {
            "id": account_source_id(name),
            "name": name,
            "accountable_type": cfg["type"],
            "accountable": {"subtype": cfg["subtype"]} if cfg.get("subtype") else {},
            "subtype": cfg.get("subtype"),
            "balance": money(current * sign),
            "status": cfg.get("status", "active"),
            # Opening balance and history come from the valuations below.
            "skip_opening_balance_import": True,
        }
        if cfg.get("currency"):
            account["currency"] = cfg["currency"]
        records["Account"].append(record("Account", account))

        for index, (day, value) in enumerate(anchors):
            valuation = {
                "id": f"monarch-valuation:{name}:{day.isoformat()}",
                "account_id": account_source_id(name),
                "date": day.isoformat(),
                "amount": money(value * sign),
                "kind": "opening_anchor" if index == 0 else "reconciliation",
                "name": "Opening balance" if index == 0 else "Balance from Monarch",
            }
            if cfg.get("currency"):
                valuation["currency"] = cfg["currency"]
            records["Valuation"].append(record("Valuation", valuation))

    records["Category"] = [
        record("Category", {"id": cid, "name": n, **existing["categories"].get(n, {})})
        for n, cid in sorted(categories.items())
    ]
    stats["categories_reusing_existing"] = sum(1 for n in categories if n in existing["categories"])
    records["Tag"] = [record("Tag", {"id": tid, "name": n}) for n, tid in sorted(tags.items())]
    records["Merchant"] = [record("Merchant", {"id": mid, "name": n}) for n, mid in sorted(merchants.items())]

    ordered = [r for t in RECORD_ORDER for r in records[t]]
    for t in RECORD_ORDER:
        stats[t] = len(records[t])
    return ordered, dict(stats)


def write_ndjson(records, path):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def chunk_lines(lines, max_bytes=DEFAULT_CHUNK_BYTES):
    chunk, size = [], 0
    for line in lines:
        line_size = len(line.encode("utf-8"))
        if chunk and size + line_size > max_bytes:
            yield "".join(chunk)
            chunk, size = [], 0
        chunk.append(line)
        size += line_size
    if chunk:
        yield "".join(chunk)


# ---------------------------------------------------------------------------
# Sure API
# ---------------------------------------------------------------------------

class SureClient:
    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def request(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "X-Api-Key": self.api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"{method} {path} failed with HTTP {e.code}: {detail}") from None

    def paginated(self, path, key):
        items, page = [], 1
        while True:
            body = self.request("GET", f"{path}?page={page}&per_page=100")
            items.extend(body.get(key, []))
            if page >= body.get("pagination", {}).get("total_pages", 1):
                return items
            page += 1

    def account_names(self):
        return {a["name"] for a in self.paginated("/api/v1/accounts", "accounts")}

    def existing_records(self):
        """Categories and merchants the import would otherwise overwrite.

        Sure's importer reuses a same-named category or merchant and rewrites
        its color, icon and logo from the import file.
        """
        categories = {
            c["name"]: {"color": c["color"], "lucide_icon": c["icon"]}
            for c in self.paginated("/api/v1/categories", "categories")
        }
        merchants = {
            m["name"] for m in self.request("GET", "/api/v1/merchants") if m.get("type") == "FamilyMerchant"
        }
        return {"categories": categories, "merchants": merchants}


def conflicts_with_existing_data(records, client, allow_existing_accounts=False):
    """Return reasons the import would duplicate or rewrite data already in Sure."""
    problems = []
    names = {r["data"]["name"] for r in records if r["type"] == "Account"}
    clashes = sorted(names & client.account_names())
    if clashes and not allow_existing_accounts:
        problems.append(
            "Sure already has accounts with these names, so the import may duplicate them: "
            + ", ".join(clashes) + ". Pass --allow-existing-accounts to import anyway."
        )

    existing = client.existing_records()
    recolored = sorted(
        r["data"]["name"] for r in records
        if r["type"] == "Category" and r["data"]["name"] in existing["categories"]
        and {k: r["data"].get(k) for k in ("color", "lucide_icon")} != existing["categories"][r["data"]["name"]]
    )
    merchants = sorted(
        r["data"]["name"] for r in records
        if r["type"] == "Merchant" and r["data"]["name"] in existing["merchants"]
    )
    if recolored or merchants:
        problems.append(
            "The import would change existing categories or merchants ("
            + ", ".join(recolored + merchants)
            + "). Re-run convert against this server so it can leave them as they are."
        )
    return problems


def upload(ndjson_path, client, allow_existing_accounts=False, poll_seconds=5):
    with open(ndjson_path, encoding="utf-8") as f:
        lines = f.readlines()
    content = "".join(lines)

    chunks = list(chunk_lines(lines))
    # The session ID is derived from the file, so re-running an interrupted
    # upload of the same file resumes that session instead of importing twice.
    session_id = "monarch-to-sure:" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:40]

    session = client.request("POST", "/api/v1/import_sessions", {
        "type": "SureImport",
        "client_session_id": session_id,
        "expected_chunks": len(chunks),
    })["data"]

    if session["status"] == "complete":
        print("This file was already imported; nothing to do.")
        return session

    # A resumed session may already have created some of these records, so the
    # checks only apply before anything has been uploaded.
    if session["status"] == "pending" and not session.get("chunks_count"):
        problems = conflicts_with_existing_data(
            [json.loads(line) for line in lines if line.strip()], client, allow_existing_accounts
        )
        if problems:
            raise RuntimeError("\n".join(problems))

    if session["status"] != "importing":
        for sequence, chunk in enumerate(chunks, start=1):
            print(f"Uploading chunk {sequence}/{len(chunks)}")
            client.request("POST", f"/api/v1/import_sessions/{session['id']}/chunks", {
                "sequence": sequence,
                "client_chunk_id": f"chunk-{sequence}",
                "raw_file_content": chunk,
            })
        client.request("POST", f"/api/v1/import_sessions/{session['id']}/publish")

    print("Waiting for Sure to process the import")
    while True:
        session = client.request("GET", f"/api/v1/import_sessions/{session['id']}")["data"]
        if session["status"] in ("complete", "failed"):
            return session
        time.sleep(poll_seconds)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_server_args(parser):
    parser.add_argument("--url", default=os.environ.get("SURE_URL"), help="Sure server URL (or SURE_URL)")
    parser.add_argument("--api-key", default=os.environ.get("SURE_API_KEY"), help="read/write API key (or SURE_API_KEY)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-config", help="write an editable account config from the exports")
    p.add_argument("--balances", required=True)
    p.add_argument("--transactions", required=True)
    p.add_argument("-o", "--output", default="accounts.json")
    p.add_argument("--no-input", action="store_true", help="don't ask which accounts to disable or skip")

    p = sub.add_parser("convert", help="build the Sure NDJSON import file")
    p.add_argument("--balances", required=True)
    p.add_argument("--transactions", required=True)
    p.add_argument("--config", default="accounts.json")
    p.add_argument("--include-pending", action="store_true", help="also import pending transactions")
    p.add_argument("-o", "--output", default="sure-import.ndjson")
    add_server_args(p)
    p.add_argument(
        "--offline", action="store_true",
        help="build without checking the server's existing categories and merchants; upload will refuse it if they clash",
    )

    p = sub.add_parser("upload", help="import an NDJSON file into Sure")
    p.add_argument("ndjson")
    add_server_args(p)
    p.add_argument("--allow-existing-accounts", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "init-config":
        interactive = sys.stdin.isatty() and not args.no_input
        config = init_config(args.balances, args.transactions, args.output, interactive=interactive)
        print(f"\nWrote {args.output} with {len(config['accounts'])} accounts. Review the types before converting.")

    elif args.command == "convert":
        existing = None
        if not args.offline:
            if not args.url or not args.api_key:
                parser.error("convert needs --url and --api-key (or SURE_URL and SURE_API_KEY), or --offline")
            existing = SureClient(args.url, args.api_key).existing_records()
        records, stats = convert(
            args.balances, args.transactions, load_config(args.config), args.include_pending, existing
        )
        write_ndjson(records, args.output)
        print(f"Wrote {args.output}")
        for key, value in sorted(stats.items()):
            print(f"  {key}: {value}")

    elif args.command == "upload":
        if not args.url or not args.api_key:
            parser.error("upload needs --url and --api-key (or SURE_URL and SURE_API_KEY)")
        session = upload(args.ndjson, SureClient(args.url, args.api_key), args.allow_existing_accounts)
        print(f"Import session {session['id']}: {session['status']}")
        print(json.dumps(session.get("summary", {}), indent=2))
        if session["status"] != "complete":
            print(json.dumps(session.get("error", {}), indent=2), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
