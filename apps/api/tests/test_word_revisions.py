from io import BytesIO

from docx import Document
from docx.oxml import OxmlElement
from apps.api.parsers.docx import DocxParser


def wrap(element, kind):
    parent = element.getparent()
    wrapper = OxmlElement('w:' + kind)
    parent.replace(element, wrapper)
    wrapper.append(element)
    return wrapper


def parse(document):
    output = BytesIO()
    document.save(output)
    original = output.getvalue()
    result = DocxParser().parse(original)
    assert result.success
    assert output.getvalue() == original
    return result.structured_content


def test_insert_delete_and_move_keep_final_order():
    doc = Document()
    p = doc.add_paragraph('起点')
    wrap(p.add_run('旧内容')._r, 'del')
    wrap(p.add_run('新内容')._r, 'ins')
    wrap(p.add_run('移动内容')._r, 'moveFrom')
    p.add_run('终点')
    wrap(p.add_run('移动内容')._r, 'moveTo')
    result = parse(doc)
    assert result.full_text() == '起点新内容终点移动内容'
    assert result.metadata['docx_extraction']['revision_view']['excluded'] == 2
    # Parsing does not mutate the caller's original document.
    assert len(doc.element.body.xpath('.//w:del')) == 1


def test_body_wrappers_and_table_cells():
    doc = Document()
    wrap(doc.add_paragraph('删除段落')._p, 'del')
    wrap(doc.add_heading('保留标题', 1)._p, 'ins')
    cell = doc.add_table(rows=1, cols=1).cell(0, 0)
    wrap(cell.paragraphs[0].add_run('旧值')._r, 'del')
    wrap(cell.paragraphs[0].add_run('新值')._r, 'ins')
    result = parse(doc)
    assert [b.text for b in result.blocks] == ['保留标题', '新值']
    assert result.blocks[1].heading_path == ['保留标题']


def test_nested_insertions_inside_deletion_never_escape():
    doc = Document()
    p = doc.add_paragraph('保留')
    wrapper = wrap(p.add_run('不能泄漏')._r, 'ins')
    wrap(wrapper, 'del')
    assert parse(doc).full_text() == '保留'


def test_all_inserted_text_is_not_empty():
    doc = Document()
    wrap(doc.add_paragraph().add_run('整篇修订正文')._r, 'ins')
    assert parse(doc).full_text() == '整篇修订正文'


def test_all_deleted_text_stays_empty():
    doc = Document()
    wrap(doc.add_paragraph().add_run('已删除')._r, 'del')
    assert parse(doc).full_text().strip() == ''


def test_deleted_table_row_is_excluded():
    doc = Document()
    table = doc.add_table(rows=2, cols=1)
    table.cell(0, 0).text = '删除行'
    table.cell(1, 0).text = '保留行'
    table.rows[0]._tr.get_or_add_trPr().append(OxmlElement('w:del'))
    assert parse(doc).full_text() == '保留行'
