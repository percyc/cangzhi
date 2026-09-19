"""Shared-search integration: selection changes anchors, not scope or ranking."""
import asyncio
from types import SimpleNamespace

import pytest

from apps.api.models.chunks import DocumentChunk
from apps.api.models.documents import Document, DocumentVersion, DocumentSourceType
from apps.api.services.hybrid_retrieval import RetrievalStatus, VectorRecall
from apps.api.services.scope_keys import DocumentSelection
from apps.api.services.search import _search_documents_lexical, search_documents
from apps.api.tests.test_search import search_db  # noqa: F401


@pytest.mark.parametrize('hybrid', [False, True])
def test_scoped_selection_preserves_document_order_scores_and_source(search_db, monkeypatch, hybrid):
    async def scenario():
        async with search_db() as db:
            docs=[]
            for number in range(3):
                document=Document(title='Operations manual',source_type=DocumentSourceType.note)
                db.add(document)
                await db.flush()
                version=DocumentVersion(document_id=document.id,version_number=1,
                    content_hash=str(number)*64,processing_status='ready')
                db.add(version)
                await db.flush()
                document.current_version_id=version.id
                docs.append(document.id)
                for index,body in enumerate([
                    'Operations manual installation. '*30,
                    'Renewal deadline is 30 days; written approval is required.',
                ]):
                    db.add(DocumentChunk(document_id=document.id,document_version_id=version.id,
                        external_id=str(index),role='child',chunk_type='paragraph',order_index=index,
                        content=body,search_text='Operations manual '+body,content_hash=str(index)*64,
                        char_count=len(body),page=9+index,paragraph_index=index,
                        source_start=index*1000,source_end=index*1000+len(body),is_current=True))
            await db.commit()
            async def vector(db, *,query,filters,limit):
                if not hybrid:
                    return VectorRecall([],RetrievalStatus('keyword',False,'test'))
                scoped=await _search_documents_lexical(db,query=query,limit=limit,**filters)
                rows=[]
                for candidates in scoped.candidate_rows.values():
                    for row in reversed(candidates):
                        values=dict(row._mapping)
                        values['rank']=0.9
                        rows.append(SimpleNamespace(**values))
                return VectorRecall(rows,RetrievalStatus('hybrid',True))
            monkeypatch.setattr('apps.api.services.hybrid_retrieval.recall_vector_chunks',vector)
            kwargs=dict(query='manual renewal deadline',limit=1,
                document_boundary=DocumentSelection(document_ids=docs[:2]))
            first=await search_documents(db,**kwargs)
            second=await search_documents(db,offset=1,**kwargs)
            assert [first.hits[0].document_id,second.hits[0].document_id]==docs[:2]
            for result in (first,second):
                hit=result.hits[0]
                assert 'installation' in hit.snippet
                assert hit.page==9 and hit.source_start==0
                assert len(hit.supporting_hits)==1
                extra=hit.supporting_hits[0]
                assert 'Renewal deadline is 30 days' in extra.snippet
                assert extra.page==10 and extra.source_start==1000
                assert extra.source_end==1000+len('Renewal deadline is 30 days; written approval is required.')
                assert extra.document_id==hit.document_id
                assert extra.document_version_id==hit.document_version_id
                assert hit.retrieval_channels==(['lexical','vector'] if hybrid else ['lexical'])
                assert hit.score>0
                public=result.to_dict()
                assert 'candidate_rows' not in public
                assert 'document_boundary' not in public['filters']
                support=public['hits'][0]['supporting_evidence']
                assert len(support)==1 and support[0]['chunk']['id']==extra.chunk_id
                assert 'supporting_evidence' not in support[0]
            empty=await search_documents(db,query='manual renewal',matches_none=True)
            assert empty.hits==[]
            from apps.api.services.deep_analysis import DeepAnalysisService, AgentDecision
            from apps.api.services.qa import AskRequest
            allowed=set()
            observation, _, evidence, _ = await DeepAnalysisService(None)._execute_action(
                db, request=AskRequest(question='manual renewal deadline',
                    document_boundary=DocumentSelection(document_ids=docs[:2])),
                decision=AgentDecision(action='search',query='manual renewal deadline',summary='find evidence'),
                allowed_dataset_documents=set(),allowed_dataset_ids=set(),allowed_chunk_ids=allowed)
            assert len(observation['output']['hits'])==2
            assert len(evidence)==4
            assert {e.chunk_id for e in evidence}==allowed
            assert {e.document_id for e in evidence}==set(docs[:2])
            assert all(h['supporting_evidence'] for h in observation['output']['hits'])
            from apps.api.api.mcp import _call_tool
            from apps.api.security.api_auth import APIIdentity
            identity=APIIdentity(admin=SimpleNamespace(id=1),auth_method='cookie',
                scopes=frozenset({'knowledge:search'}))
            mcp=await _call_tool('knowledge_search',{
                'query':'manual renewal deadline',
                'document_selection':{'document_ids':docs[:2]},'limit':1},identity,db)
            hit=mcp['structuredContent']['hits'][0]
            assert hit['document_id']==docs[0]
            assert len(hit['supporting_evidence'])==1
            assert hit['supporting_evidence'][0]['document_id']==docs[0]
    asyncio.run(scenario())
