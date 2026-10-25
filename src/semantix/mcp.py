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
import pathlib
from typing import Any, Callable, IO

from semantix import __version__
from semantix.signing import assess_receipt, verify_receipt_dict
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
        "matches what the verdicts were computed from "
        "(fresh/partial/stale/unknown/unchecked — `unchecked` means no estate "
        "sources were configured, so nothing was re-checked) — "
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
        "Verify a semantic receipt before relying on it. Returns `valid` (signature "
        "holds AND the receipt is valid as the kind of statement it makes), "
        "`describes` (`current` or `as_of`), `historically_valid` (a true record of "
        "its instant) and `current_use` (the definition may be relied on NOW — only "
        "for a current receipt whose freshness re-check found the estate unchanged; "
        "never for an as-of receipt). Quote a metric as current only if "
        "`current_use` is true.",
        {"receipt": {"type": "object", "description": "the receipt JSON object"},
         "public_key": {"type": "string", "description": "optional pinned signer key (base64url)"}},
        ["receipt"],
    ),
]


class McpServer:
    def __init__(self, db_path: str, estate: dict | None = None):
        # The round-five typo'd-`--db` guard covered all nine CLI read
        # commands and missed this one — the agent-facing entrypoint, where a
        # path typo silently created an empty store and every tool call then
        # answered "no scans recorded": a data-problem answer to a path
        # problem, on the surface least able to notice.
        if not pathlib.Path(db_path).exists():
            raise ValueError(
                f"no store at {db_path} — the MCP server serves an existing "
                f"store and will not create one")
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
        unknown = sorted(set(args) - {"concept", "as_of"})
        if unknown:
            # Estate sources are SERVER configuration (--dbt/--osi/... at
            # startup). A per-call `osi_path` was silently ignored, so a
            # caller believed it had re-checked freshness against a document
            # the server never opened.
            return {"error": f"unknown argument(s) {', '.join(unknown)} — estate "
                             f"sources for freshness are configured when the "
                             f"server starts, not per call"}
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

    def _live_freshness(self, receipt: dict) -> dict | None:
        """A freshness check NOW, against the receipt's own scan — only when
        this server was started with estate sources. Without them there is
        nothing to look at, and the receipt is historical."""
        scan_id = receipt.get("scan_id") if isinstance(receipt, dict) else None
        if not self.estate or not isinstance(scan_id, str):
            return None
        from semantix.services.freshness import check_freshness

        try:
            return check_freshness(self.store, scan_id=scan_id, **self.estate)
        except Exception as e:  # noqa: BLE001 — degrade, never crash verification
            return {"status": "unknown", "scan_id": scan_id,
                    "note": f"estate re-check failed ({type(e).__name__}: {e})"}

    def _verify_receipt(self, args: dict) -> Any:
        ok, message = verify_receipt_dict(args["receipt"], args.get("public_key"))
        # A receipt that does not verify is an ERROR result, not a successful
        # call that happens to say False. An agent checking only the
        # transport-level flag would otherwise treat a forged receipt as good.
        result = {"valid": ok, "detail": message}
        if not ok:
            result["error"] = f"receipt did not verify: {message}"
            return result
        # A valid SIGNATURE is not a valid STATUS. This tool is documented as
        # "check a receipt before trusting its status", and it evaluated bytes
        # only — the decay check existed solely on the CLI, so the surface
        # agents actually use kept trusting receipts whose OWN freshness block
        # says the estate has drifted.
        # Two decisions, never one boolean: round eight copied historical
        # validity into `current_use`, so a stale as-of receipt told agents
        # `current_use: true` beside a warning saying it described the past.
        live = self._live_freshness(args["receipt"])
        a = assess_receipt(args["receipt"], live)
        if live is not None:
            result["live_freshness"] = {k: live.get(k) for k in
                                        ("status", "checked_at", "scan_id", "note")}
        result.update({
            "describes": a.describes,
            "instant": a.instant,
            "historically_valid": a.historical,
            "current_use": a.current_use,
            "trusted": a.valid_for_stated_use,
            "reliance": a.reliance(),
        })
        if a.warnings:
            result["warnings"] = list(a.warnings)
        if not a.valid_for_stated_use:
            result["valid"] = False
            result["error"] = ("signature is valid but the receipt cannot be relied "
                               "on: " + "; ".join(a.warnings))
        return result

    # ---------------------------------------------------------------- rpc core

    def handle(self, message: Any) -> Any:
        """One JSON-RPC message → response (None for notifications).

        Accepts anything the transport can deliver. A batch is a JSON array
        and a malformed client can send a bare scalar; assuming an object
        raised out of the loop and killed attestation for the whole session,
        which is a poor answer to a bad request.
        """
        if isinstance(message, list):
            if not message:
                return self._error(None, -32600, "empty batch")
            replies = [r for r in (self.handle(m) for m in message) if r is not None]
            return replies or None
        if not isinstance(message, dict):
            return self._error(None, -32600, "request must be a JSON object or array")

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
