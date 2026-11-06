"""Collect the v6 fresh test's statements: ask Genie once, build the query history.

    python collect.py [PROFILE]   # Databricks CLI profile (default: touchstone)

Asks every conversation in questions.json of the Genie space in space_id (follow-ups
in the same conversation), keeps every answer as returned (genie_answers.jsonl), and
writes pilot/history.jsonl: each statement Genie ran (and any queries in an
official.json, as SQL editor runs). Nothing is selected or edited.

The history is built from Genie's own replies, not exported from system.query.history:
the reply carries the statement exactly as it ran (16 of 16 in the v4 fresh test), so
no warehouse query is spent waiting for the system table. Run it once.
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
PROFILE = sys.argv[1] if len(sys.argv) > 1 else "touchstone"
SPACE = (HERE / "space_id").read_text().strip()
DONE = ("COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED")


def cli(*args: str) -> dict:
    out = subprocess.run(["databricks", "genie", *args, "--profile", PROFILE, "-o", "json"],
                         capture_output=True, text=True, timeout=300)
    text = out.stdout[out.stdout.find("{"):] if "{" in out.stdout else ""
    return json.loads(text) if text else {"_error": out.stderr[:300]}


def converse(turns: list[str]) -> list[dict]:
    out, conv = [], None
    for i, question in enumerate(turns):
        m = (cli("start-conversation", SPACE, question) if conv is None
             else cli("create-message", SPACE, conv, question))
        conv = conv or m.get("conversation_id")
        msg = m.get("message_id") or m.get("id")
        deadline = time.time() + 300
        while time.time() < deadline and m.get("status") not in DONE:
            time.sleep(4)
            m = cli("get-message", SPACE, conv, msg)
        queries = [a["query"] for a in m.get("attachments") or [] if a.get("query")]
        out.append({"question": question, "turn": i + 1, "context": turns[:i],
                    "conversation_id": conv, "status": m.get("status"),
                    "created_timestamp": m.get("created_timestamp"),
                    "error": m.get("error") or m.get("_error"),
                    "queries": [{"sql": q.get("query"), "statement_id": q.get("statement_id")}
                                for q in queries]})
    return out


def history(answers: list[dict], at: str) -> list[dict]:
    rows = []
    for a in answers:
        for q in a["queries"]:
            if not q["sql"]:
                continue
            ts = a["created_timestamp"]
            rows.append({"statement_id": q["statement_id"], "statement_text": q["sql"],
                         "start_time": (datetime.datetime.fromtimestamp(ts / 1000, datetime.UTC)
                                        .isoformat(timespec="milliseconds")
                                        .replace("+00:00", "Z")) if ts else at,
                         "executed_by": "genie-user", "client_application":
                         "Databricks SQL Genie Space", "session_id": a["conversation_id"],
                         "statement_type": "SELECT", "dashboard_id": None,
                         "genie_space_id": SPACE})
    official = HERE / "official.json"
    for name, sql in (json.loads(official.read_text()) if official.exists() else {}).items():
        rows.append({"statement_id": f"{name}-editor", "statement_text": sql, "start_time": at,
                     "executed_by": "analyst@example.com",
                     "client_application": "Databricks SQL Editor",
                     "session_id": f"sess-{name}-editor", "statement_type": "SELECT",
                     "dashboard_id": None, "genie_space_id": None})
    return rows


if __name__ == "__main__":
    if (HERE / "genie_answers.jsonl").exists():
        sys.exit("genie_answers.jsonl exists: the test asks Genie once")
    conversations = json.loads((HERE / "questions.json").read_text())
    with ThreadPoolExecutor(3) as pool:
        answers = [a for conv in pool.map(converse, conversations) for a in conv]
    at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    with open(HERE / "genie_answers.jsonl", "w") as f:
        for a in answers:
            f.write(json.dumps(a) + "\n")
    rows = history(answers, at)
    with open(HERE / "pilot/history.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(sum(a["status"] == "COMPLETED" for a in answers), "of", len(answers), "answered;",
          len(rows), "history rows")
