"""Opt-in read-only candidate check against explicitly selected local documents.

Loads candidate source in an ephemeral API process, not the running server.
No jobs, chunks, versions or model settings are written. Default does not call AI.
"""
import argparse
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--ids", type=int, nargs="+", required=True)
parser.add_argument("--with-ai", action="store_true", help="Up to two configured-model calls per document")
args = parser.parse_args()
if len(args.ids) > 10 or any(i <= 0 for i in args.ids):
    parser.error("select 1–10 positive document IDs")
source = (Path(__file__).resolve().parents[1] / "apps/api/services/chunking_adaptive.py").read_text()
program = "import types, sys\nmodule = types.ModuleType('apps.api.services.chunking_adaptive')\n"
program += "module.__package__ = 'apps.api.services'\nsys.modules[module.__name__] = module\n"
program += f"exec(compile({source!r}, 'chunking_adaptive.py', 'exec'), module.__dict__)\n"
program += f"DOCUMENT_IDS = {args.ids!r}\nWITH_AI = {args.with_ai!r}\n"
program += r'''
import asyncio, json
from sqlalchemy import text
from apps.api.core.db import AsyncSessionLocal
from apps.api.ai.provider import build_provider_from_db

async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text('SET TRANSACTION READ ONLY'))
        provider = await build_provider_from_db(db) if WITH_AI else None
        if provider is not None and hasattr(provider, '_timeout'):
            provider._timeout = min(float(provider._timeout), 10.0)
        for document_id in DOCUMENT_IDS:
            row = (await db.execute(text("""
                SELECT v.id, v.structured_content FROM documents d
                JOIN document_versions v ON v.id=d.current_version_id
                JOIN workspaces w ON w.id=d.workspace_id
                WHERE d.id=:id AND NOT d.is_deleted AND w.status='active'
            """), {'id': document_id})).first()
            if row is None or not row[1]:
                print(json.dumps({'document_id': document_id, 'status': 'missing_structure'}), flush=True)
                continue
            try:
                result = await asyncio.to_thread(module.build_adaptive_candidate, row[1], provider=provider, max_calls=2)
                summary = {key: result[key] for key in (
                    'decision', 'reasons', 'calls', 'accepted_windows', 'eligible_windows',
                    'assisted_blocks', 'total_blocks', 'baseline', 'candidate')}
                print(json.dumps({'document_id': document_id, 'version_id': row[0], **summary}, ensure_ascii=False), flush=True)
            except Exception as exc:
                print(json.dumps({'document_id': document_id, 'error_type': type(exc).__name__}), flush=True)
        await db.rollback()
asyncio.run(main())
'''
completed = subprocess.run(["docker", "compose", "exec", "-T", "api", "python", "-"],
                           input=program, text=True, timeout=300)
raise SystemExit(completed.returncode)
