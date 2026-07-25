"""Attestation MCP server (§6, pulled forward from Phase 2).

The receipt schema was frozen in Phase 0 precisely so this surface could
exist: an agent about to answer "what was net revenue in March" first calls
`attest` and learns whether the number it is about to quote rests on a
CERTIFIED definition or a CONTRADICTION — and ships the receipt with its
answer. This module puts that check in the agent's answer path today.

Deliberately dependency-free: MCP's stdio transport is newline-delimited
JSON-RPC 2.0, which the stdlib handles fine. The server is read-only over an
existing store — scans happen elsewhere; agents get attestation, as-of
meaning, history, and receipt verification. Wire it up with:

    {"command": "semantix", "args": ["mcp", "--db", "/path/to/semantix.db"]}
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from typing import Any, Callable, IO

from semantix import __version__
from semantix.signing import verify_receipt_dict
from semantix.store import Store

PROTOCOL_VERSION = "2025-06-18"


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties,
                        "required": required},
    }


TOOLS = [
    _tool(
        "attest",
        "Issue a signed semantic receipt for a metric concept. Call this BEFORE "
        "quoting a numeric value for a business metric: the receipt says whether "
        "the metric's definition is CERTIFIED, CONTRADICTED, or unverifiable, "
        "which definition versions are live, their physical lineage, and every "
        "known divergence. Its `freshness` block says whether the estate still "
        "matches what the verdicts were computed from (fresh/partial/stale/unknown) — "
        "treat a stale receipt as needing a rescan, not as truth. Quote the "
        "receipt_id alongside the number.",
        {"concept": {"type": "string", "description": "metric/concept name, e.g. net_revenue"},
         "as_of": {"type": "string", "description": "optional ISO timestamp the answer refers to"}},
        ["concept"],
    ),
    _tool(
        "asof",
        "What did this metric MEAN at a point in time — the definition(s) live at "
        "that date. Use when answering about historical periods: a metric whose "
        "definition changed since then is a silent restatement risk.",
        {"concept": {"type": "string"},
         "at": {"type": "string", "description": "ISO timestamp, e.g. 2026-03-01T00:00:00Z"}},
        ["concept", "at"],
    ),
    _tool(
        "history",
        "Valid-time history of a metric's definitions across every site that "
        "computes it, with definition-change boundaries (restatement points).",
        {"concept": {"type": "string"}},
        ["concept"],
    ),
    _tool(
        "concepts",
        "List every concept and verdict from the latest scan — the estate's "
        "current semantic health. Severity-ranked; CONTRADICTION means competing "
        "definitions disagree.",
        {}, [],
    ),
    _tool(
        "relationships",
        "Certified join paths between tables. Call this BEFORE writing any SQL "
        "that joins tables: `certified` edges were checked against the "
        "warehouse (unique key, zero orphans) and are safe to join through; "
        "`failed` edges are known broken — joining through them silently "
        "multiplies or drops rows; `unverified` edges were seen in real "
        "queries but never checked, so treat them as a hypothesis, not a "
        "fact. Frequency of use is NOT evidence of correctness.",
        {"table": {"type": "string",
                   "description": "optional: only edges touching this relation, "
                                  "e.g. analytics.fct_orders"}},
        [],
    ),
    _tool(
        "verify_receipt",
        "Verify a semantic receipt's Ed25519 signature. Use to check a receipt "
        "received from elsewhere before trusting its status.",
        {"receipt": {"type": "object", "description": "the receipt JSON object"},
         "public_key": {"type": "string", "description": "optional pinned signer key (base64url)"}},
        ["receipt"],
    ),
]


class McpServer:
    def __init__(self, db_path: str, estate: dict | None = None):
        self.store = Store(db_path)
        # Estate sources (services.freshness kwargs). When configured, every
        # attest re-checks the live estate so receipts decay honestly.
        self.estate = estate
        self._handlers: dict[str, Callable[[dict], Any]] = {
            "attest": self._attest,
            "asof": self._asof,
            "history": self._history,
            "concepts": self._concepts,
            "relationships": self._relationships,
            "verify_receipt": self._verify_receipt,
        }

    # ------------------------------------------------------------- tool impls

    def _attest(self, args: dict) -> Any:
        freshness = None
        if self.estate:
            from semantix.services.freshness import check_freshness

            freshness = check_freshness(self.store, **self.estate)
        receipt = self.store.issue_receipt(
            args["concept"], args.get("as_of"), freshness=freshness
        )
        if receipt is None:
            return {"error": f"no verdicts recorded for {args['concept']!r} — "
                             "has this estate been scanned?"}
        return receipt.as_dict()

    def _asof(self, args: dict) -> Any:
        live = self.store.as_of(args["concept"], args["at"])
        if not live:
            return {"error": f"{args['concept']!r} had no live definition at {args['at']}"}
        out = []
        for iv in live:
            row = self.store.fingerprint_row(iv.ref_id)
            out.append({**asdict(iv),
                        "measure_expr": row["measure_expr"] if row else None})
        return {"concept": args["concept"], "at": args["at"], "definitions": out}

    def _history(self, args: dict) -> Any:
        intervals = self.store.history(args["concept"])
        if not intervals:
            return {"error": f"no recorded definitions for {args['concept']!r}"}
        changes = [iv for iv in intervals if iv.valid_to is not None]
        return {
            "concept": args["concept"],
            "intervals": [asdict(iv) for iv in intervals],
            "restatement_boundaries": len(changes),
            "warning": ("historical comparisons spanning a definition change are "
                        "semantic restatements" if changes else None),
        }

    def _concepts(self, args: dict) -> Any:
        scan = self.store.latest_scan()
        if scan is None:
            return {"error": "no scans recorded in this store"}
        return {
            "scan_id": scan["scan_id"],
            "started_at": scan["started_at"],
            "verdicts": [
                {"verdict": v["verdict"], "severity": v["severity"],
                 "concept": v["concept_label"], "summary": v["summary"]}
                for v in self.store.verdicts_for_scan(scan["scan_id"])
            ],
        }

    def _relationships(self, args: dict) -> Any:
        from semantix.services.relationships import relationships_for_agent

        result = relationships_for_agent(self.store, args.get("table"))
        if not (result["certified"] or result["failed"] or result["unverified"]):
            return {"error": "no relationships recorded — run "
                             "`semantix mine-relationships` against query history"}
        return result

    def _verify_receipt(self, args: dict) -> Any:
        ok, message = verify_receipt_dict(args["receipt"], args.get("public_key"))
        return {"valid": ok, "detail": message}

    # ---------------------------------------------------------------- rpc core

    def handle(self, message: dict) -> dict | None:
        """One JSON-RPC message → response dict (None for notifications)."""
        method = message.get("method", "")
        msg_id = message.get("id")
        if msg_id is None:  # notification — nothing to say back
            return None

        if method == "initialize":
            return self._result(msg_id, {
                "protocolVersion": message.get("params", {}).get(
                    "protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "semantix", "version": __version__},
            })
        if method == "ping":
            return self._result(msg_id, {})
        if method == "tools/list":
            return self._result(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            params = message.get("params", {})
            handler = self._handlers.get(params.get("name", ""))
            if handler is None:
                return self._error(msg_id, -32602, f"unknown tool {params.get('name')!r}")
            try:
                payload = handler(params.get("arguments") or {})
            except KeyError as e:
                return self._result(msg_id, _tool_error(f"missing argument {e}"))
            except Exception as e:  # noqa: BLE001 — surface, don't crash the server
                return self._result(msg_id, _tool_error(str(e)))
            is_error = isinstance(payload, dict) and "error" in payload
            return self._result(msg_id, {
                "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
                "isError": is_error,
            })
        return self._error(msg_id, -32601, f"method {method!r} not supported")

    @staticmethod
    def _result(msg_id: Any, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": code, "message": message}}


def _tool_error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def serve(db_path: str, stdin: IO[str] = sys.stdin, stdout: IO[str] = sys.stdout,
          estate: dict | None = None) -> int:
    """Blocking stdio loop: one JSON-RPC message per line."""
    server = McpServer(db_path, estate=estate)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue  # not ours to answer; malformed ids are unrecoverable
        response = server.handle(message)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
    return 0
