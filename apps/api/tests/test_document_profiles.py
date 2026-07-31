from apps.api.documents import detect_document_type, get_chunking_config


def _detect(*, blocks, title="", headings=(), paragraphs=(), parser="markdown"):
    return detect_document_type(
        parser_type=parser,
        block_types=blocks,
        title=title,
        headings=headings,
        first_paragraphs=paragraphs,
    )


def test_ambiguous_document_falls_back_to_general():
    profile = _detect(
        blocks=["heading", "paragraph"],
        title="一些工作想法",
        paragraphs=["今天整理了模型和产品设计方面的想法。"],
    )
    assert profile.detected_type == "general"
    assert profile.evidence["fallback_reason"] == "low_confidence"


def test_contract_requires_multiple_specific_signals():
    profile = _detect(
        blocks=["heading", "paragraph", "paragraph"],
        title="服务协议",
        paragraphs=["甲方与乙方双方约定保密义务及违约责任。"],
    )
    assert profile.detected_type == "contract"
    assert profile.confidence >= 0.6


def test_code_profile_uses_larger_child_chunks():
    profile = _detect(
        blocks=["code_block", "code_block", "paragraph"],
        title="example.py",
    )
    assert profile.detected_type == "code"
    config = get_chunking_config(profile)
    assert config.child_target_max_chars == 1200
    assert config.child_hard_max_chars == 1800
    assert config.child_overlap_chars == 200


def test_table_profile_uses_rows_without_overlap():
    profile = _detect(
        blocks=["heading", "table", "table"],
        title="季度数据",
    )
    assert profile.detected_type == "table"
    config = get_chunking_config(profile)
    assert config.child_target_max_chars == 1800
    assert config.child_hard_max_chars == 2400
    assert config.child_overlap_chars == 0


def test_detection_is_deterministic():
    kwargs = {
        "blocks": ["heading", "table", "table"],
        "title": "季度数据",
        "headings": ["收入明细"],
    }
    assert _detect(**kwargs).to_dict() == _detect(**kwargs).to_dict()
