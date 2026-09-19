"""Opt-in read-only checks of known local fixtures; no source text in output.

Use before and after maintenance. These five (optionally seven) questions are smoke checks, not a
full retrieval golden set. Runs installed API code against the serving index.
"""
import argparse
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--strict", action="store_true")
parser.add_argument("--include-structural", action="store_true", help="Include two local structural trial PDFs")
args = parser.parse_args()
program = r'''
import asyncio, json, unicodedata
from sqlalchemy import text
from apps.api.core.db import AsyncSessionLocal
from apps.api.services.search import search_documents
from apps.api.services.workspaces import bind_workspace_context

CASES = [
    (8, 671, 'GB 14762-2008适用于哪些车辆和发动机？', ['25', 'M2', 'M3', 'N2', 'N3', '3500', 'M1', '可不按本标准']),
    (8, 664, 'GB 19756-2005的适用范围是什么，哪些柴油机可不按本标准进行型式核准？', ['三轮汽车', '低速货车', '17691', '可不按本标准']),
    (1, 389, '2008年修正的专利法规定外观设计专利权期限如何计算？', ['十年', '申请日']),
    (1, 7, '2026年5月8日AI智能体大赛总结会有多少个作品？', ['23', '100']),
    (1, 8, '实习计划的基础建设期要多久，阶段产出是什么？', ['1.5', '2', 'Demo']),
]
if INCLUDE_STRUCTURAL:
    CASES.extend([
        (8, 992, '声环境质量标准的1类声环境功能区主要包括哪些区域？',
         ['居民住宅', '医疗卫生', '文化教育', '科研设计', '行政办公']),
        (8, 994, '国家危险废物名录中感染性、损伤性和病理性废物的代码分别是什么？',
         ['感染性废物', '841-001-01', '损伤性废物', '841-002-01', '病理性废物', '841-003-01']),
    ])
normalize = lambda value: ''.join(unicodedata.normalize('NFKC', value).split()).lower()
async def main():
    outcomes = []
    for workspace, doc, question, terms in CASES:
        async with AsyncSessionLocal() as db:
            await db.execute(text('SET TRANSACTION READ ONLY'))
            bind_workspace_context(db.sync_session, workspace_id=workspace, workspace_slug='validation')
            result = await search_documents(db, query=question, limit=8)
            hits = [hit for hit in result.hits if hit.document_id == doc]
            context = normalize('\n'.join((hit.context or hit.snippet or '') for hit in hits))
            checks = [normalize(term) in context for term in terms]
            ok = bool(hits) and all(checks)
            outcomes.append(ok)
            print(json.dumps({'document_id': doc, 'hits':len(hits), 'condition_checks':checks,
                              'passed':ok}, ensure_ascii=False), flush=True)
            await db.rollback()
    if STRICT and not all(outcomes):
        raise SystemExit(1)
asyncio.run(main())
'''.replace('STRICT', repr(args.strict)).replace('INCLUDE_STRUCTURAL', repr(args.include_structural))
result = subprocess.run(["docker", "compose", "exec", "-T", "api", "python", "-"],
                        input=program, text=True, timeout=240)
raise SystemExit(result.returncode)
