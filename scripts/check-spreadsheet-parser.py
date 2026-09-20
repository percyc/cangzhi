"""Read-only local candidate check; no requeue, DB writes or model calls.

Usage: python3 scripts/check-spreadsheet-parser.py DOCUMENT_ID
Requires the existing API service. Candidate code lives only in this subprocess.
Only counts and timings are printed, never file contents or storage keys.
"""
import argparse
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("document_id", type=int)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
sources = {
    name: (root / (name.replace(".", "/") + ".py")).read_text()
    for name in (
        "apps.api.parsers.spreadsheet_layout",
        "apps.api.parsers.spreadsheet",
        "apps.api.services.chunker",
        "apps.api.services.structured_table",
    )
}
program = r'''
import asyncio, json, sys, time, types
from sqlalchemy import text
from apps.api.core.db import AsyncSessionLocal
from apps.api.storage.local import LocalBlobStorage
from apps.api.parsers.spreadsheet import XlsxParser as InstalledParser
SOURCES = __SOURCES__
async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text('SET TRANSACTION READ ONLY'))
        key = (await db.execute(text("""
            SELECT b.storage_key FROM documents d
            JOIN document_versions v ON v.id=d.current_version_id
            JOIN blobs b ON b.id=v.blob_id WHERE d.id=:id
        """), {'id': __DOCUMENT_ID__})).scalar_one()
    with LocalBlobStorage('/app/storage').open(key) as handle:
        content = handle.read()
    start = time.monotonic()
    old = InstalledParser().parse(content)
    print(json.dumps({'installed_success':old.success, 'installed_error':old.error_details,
                      'seconds':round(time.monotonic()-start, 2)}))
    for name, source in SOURCES.items():
        module = types.ModuleType(name)
        module.__package__ = name.rsplit('.', 1)[0]
        sys.modules[name] = module
        exec(compile(source, '<candidate>', 'exec'), module.__dict__)
    candidate = sys.modules['apps.api.parsers.spreadsheet']
    start = time.monotonic()
    result = candidate.XlsxParser().parse(content)
    report = {'candidate_success':result.success, 'error':result.error_details,
              'seconds':round(time.monotonic()-start, 2)}
    if result.success:
        structured = result.structured_content
        chunks = sys.modules['apps.api.services.chunker'].build_chunk_specs(structured)
        rows = sys.modules['apps.api.services.structured_table'].extract_table_row_payloads(structured)
        report.update(non_empty_cells=structured.metadata.get('non_empty_cells'),
            non_empty_rows=structured.metadata.get('non_empty_rows'), blocks=len(structured.blocks),
            dataset_rows=len(rows), child_chunks=sum(c.role=='child' for c in chunks),
            fallback_blocks=sum(b.extra.get('dataset_eligible') is False for b in structured.blocks))
    print(json.dumps(report, ensure_ascii=False))
asyncio.run(main())
'''
program = program.replace("__SOURCES__", repr(sources)).replace("__DOCUMENT_ID__", str(args.document_id))
subprocess.run(["docker", "compose", "exec", "-T", "api", "python", "-"],
               input=program, text=True, cwd=root, check=True)
