"""Ingestion CLI (local orchestrator).

  erp-ingest sync [--source localfs-synthetic|localfs-private|all]
  erp-ingest reconcile --source NAME --tenant TENANT_ID
  erp-ingest replay-event EVENT_ID
  erp-ingest status --tenant TENANT_ID

Output contains IDs, outcomes and coverage only — never document text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from erp_ingestion.wiring import build_pipeline
from erp_rag.config import Settings
from erp_rag.runtime import build_core


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    core = await build_core(settings)
    roots = {k: v for k, v in settings.local_sources.items() if v.exists()}
    pipeline = build_pipeline(core, local_roots=roots)
    try:
        if args.cmd == "sync":
            names = list(roots) if args.source == "all" else [args.source]
            failed = 0
            for name in names:
                results = await pipeline.sync(name)
                for r in results:
                    print(
                        json.dumps(
                            {
                                "source": name,
                                "outcome": r.outcome,
                                "document_id": r.document_id,
                                "version": r.document_version,
                                "chunks": r.chunks,
                                "detail": r.detail,
                            }
                        )
                    )
                    failed += r.outcome == "failed"
                print(json.dumps({"source": name, "events": len(results)}), file=sys.stderr)
            return 1 if failed else 0
        if args.cmd == "reconcile":
            for r in await pipeline.reconcile(args.source, args.tenant):
                print(json.dumps({"outcome": r.outcome, "document_id": r.document_id}))
            return 0
        if args.cmd == "replay-event":
            r = await pipeline.replay_failed(args.event_id)
            print(json.dumps({"outcome": r.outcome, "document_id": r.document_id, "detail": r.detail}))
            return 0 if r.outcome != "failed" else 1
        if args.cmd == "status":
            for rec in await core.store.list_tenant(args.tenant):
                print(
                    json.dumps(
                        {
                            "document_id": rec.document_id,
                            "title": rec.title,
                            "status": rec.status,
                            "version": rec.current_version,
                            "label": rec.sensitivity_label,
                            "acl_version": rec.acl_version,
                            "revoked": rec.revoked,
                            "governance": rec.governance_source,
                        }
                    )
                )
            return 0
        return 2
    finally:
        await core.close()


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="erp-ingest")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync")
    s.add_argument("--source", default="all")
    r = sub.add_parser("reconcile")
    r.add_argument("--source", required=True)
    r.add_argument("--tenant", required=True)
    e = sub.add_parser("replay-event")
    e.add_argument("event_id")
    st = sub.add_parser("status")
    st.add_argument("--tenant", required=True)
    sys.exit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
