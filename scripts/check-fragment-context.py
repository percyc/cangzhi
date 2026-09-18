"""Read-only, opt-in production fixture check in a separate Python process.

Run from repository root. Does not modify containers, indexes or source data.
Uses the running API's configuration without reading/exporting any secrets.
Only public-standard questions are printed; no other retrieved text is logged.
Literal coverage assertions are smoke checks, not a substitute for reviewing
the answer's meaning or a cross-document retrieval golden set.
"""
import argparse
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--deep", action="store_true", help="Validate deep analysis instead of quick Q&A")
parser.add_argument("--search-only", action="store_true", help="Check shared search and MCP adapter, without model generation")
parser.add_argument("--deployed", action="store_true", help="Verify and use installed service code; do not load candidate modules")
args = parser.parse_args()

modules = {
    f"apps.api.services.{name}": Path(f"apps/api/services/{name}.py").read_text()
    for name in ("fragment_context", "search", "qa", "deep_analysis")
}
program = '''
import asyncio, json, sys, types, unicodedata, importlib, hashlib
from pathlib import Path
from types import SimpleNamespace
import apps.api.models
from sqlalchemy import text
from apps.api.core.db import AsyncSessionLocal
from apps.api.services.workspaces import bind_workspace_context
SOURCES = SOURCES_PLACEHOLDER
DEPLOYED = DEPLOYED_PLACEHOLDER
for name, source in SOURCES.items():
    if DEPLOYED:
        module = importlib.import_module(name)
        assert hashlib.sha256(Path(module.__file__).read_bytes()).digest() == hashlib.sha256(source.encode()).digest(), 'Deployed code mismatch: ' + name
        continue
    module = types.ModuleType(name)
    module.__file__ = '<candidate-validation>'
    sys.modules[name] = module
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
from apps.api.services.search import search_documents
from apps.api.services.qa import QAService, AskRequest
from apps.api.ai import build_provider_from_db
from apps.api.services.deep_analysis import DeepAnalysisService
DEEP = DEEP_PLACEHOLDER
SEARCH_ONLY = SEARCH_ONLY_PLACEHOLDER

async def main():
    async with AsyncSessionLocal() as db:
        await db.execute(text('SET TRANSACTION READ ONLY'))
        bind_workspace_context(db.sync_session, workspace_id=8, workspace_slug='validation')
        question = 'GB 14762-2008适用于哪些车辆和发动机？'
        result = await search_documents(db, query=question, limit=8)
        hits = [h for h in result.hits if h.document_id == 671]
        normalize = lambda s: ''.join(unicodedata.normalize('NFKC', s).split())
        recovered = normalize(hits[0].context or '') if hits else ''
        checks = {v: normalize(v) in recovered for v in ['25', 'M2', 'M3', 'N2', 'N3', '3500', 'M1', '可不按本标准']}
        print(json.dumps({'stage':'search', 'checks':checks, 'chars': len(hits[0].context or '') if hits else 0}, ensure_ascii=False), flush=True)
        assert all(checks.values()), 'Source coverage incomplete'
        from apps.api.api.mcp import _call_tool
        from apps.api.security.api_auth import APIIdentity
        identity = APIIdentity(admin=SimpleNamespace(id=0), auth_method='cookie', scopes=frozenset({'knowledge:search'}))
        mcp = await _call_tool('knowledge_search', {'query':question, 'limit':8}, identity, db)
        mcp_hits = [h for h in mcp['structuredContent']['hits'] if h['document_id'] == 671]
        assert mcp_hits and mcp_hits[0]['context'] == hits[0].context
        print(json.dumps({'stage':'mcp_adapter', 'same_context':True}), flush=True)
        if SEARCH_ONLY:
            await db.rollback()
            return
        provider = await build_provider_from_db(db)
        service = DeepAnalysisService(provider) if DEEP else QAService(provider)
        answer = await asyncio.wait_for(service.ask(db, AskRequest(question=question)), timeout=180)
        print(json.dumps({'stage':'answer', 'answer':answer.answer, 'insufficient_evidence':answer.insufficient_evidence,
                         'evidence_count':len(answer.evidence)}, ensure_ascii=False), flush=True)
        if DEEP:
            audits = (answer.retrieval or {}).get('analysis', {}).get('evidence_audits', [])
            print(json.dumps({'stage':'audit', 'completed':len(audits)}), flush=True)
            assert audits, 'Deep analysis did not complete an evidence audit'
        assert not answer.insufficient_evidence
        assert all(normalize(v) in normalize(answer.answer) for v in ['25', 'M2', 'M3', 'N2', 'N3', '3500', 'M1', '可不按本标准'])
        await db.rollback()
asyncio.run(main())
'''.replace('SOURCES_PLACEHOLDER', repr(modules)).replace('DEEP_PLACEHOLDER', repr(args.deep)).replace('SEARCH_ONLY_PLACEHOLDER', repr(args.search_only)).replace('DEPLOYED_PLACEHOLDER', repr(args.deployed))
result = subprocess.run(
    ["docker", "compose", "exec", "-T", "api", "python", "-"],
    input=program, text=True, timeout=240,
)
raise SystemExit(result.returncode)
