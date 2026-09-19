from types import SimpleNamespace

import pytest

from apps.api.services.evidence_selection import _features, choose_evidence_row


def row(key, content, *, document=1, version=1):
    return SimpleNamespace(chunk_id=key, document_id=document,
        document_version_id=version, content=content, rank=999)


@pytest.mark.parametrize('query,title,distractor,answer', [
    ('设备使用手册的保修期限多久', '设备使用手册', '设备使用手册安装方法', '保修期限为三年'),
    ('Service manual renewal requirements', 'Service manual', 'Service manual installation', 'Renewal requires written approval'),
    ('试验规范中医疗设备的检测周期', '试验规范', '试验规范的编制要求', '医疗设备的检测周期为一年'),
    ('手册中ＡＢＣ型号校准要求', '手册', '手册介绍其他型号', 'ABC 型号校准要求为每年一次'),
])
def test_focus_not_title_selects_body(query, title, distractor, answer):
    a, b = row(1, distractor * 8), row(2, answer)
    assert choose_evidence_row(query, title, [a, b], [b, a]) is b
    assert b.content == answer


def test_title_only_and_no_match_keep_anchor():
    a, b = row(9, 'manual index'), row(2, 'manual index other details')
    assert choose_evidence_row('manual', 'manual', [a,b], [b]) is a
    assert choose_evidence_row('unrelated', 'manual', [a,b], [b]) is a


def test_ties_keep_anchor_and_duplicate_rows_do_not_add_votes():
    a, b = row(9, 'renewal policy'), row(2, 'renewal policy')
    assert choose_evidence_row('renewal', 'guide', [a,b,b], [b,b,b]) is a


def test_same_document_and_version_only():
    a=row(1,'overview')
    outsiders=[row(2,'renewal policy',document=2),row(3,'renewal policy',version=2)]
    assert choose_evidence_row('renewal', 'guide', [a]+outsiders, outsiders) is a


def test_candidate_bound_and_empty():
    rows=[row(i, 'overview') for i in range(8)] + [row(100,'renewal')]
    assert choose_evidence_row('renewal','guide',rows,[]) is rows[0]
    assert choose_evidence_row('renewal','guide',[],[]) is None
    assert choose_evidence_row('renewal','guide',[],[rows[0]]) is rows[0]


def test_feature_budget_preserves_question_tail():
    query=''.join(chr(0x4e00+i) for i in range(250))+'尾部焦点'
    result=_features(query)
    assert len(result)<=128
    assert result[-1]=='焦点'


def test_body_bound_does_not_load_unlimited_text():
    a,b=row(1,'overview'),row(2,'x'*16000+'renewal')
    assert choose_evidence_row('renewal','guide',[a,b],[]) is a


def test_spaced_identifier_does_not_reward_unrelated_references():
    a=row(1,'equipment applicability scope')
    b=row(2,'equipment applicability scope ISO 1234 ISO 5678')
    assert _features('ISO 9001 scope') == ['iso9001', 'scope']
    assert choose_evidence_row('ISO 9001 equipment applicability scope','guide',[a,b],[]) is a


def test_english_tokens_require_word_boundaries():
    a,b=row(1,'overview'),row(2,'corporate governance')
    assert choose_evidence_row('rate','guide',[a,b],[]) is a
    a,b=row(1,'overview'),row(2,'some thing')
    assert choose_evidence_row('something','guide',[a,b],[]) is a
