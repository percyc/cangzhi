"""Read a final-text view without saving or accepting revisions in the source."""


def final_text_view(body):
    from docx.oxml.ns import qn

    included = {qn("w:ins"), qn("w:moveTo")}
    excluded = {qn("w:del"), qn("w:moveFrom")}
    counts = {"included": 0, "excluded": 0}
    # A deleted row carries its marker in trPr rather than around its text.
    for row in list(body.iter(qn("w:tr"))):
        properties = row.find(qn("w:trPr"))
        if properties is not None and properties.find(qn("w:del")) is not None:
            row.getparent().remove(row)
            counts["excluded"] += 1
    # Bottom-up, only in memory. Property markers are not content wrappers.
    for element in reversed(list(body.iter())):
        parent = element.getparent()
        if parent is None:
            continue
        if element.tag in included | excluded:
            if parent.tag.endswith("Pr") or parent.tag.endswith("PrChange"):
                continue
            if element.tag in excluded:
                counts["excluded"] += 1
                parent.remove(element)
            else:
                counts["included"] += 1
                position = parent.index(element)
                for child in list(element):
                    parent.insert(position, child)
                    position += 1
                parent.remove(element)
    return {"view": "final_text", **counts,
            "scope": "text_insert_delete_move",
            "limitations": "不接受原件修订；复杂表格结构、段落合并及格式修订不保证还原。"}
