"""Guard the browser's v1 ask envelope without calling providers or storing history."""
import pytest
from pydantic import ValidationError

from apps.api.api.v1 import KnowledgeAskPayload, _selection


@pytest.mark.parametrize("mode", ["quick", "deep"])
@pytest.mark.parametrize("document_ids", [[], [20, 21]])
def test_browser_ask_envelope(mode, document_ids):
    payload = {
        "question": "合成问题", "mode": mode, "scope_slug": "all",
        "category_ids": [], "tag_ids": [], "source_types": [],
        "connector_ids": [], "conversation_id": 9, "context_label": "全部知识",
    }
    if document_ids:
        payload["document_selection"] = {"document_ids": document_ids}
    parsed = KnowledgeAskPayload.model_validate(payload)
    selection = _selection(parsed.document_selection)
    if document_ids:
        assert list(selection.document_ids) == document_ids
    else:
        assert selection is None


def test_legacy_top_level_document_ids_remains_rejected():
    # Fix the caller, not the stable read API or document authorization boundary.
    with pytest.raises(ValidationError) as error:
        KnowledgeAskPayload.model_validate({"question": "合成问题", "document_ids": []})
    assert error.value.errors()[0]["type"] == "extra_forbidden"
