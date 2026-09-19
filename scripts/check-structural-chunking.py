"""Read-only, no-model comparison of explicit documents and structural candidates."""
import argparse
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--ids', type=int, nargs='+', required=True)
args = parser.parse_args()
if not 1 <= len(args.ids) <= 10 or any(i <= 0 for i in args.ids):
    parser.error('select 1–10 positive document IDs')
program = "import types, sys\n"
for name in ('chunker', 'chunking_structural'):
    source = (Path(__file__).resolve().parents[1] / f'apps/api/services/{name}.py').read_text()
    program += f"module = types.ModuleType('apps.api.services.{name}')\n"
    program += "module.__package__ = 'apps.api.services'\nsys.modules[module.__name__] = module\n"
    program += f"exec(compile({source!r}, '{name}.py', 'exec'), module.__dict__)\n"
program += f"DOCUMENT_IDS={args.ids!r}\n"
program += r'''
import asyncio, json
from sqlalchemy import text
from apps.api.core.db import AsyncSessionLocal, engine
from apps.api.services.chunking_candidate import build_candidate
from apps.api.documents import detect_document_type, get_chunking_config

async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text('SET TRANSACTION READ ONLY'))
        for identifier in DOCUMENT_IDS:
            row = (await db.execute(text("""
                SELECT v.structured_content, d.title FROM documents d
                JOIN document_versions v ON v.id=d.current_version_id
                JOIN workspaces w ON w.id=d.workspace_id
                WHERE d.id=:id AND NOT d.is_deleted AND w.status='active'
                  AND length(v.structured_content::text)<=8000000
            """), {'id': identifier})).first()
            if not row or not row[0]:
                print(json.dumps({'id': identifier, 'status': 'unavailable_or_oversize'}), flush=True)
                continue
            existing = (await db.execute(text("""
                SELECT count(*),count(*) FILTER(WHERE c.char_count<50),
                       count(*) FILTER(WHERE c.char_count<100)
                FROM document_chunks c JOIN documents d ON d.id=c.document_id
                WHERE d.id=:id AND c.document_version_id=d.current_version_id AND c.role='child' AND c.is_current
            """), {'id': identifier})).one()
            try:
                blocks = row[0].get('blocks') or []
                profile = detect_document_type(
                    parser_type=row[0].get('document_type'), title=row[1],
                    block_types=[b['type'] for b in blocks],
                    headings=[b['text'] for b in blocks if b['type']=='heading' and b.get('text')],
                    first_paragraphs=[b['text'] for b in blocks if b['type']=='paragraph' and b.get('text')][:10])
                config = get_chunking_config(profile)
                sizing = dict(child_max_chars=config.child_target_max_chars,
                    child_hard_max_chars=config.child_hard_max_chars,
                    child_min_chars=config.child_target_min_chars,
                    child_overlap_chars=config.child_overlap_chars)
                result = module.build_structural_candidate(row[0], **sizing)
                old = build_candidate(row[0], max_calls=0, **sizing)
                children = [s for s in result['specs'] if s['role']=='child']
                report = {'id': identifier, 'type': row[0].get('document_type'),
                    'serving_children':existing[0], 'serving_under50':existing[1],
                    'serving_under100':existing[2], 'candidate':result['candidate'],
                    'v1_no_model':old['candidate'], 'calls':result['calls'],
                    'sizing':sizing, 'parent_text_complete':result['parent_text_complete'],
                    'source_content_complete':result['source_content_complete'],
                    'uncovered_nonspace_chars':result['uncovered_nonspace_chars'],
                    'unverified_spans':result['unverified_spans'],
                    'fragmentation_regressed':module.fragmentation_regressed(
                        {'children':existing[0], 'short_children':existing[2]}, result['candidate']),
                    'candidate_under50':sum(s['char_count']<50 for s in children),
                    'candidate_max_chars':max((s['char_count'] for s in children),default=0)}
                print(json.dumps(report, ensure_ascii=False), flush=True)
            except Exception as error:
                print(json.dumps({'id':identifier,'error_type':type(error).__name__}), flush=True)
        await db.rollback()
    await engine.dispose()
asyncio.run(main())
'''
completed = subprocess.run(['docker','compose','exec','-T','api','python','-'], input=program,
                           text=True, timeout=300)
raise SystemExit(completed.returncode)
