from typing import Any

from spyhop.infrastructure.jira.adf import adf_to_text

Node = dict[str, Any]


def doc(*content: Node) -> Node:
    return {"type": "doc", "version": 1, "content": list(content)}


def paragraph(*content: Node) -> Node:
    return {"type": "paragraph", "content": list(content)}


def text(value: str, *marks: str, href: str | None = None) -> Node:
    node: Node = {"type": "text", "text": value}
    node_marks: list[Node] = [{"type": mark} for mark in marks]

    if href:
        node_marks.append({"type": "link", "attrs": {"href": href}})

    if node_marks:
        node["marks"] = node_marks

    return node


def test_empty_values() -> None:
    assert adf_to_text(None) == ""
    assert adf_to_text({"type": "doc", "content": []}) == ""
    assert adf_to_text("  plain v2 text  ") == "plain v2 text"


def test_paragraphs_marks_and_links() -> None:
    result = adf_to_text(
        doc(
            paragraph(text("Steps", "strong"), text(" with "), text("code()", "code")),
            paragraph(
                text("see "), text("ticket", href="https://x/1"), {"type": "hardBreak"}, text("ok")
            ),
        )
    )

    assert result == "**Steps** with `code()`\n\nsee [ticket](https://x/1)\nok"


def test_nested_ordered_list() -> None:
    result = adf_to_text(
        doc(
            {
                "type": "orderedList",
                "attrs": {"order": 1},
                "content": [
                    {"type": "listItem", "content": [paragraph(text("Create"))]},
                    {
                        "type": "listItem",
                        "content": [
                            paragraph(text("From the company")),
                            {
                                "type": "bulletList",
                                "content": [
                                    {"type": "listItem", "content": [paragraph(text("Export"))]}
                                ],
                            },
                        ],
                    },
                ],
            }
        )
    )

    assert result == "1. Create\n2. From the company\n   - Export"


def test_code_block_heading_and_unknown_nodes() -> None:
    result = adf_to_text(
        doc(
            {"type": "heading", "attrs": {"level": 2}, "content": [text("Logs")]},
            {
                "type": "codeBlock",
                "attrs": {"language": "java"},
                "content": [text("throw new X();")],
            },
            {"type": "somethingNew", "content": [text("still readable")]},
        )
    )

    assert result == "## Logs\n\n```java\nthrow new X();\n```\n\nstill readable"
