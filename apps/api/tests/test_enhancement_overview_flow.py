"""Hierarchy orchestration: budget, evidence lineage, cancellation and stale output."""
import copy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select, func

from apps.api.models.enhancement import EnhancementNode, EnhancementRun
from apps.api.models.chunks import DocumentChunk
from apps.api.services import knowledge_enhancement as service
from apps.worker.services import enhancement_processor as worker
from test_knowledge_enhancement import db, document, enable, step  # noqa: F401


def model(monkeypatch, callback=None, fail_overview=False):
    calls = []
    def generate(**kwargs):
        payload = json.loads(kwargs["prompt"])
        calls.append(payload)
        if "segments" in payload:
            return {"summary": {"text": "局部条件与事实", "evidence_ids": [0]},
                    "entities": [], "relations": [], "events": []}
        if callback:
            callback()
        if fail_overview:
            raise RuntimeError("PRIVATE_MODEL_ERROR")
        return {"summary": {"text": "综合概览，保留各处条件与冲突。",
                            "support_refs": [c["ref"] for c in payload["children"]]}}
    monkeypatch.setattr(worker, "build_provider_from_session", lambda _: SimpleNamespace(
        name="fake", _model="test", _timeout=30, generate_json=generate))
    return calls


def start(db, count=5, budget=6):
    enable(db, budget=budget, modules=["chapter", "overview"])
    doc, version = document(db, count=count)
    run = service.start_run(db, doc.id, cost_acknowledged=True)
    db.commit()
    return doc, version, run


def test_budget_includes_reducers_resume_without_reanalyzing(db, monkeypatch):
    doc, version, run = start(db)
    original = copy.deepcopy(version.structured_content)
    calls = model(monkeypatch)
    for _ in range(6):
        assert step(db)
    summary = service.run_summary(db, db.get(EnhancementRun, run["id"]))
    assert summary["status"] == "partial"
    assert summary["completed_windows"] == 5
    assert summary["hierarchy"]["completed_nodes"] == 1
    assert summary["hierarchy"]["total_nodes"] == 3
    assert summary["calls_used"] == 6
    service.resume_run(db, run["id"], additional_calls=2, cost_acknowledged=True)
    db.commit()
    assert step(db) and step(db)
    summary = service.run_summary(db, db.get(EnhancementRun, run["id"]))
    assert summary["status"] == "completed"
    assert summary["hierarchy"]["completed_nodes"] == 3
    assert len(calls) == 8 and sum("segments" in p for p in calls) == 5
    assert all(len(p["children"]) <= 4 for p in calls if "children" in p)
    assert calls[-1]["children"][0]["ref"].startswith("n:")
    db.refresh(version)
    assert version.structured_content == original
    assert db.scalar(select(func.count()).select_from(DocumentChunk)) == 0


def test_failure_spends_budget_and_preserves_leaves(db, monkeypatch):
    _, _, run = start(db, count=1, budget=2)
    model(monkeypatch, fail_overview=True)
    assert step(db)
    assert not step(db)
    detail = service.read_run(db, run["id"])
    assert detail["run"]["status"] == "partial"
    assert detail["run"]["calls_used"] == 2
    assert detail["windows"][0]["status"] == "completed"
    assert "PRIVATE_MODEL_ERROR" not in json.dumps(detail)
    service.resume_run(db, run["id"], additional_calls=1, cost_acknowledged=True)
    db.commit()
    calls = model(monkeypatch)
    assert step(db)
    assert len(calls) == 1 and "segments" not in calls[0]
    assert service.read_run(db, run["id"])["run"]["status"] == "completed"


@pytest.mark.parametrize("action", ["cancel", "source_change"])
def test_late_overview_result_is_never_published(db, monkeypatch, action):
    _, version, run = start(db, count=1, budget=2)
    model(monkeypatch)
    assert step(db)
    def interrupt():
        if action == "cancel":
            service.cancel_run(db, run["id"])
        else:
            version.structured_content = {"document_type": "txt", "blocks": [{"type": "paragraph", "text": "Changed"}]}
        db.commit()
    model(monkeypatch, callback=interrupt)
    step(db)
    node = db.scalar(select(EnhancementNode))
    assert node.result is None
    assert db.get(EnhancementRun, run["id"]).status == ("cancelled" if action == "cancel" else "stale")


def test_existing_configuration_does_not_gain_overview(db):
    enable(db, modules=["chapter", "graph"])
    doc, _ = document(db)
    run = service.start_run(db, doc.id, cost_acknowledged=True)
    assert run["hierarchy"]["enabled"] is False
    assert db.scalar(select(func.count()).select_from(EnhancementNode)) == 0
    enable(db, modules=["chapter", "overview"])
    new = service.start_run(db, doc.id, cost_acknowledged=True)
    assert new["id"] != run["id"] and new["hierarchy"]["enabled"]


def test_overview_requires_chapter(db):
    with pytest.raises(service.EnhancementError):
        enable(db, modules=["overview"])
