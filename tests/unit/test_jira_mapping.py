from typing import Any

from spyhop.infrastructure.jira.mapping import ticket_text


def doc(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": list(content)}


def paragraph(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def item(text: str) -> dict[str, Any]:
    return {"type": "listItem", "content": [paragraph(text)]}


def cell(text: str) -> dict[str, Any]:
    return {"type": "tableCell", "content": [paragraph(text)]}


def test_ticket_text_gives_each_field_with_a_value_by_its_name() -> None:
    issue = {
        "key": "LPD-1",
        "names": {
            "customfield_1": "Steps to Reproduce",
            "customfield_2": "Environments Information",
            "versions": "Affects versions",
            "description": "Description",
        },
        "fields": {
            "summary": "Broken page",
            "customfield_1": doc(
                {"type": "orderedList", "content": [item("Log in."), item("Open the page.")]}
            ),
            "customfield_2": doc(
                {"type": "table", "content": [{"type": "tableRow", "content": [cell("master")]}]}
            ),
            "versions": [{"id": "1", "name": "Master"}],
            "watches": {"watchCount": 1},
            "customfield_3": None,
            "description": doc(
                {
                    "type": "heading",
                    "attrs": {"level": 1},
                    "content": [{"type": "text", "text": "Bug"}],
                },
                {"type": "bulletList", "content": [item("One"), item("Two")]},
            ),
            "comment": {
                "total": 3,
                "comments": [
                    {
                        "author": {"displayName": "Ana"},
                        "created": "2026-11-20T10:00:00.000+0100",
                        "body": doc(paragraph("It fails on master too.")),
                    }
                ],
            },
        },
    }

    assert ticket_text(issue) == (
        "# LPD-1: Broken page\n"
        "\n## Description\n\n### Bug\n- One\n- Two\n"
        "\n## Steps to Reproduce\n\n1. Log in.\n2. Open the page.\n"
        "\n## Environments Information\n\n| master |\n"
        "\n## Affects versions\n\nMaster\n"
        "\n## Comments\n"
        "\nJIRA gave only 1 of the 3 comments.\n"
        "\n### Ana, 2026-11-20T10:00:00.000+0100\n\nIt fails on master too.\n"
    )
