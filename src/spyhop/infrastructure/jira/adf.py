"""Converts Atlassian Document Format (ADF) to plain Markdown-like text.

Only the structure an agent needs survives: paragraphs, headings, lists, code,
quotes, tables and links. Unknown nodes fall back to their text content, so a
new node type degrades instead of disappearing.
"""

from typing import Any

_BLOCK_SEPARATOR = "\n\n"


def adf_to_text(document: Any) -> str:
    """Accepts an ADF document, a plain string (v2-style fields) or None."""

    if document is None:
        return ""

    if isinstance(document, str):
        return document.strip()

    if not isinstance(document, dict):
        return ""

    return _blocks(document.get("content", [])).strip()


def _blocks(nodes: list[Any], indent: str = "") -> str:
    rendered = [_block(node, indent) for node in nodes if isinstance(node, dict)]

    return _BLOCK_SEPARATOR.join(text for text in rendered if text.strip())


def _block(node: dict[str, Any], indent: str) -> str:
    kind = node.get("type")
    content = node.get("content", [])

    if kind == "paragraph":
        return indent + _inline(content).replace("\n", "\n" + indent)

    if kind == "heading":
        level = int(node.get("attrs", {}).get("level", 1))

        return indent + "#" * min(max(level, 1), 6) + " " + _inline(content)

    if kind in ("bulletList", "orderedList"):
        return _list(node, indent)

    if kind == "codeBlock":
        language = node.get("attrs", {}).get("language") or ""
        code = "".join(child.get("text", "") for child in content if isinstance(child, dict))

        return f"{indent}```{language}\n{code}\n{indent}```"

    if kind == "blockquote":
        return "\n".join(f"{indent}> {line}" for line in _blocks(content).splitlines())

    if kind == "rule":
        return indent + "---"

    if kind in ("panel", "expand", "nestedExpand", "layoutSection", "layoutColumn"):
        title = node.get("attrs", {}).get("title")
        body = _blocks(content, indent)

        return f"{indent}**{title}**\n\n{body}" if title else body

    if kind == "table":
        return _table(node, indent)

    if kind in ("mediaSingle", "mediaGroup", "media"):
        return indent + "[attachment]"

    return indent + _inline(content) if content else ""


def _list(node: dict[str, Any], indent: str) -> str:
    ordered = node.get("type") == "orderedList"
    start = int(node.get("attrs", {}).get("order", 1) or 1)
    lines = []

    for index, item in enumerate(node.get("content", [])):
        marker = f"{start + index}." if ordered else "-"
        children = item.get("content", []) if isinstance(item, dict) else []
        first, *rest = children or [{}]
        text = _block(first, "") if first else ""
        nested = _blocks(rest, indent + "   ") if rest else ""

        lines.append(f"{indent}{marker} {text}" + (f"\n{nested}" if nested else ""))

    return "\n".join(lines)


def _table(node: dict[str, Any], indent: str) -> str:
    rows = []

    for row in node.get("content", []):
        cells = [
            _blocks(cell.get("content", [])).replace("\n", " ")
            for cell in row.get("content", [])
            if isinstance(cell, dict)
        ]

        rows.append(f"{indent}| " + " | ".join(cells) + " |")

    return "\n".join(rows)


def _inline(nodes: list[Any]) -> str:
    parts = []

    for node in nodes:
        if not isinstance(node, dict):
            continue

        kind = node.get("type")
        attrs = node.get("attrs", {})

        if kind == "text":
            parts.append(_marked(node.get("text", ""), node.get("marks", [])))
        elif kind == "hardBreak":
            parts.append("\n")
        elif kind == "mention":
            parts.append(str(attrs.get("text") or "@someone"))
        elif kind == "emoji":
            parts.append(str(attrs.get("text") or attrs.get("shortName") or ""))
        elif kind == "inlineCard":
            parts.append(str(attrs.get("url", "")))
        elif kind == "status":
            parts.append(f"[{attrs.get('text', '')}]")
        elif kind == "date":
            parts.append(str(attrs.get("timestamp", "")))
        else:
            parts.append(_inline(node.get("content", [])))

    return "".join(parts)


def _marked(text: str, marks: list[Any]) -> str:
    for mark in marks:
        kind = mark.get("type") if isinstance(mark, dict) else None

        if kind == "code":
            text = f"`{text}`"
        elif kind == "strong":
            text = f"**{text}**"
        elif kind == "em":
            text = f"*{text}*"
        elif kind == "link":
            href = mark.get("attrs", {}).get("href")

            if href and href != text:
                text = f"[{text}]({href})"

    return text
