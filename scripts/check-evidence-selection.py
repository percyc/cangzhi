"""Opt-in paired, read-only evaluation on the maintainer's local fixtures.

No generation, production writes, or source text output. One query embedding
per fixture, shared by baseline/candidate; candidate modules live only in the
diagnostic subprocess. These draft labels are not a representative golden set.
"""
import argparse
import json
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--candidate', action='store_true')
parser.add_argument('--count', type=int, default=30)
parser.add_argument('--indices', type=int, nargs='+', help='One-based fixture indices for diagnosis')
parser.add_argument('--diagnose', action='store_true', help='Print local selector scores, never source text')
args = parser.parse_args()
if not 1 <= args.count <= 30:
    parser.error('count must be between 1 and 30')
root = Path(__file__).resolve().parents[1]
cases = json.loads((root / 'scripts/retrieval-fixtures.json').read_text())[:args.count]
if args.indices:
    if any(i < 1 or i > len(cases) for i in args.indices):
        parser.error('fixture index outside selected range')
    cases = [cases[i-1] for i in args.indices]
sources = {name: (root / f'apps/api/services/{name}.py').read_text()
           for name in ('evidence_selection', 'search')} if args.candidate else {}
program = r'''
import asyncio,copy,hashlib,importlib,json,sys,time,types,unicodedata
from sqlalchemy import text
from apps.api.core.db import AsyncSessionLocal
from apps.api.services.workspaces import bind_workspace_context
import apps.api.services.search as baseline
import apps.api.services.hybrid_retrieval as hybrid
CASES = CASES_PLACEHOLDER
SOURCES = SOURCES_PLACEHOLDER
DIAGNOSE = DIAGNOSE_PLACEHOLDER
candidate = baseline
for name, source in SOURCES.items():
 module = types.ModuleType('apps.api.services.'+name)
 module.__package__='apps.api.services'
 sys.modules[module.__name__]=module
 exec(compile(source,'<candidate:'+name+'>','exec'),module.__dict__)
 if name=='search': candidate=module
N=lambda s: ''.join(unicodedata.normalize('NFKC',s).split()).lower()
original_vector=hybrid.recall_vector_chunks
original_lexical=baseline._search_documents_lexical
original_best=baseline._best_document_rows
async def main():
 totals={'baseline':0,'candidate':0,'labels_valid':0,'regressions':0}
 for index,(workspace,document,question,terms) in enumerate(CASES,1):
  cache=[]
  lexical_cache=[]
  row_cache=[]
  def capture_rows(rows,**kwargs):
   row_cache.extend(rows)
   return original_best(rows,**kwargs)
  async def lexical(*args,**kwargs):
   if not lexical_cache:
    value=await original_lexical(*args,**kwargs)
    lexical_cache.append(copy.deepcopy(value))
   value=copy.deepcopy(lexical_cache[0])
   if SOURCES:
    value.candidate_rows=candidate._retain_document_candidates(row_cache,[h.document_id for h in value.hits])
    value.hits=[candidate.SearchHit(**vars(hit)) for hit in value.hits]
   return value
  async def vector(*args,**kwargs):
   if not cache: cache.append(await original_vector(*args,**kwargs))
   return cache[0]
  hybrid.recall_vector_chunks=vector
  baseline._best_document_rows=capture_rows
  baseline._search_documents_lexical=lexical
  candidate._search_documents_lexical=lexical
  async with AsyncSessionLocal() as db:
   await db.execute(text('SET TRANSACTION READ ONLY'))
   bind_workspace_context(db.sync_session,workspace_id=workspace,workspace_slug='validation')
   source=(await db.execute(text('SELECT v.raw_content FROM documents d JOIN document_versions v ON v.id=d.current_version_id WHERE d.id=:id AND d.workspace_id=:ws'),{'id':document,'ws':workspace})).scalar()
   label_valid=bool(source) and all(N(t) in N(source) for t in terms)
   totals['labels_valid']+=int(label_valid)
   report={'case':index,'document':document,'label_valid':label_valid}
   primary_before=None
   for name,module in [('baseline',baseline),('candidate',candidate)]:
    if name=='candidate' and not SOURCES:
     report[name]=copy.deepcopy(report['baseline'])
     totals[name]+=int(report[name]['passed'])
     continue
    start=time.monotonic()
    selection={}
    def trace(frame,event,arg):
     if event=='return' and frame.f_code.co_name=='choose_evidence_row':
      values=frame.f_locals
      anchor=values.get('anchor')
      if anchor is not None and anchor.document_id==document and 'winner' in values:
       scores=values['scores']
       selection.update(anchor=scores[anchor.chunk_id],winner=scores[values['winner']],
        lost=[t for t in values['focus'] if anchor.chunk_id in values['present'][t] and values['winner'] not in values['present'][t]],
        vector=[r.chunk_id for r in values['vector']])
    if name=='candidate' and DIAGNOSE: sys.setprofile(trace)
    result=await module.search_documents(db,query=question,limit=8)
    sys.setprofile(None)
    primary=[{k:v for k,v in h.to_dict().items() if k!='supporting_evidence'} for h in result.hits]
    if name=='baseline': primary_before=primary
    positions=[i+1 for i,h in enumerate(result.hits) if h.document_id==document]
    hits=[h for h in result.hits if h.document_id==document]
    fragments=[part for h in hits for part in [h,*getattr(h,'supporting_hits',[])]]
    context=N('\n'.join(h.context or h.snippet or '' for h in fragments))
    passed=bool(hits) and all(N(t) in context for t in terms)
    totals[name]+=int(passed)
    report[name]={'passed':passed,'document_top5':bool(positions and positions[0]<=5),
      'chunks':[h.chunk_id for h in hits], 'seconds':round(time.monotonic()-start,3),
      'documents':[h.document_id for h in result.hits]}
    report[name]['supporting_chunks']=[extra.chunk_id for h in hits for extra in getattr(h,'supporting_hits',[])]
    report[name]['primary_unchanged']=primary==primary_before
    if selection: report[name]['selection']=selection
   if report['baseline']['passed'] and not report['candidate']['passed']: totals['regressions']+=1
   print(json.dumps(report,ensure_ascii=False),flush=True)
   await db.rollback()
 print(json.dumps({'totals':totals,'cases':len(CASES)}),flush=True)
asyncio.run(main())
'''.replace('CASES_PLACEHOLDER', repr(cases)).replace('SOURCES_PLACEHOLDER',repr(sources)).replace('DIAGNOSE_PLACEHOLDER',repr(args.diagnose))
result = subprocess.run(['docker','compose','exec','-T','api','python','-'],input=program,text=True,timeout=1800)
raise SystemExit(result.returncode)
