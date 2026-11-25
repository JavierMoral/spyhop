"""The mechanical pass of the custom-pr-reviewer agent: scan.sh and scan.pl."""

import subprocess
from pathlib import Path

from spyhop.config.paths import bundled_claude_dir

SCAN = bundled_claude_dir() / "scripts" / "custom-pr-reviewer" / "scan.sh"

JAVA_CLASS = "public class Foo {{\n\n\tpublic void foo() {{\n{body}\t}}\n\n}}\n"


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def commit(root: Path, files: dict[str, str]) -> str:
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)

    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", "Commit")

    return git(root, "rev-parse", "HEAD")


def scan(
    tmp_path: Path, head: dict[str, str], base: dict[str, str] | None = None
) -> list[tuple[str, str]]:
    """Commits ``base``, then ``head``, and returns the check and the location
    of each candidate that scan.sh prints for the diff."""

    root = tmp_path / "repo"

    root.mkdir()
    git(root, "init", "-q")

    base_sha = commit(root, base or {})

    commit(root, head)

    return run(root, base_sha, "HEAD")


def run(root: Path, *args: str) -> list[tuple[str, str]]:
    output = subprocess.run(
        ["bash", str(SCAN), *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout

    return [(line.split("\t")[0], line.split("\t")[1]) for line in output.splitlines()]


def java(body: str) -> str:
    """A class whose method body starts at line 4."""

    return JAVA_CLASS.format(body=body)


def test_inline_array_membership_with_two_elements(tmp_path: Path) -> None:
    body = (
        "\t\tif (ArrayUtil.contains(new int[] {1, 2}, type)) {\n"
        "\t\t\treturn;\n"
        "\t\t}\n"
        "\t\tif (ArrayUtil.contains(new int[] {1, 2, 3, 4}, type)) {\n"
        "\t\t\treturn;\n"
        "\t\t}\n"
    )

    assert scan(tmp_path, {"src/Foo.java": java(body)}) == [
        ("JAVA_INLINE_ARRAY_MEMBERSHIP", "src/Foo.java:4")
    ]


def test_inline_array_membership_through_a_collection_factory(tmp_path: Path) -> None:
    body = '\t\tboolean valid = List.of("a", "b").contains(value);\n'

    # The call on the result of List.of is also a chained call.

    assert scan(tmp_path, {"src/Foo.java": java(body)}) == [
        ("JAVA_INLINE_ARRAY_MEMBERSHIP", "src/Foo.java:4"),
        ("JAVA_CHAINED_CALL", "src/Foo.java:4"),
    ]


def test_only_added_lines_are_reported(tmp_path: Path) -> None:
    old = "\t\tif (ArrayUtil.contains(new int[] {1, 2}, type)) {\n\t\t\treturn;\n\t\t}\n"
    new = old + "\t\tint length = name.length();\n"

    assert scan(tmp_path, {"src/Foo.java": java(new)}, {"src/Foo.java": java(old)}) == []


def test_chained_call_is_reported_once_for_each_statement(tmp_path: Path) -> None:
    body = '\t\tint length = _execute("x").getJSONArray("usages").length();\n'

    assert scan(tmp_path, {"src/Foo.java": java(body)}) == [("JAVA_CHAINED_CALL", "src/Foo.java:4")]


def test_chained_call_in_the_multi_line_layout_of_the_formatter(tmp_path: Path) -> None:
    body = '\t\tJSONArray jsonArray = _execute(\n\t\t\t"x"\n\t\t).getJSONArray("usages");\n'

    assert scan(tmp_path, {"src/Foo.java": java(body)}) == [("JAVA_CHAINED_CALL", "src/Foo.java:6")]


def test_chained_call_skips_fluent_apis(tmp_path: Path) -> None:
    body = (
        '\t\tJSONObject jsonObject = JSONUtil.put("a", 1).put("b", 2);\n'
        "\t\tList<String> names = list.stream().map(Foo::name).toList();\n"
        '\t\tMockito.when(foo.bar()).thenReturn("x");\n'
        "\t\tString text = new StringBundler(2).append(a).toString();\n"
    )

    assert scan(tmp_path, {"src/Foo.java": java(body)}) == []


def test_receiver_calls_unsorted_across_a_setup_block(tmp_path: Path) -> None:
    body = (
        '\t\trequest.setParameter("a", "1");\n'
        "\n"
        "\t\tThemeDisplay themeDisplay = new ThemeDisplay();\n"
        "\n"
        '\t\trequest.setAttribute("b", themeDisplay);\n'
    )

    assert scan(tmp_path, {"src/FooTest.java": java(body)}) == [
        ("JAVA_RECEIVER_CALLS_UNSORTED", "src/FooTest.java:4")
    ]


def test_receiver_calls_sorted_or_in_two_methods(tmp_path: Path) -> None:
    sorted_body = (
        '\t\trequest.setAttribute("b", themeDisplay);\n\t\trequest.setParameter("a", "1");\n'
    )
    two_methods = (
        "public class Foo {\n\n"
        "\tpublic void foo() {\n"
        '\t\trequest.setParameter("a", "1");\n'
        "\t}\n\n"
        "\tpublic void bar() {\n"
        '\t\trequest.setAttribute("b", themeDisplay);\n'
        "\t}\n\n"
        "}\n"
    )

    assert scan(tmp_path, {"src/A.java": java(sorted_body), "src/B.java": two_methods}) == []


def test_wrapper_declared_after_the_value_it_receives(tmp_path: Path) -> None:
    body = (
        "\t\tLanguage language = Mockito.mock(Language.class);\n"
        "\n"
        "\t\tLanguageUtil languageUtil = new LanguageUtil();\n"
        "\n"
        "\t\tlanguageUtil.setLanguage(language);\n"
        "\n"
        "\t\tSAXReaderUtil saxReaderUtil = new SAXReaderUtil();\n"
        "\n"
        "\t\tSAXReaderImpl saxReaderImpl = new SAXReaderImpl();\n"
        "\n"
        "\t\tsaxReaderUtil.setSAXReader(saxReaderImpl);\n"
    )

    assert scan(tmp_path, {"src/FooTest.java": java(body)}) == [
        ("JAVA_WRAPPER_DECLARED_LATE", "src/FooTest.java:6")
    ]


def test_private_method_parameters_unsorted(tmp_path: Path) -> None:
    content = (
        "public class Foo {\n\n"
        "\tprivate void _addAddresses(\n"
        "\t\t\tLong accountId, Account account, AccountEntry accountEntry)\n"
        "\t\tthrows Exception {\n\t}\n\n"
        "\tprivate Map<String, Long> _getIds(\n"
        "\t\tMap<String, Long> idsMap, long groupId, String... names) {\n"
        "\t\treturn null;\n\t}\n\n"
        "\tpublic void update(long userId, Account account) {\n\t}\n\n"
        "}\n"
    )

    assert scan(tmp_path, {"src/Foo.java": content}) == [
        ("JAVA_PARAMETERS_UNSORTED", "src/Foo.java:3"),
        ("JAVA_PARAMETERS_UNSORTED", "src/Foo.java:8"),
    ]


def test_assertion_message(tmp_path: Path) -> None:
    body = (
        "\t\tAssert.assertTrue(\n"
        '\t\t\t"Expected no references, but found: " + elements,\n'
        "\t\t\telements.isEmpty());\n"
        '\t\tAssert.assertEquals("alpha", name);\n'
        "\t\tAssert.assertTrue(elements.isEmpty());\n"
    )

    assert scan(tmp_path, {"src/FooTest.java": java(body)}) == [
        ("JAVA_ASSERTION_MESSAGE", "src/FooTest.java:4")
    ]


def test_json_array_loop(tmp_path: Path) -> None:
    body = (
        "\t\tJSONArray jsonArray = JSONFactoryUtil.createJSONArray();\n"
        "\n"
        "\t\tfor (long id : ids) {\n"
        "\t\t\tjsonArray.put(\n"
        '\t\t\t\tJSONUtil.put("id", id));\n'
        "\t\t}\n"
        "\n"
        "\t\tJSONArray otherJSONArray = _jsonFactory.createJSONArray();\n"
        "\n"
        "\t\tfor (long id : ids) {\n"
        "\t\t\tif (id > 0) {\n"
        "\t\t\t\totherJSONArray.put(id);\n"
        "\t\t\t}\n"
        "\t\t}\n"
    )

    # The second loop filters, so JSONUtil.toJSONArray does not apply.

    assert scan(tmp_path, {"src/FooTest.java": java(body)}) == [
        ("JAVA_JSON_ARRAY_LOOP", "src/FooTest.java:4")
    ]


def test_shell_functions_unsorted(tmp_path: Path) -> None:
    old = "function main {\n\t:\n}\n\nfunction _set_up_gcp_gke {\n\t:\n}\n\nfunction _z {\n\t:\n}\n"
    new = old.replace(
        "function _z",
        "function _set_up_gcp_gitops {\n\t:\n}\n\nfunction _a {\n\t:\n}\n\nfunction _z",
    )

    # _set_up_gcp_gitops sorts before _set_up_gcp_gke, and _a before both.

    assert scan(tmp_path, {"setup.sh": new}, {"setup.sh": old}) == [
        ("SHELL_FUNCTIONS_UNSORTED", "setup.sh:9"),
        ("SHELL_FUNCTIONS_UNSORTED", "setup.sh:13"),
    ]


def test_type_assertion_and_conditional_spread_in_typescript(tmp_path: Path) -> None:
    content = (
        "const name = value as string;\n"
        "const flags = ['a'] as const;\n"
        "const items = [...(enabled ? [] : [item])];\n"
    )

    assert scan(tmp_path, {"src/foo.ts": content, "src/foo.test.ts": content}) == [
        ("TS_TYPE_ASSERTION", "src/foo.ts:1"),
        ("TS_CONDITIONAL_SPREAD", "src/foo.ts:3"),
    ]


def test_label_change_outside_the_feature_flag_gate(tmp_path: Path) -> None:
    gate = java('\t\tif (FeatureFlagManagerUtil.isEnabled("LPD-1")) {\n\t\t\treturn;\n\t\t}\n')
    old = java('\t\tString label = LanguageUtil.get(request, "connect-sites");\n')
    new = java('\t\tString label = LanguageUtil.get(request, "connect");\n')

    assert scan(
        tmp_path, {"src/Gate.java": gate, "src/View.java": new}, {"src/View.java": old}
    ) == [("UNGATED_LABEL_CHANGE", "src/View.java:4")]


def test_label_change_without_a_feature_flag_in_the_diff(tmp_path: Path) -> None:
    old = java('\t\tString label = LanguageUtil.get(request, "connect-sites");\n')
    new = java('\t\tString label = LanguageUtil.get(request, "connect");\n')

    assert scan(tmp_path, {"src/View.java": new}, {"src/View.java": old}) == []


def test_generated_files_are_skipped(tmp_path: Path) -> None:
    body = "\t\tif (ArrayUtil.contains(new int[] {1, 2}, type)) {\n\t\t\treturn;\n\t\t}\n"

    assert scan(tmp_path, {"src/Foo.java": "// @generated\n" + java(body)}) == []


def test_worktree_scans_the_uncommitted_changes(tmp_path: Path) -> None:
    root = tmp_path / "repo"

    root.mkdir()
    git(root, "init", "-q")

    base = commit(root, {"src/Foo.java": java("")})

    (root / "src" / "Foo.java").write_text(
        java("\t\tif (ArrayUtil.contains(new int[] {1, 2}, type)) {\n\t\t\treturn;\n\t\t}\n")
    )

    assert run(root, base, "HEAD") == []
    assert run(root, base, "WORKTREE") == [("JAVA_INLINE_ARRAY_MEMBERSHIP", "src/Foo.java:4")]


def test_precedent_counts_only_production_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    line = "\t\tArrayUtil.contains(new int[] {1, 2}, type);\n"

    root.mkdir()
    git(root, "init", "-q")
    commit(
        root,
        {
            "a/src/main/java/A.java": java(line),
            "a/src/test/java/ATest.java": java(line),
            "a/src/main/java/B.java": java(""),
        },
    )

    output = subprocess.run(
        ["bash", str(SCAN), "precedent", r"ArrayUtil\.contains\(new int"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert output == "1 production files match\n  a/src/main/java/A.java\n"


def test_boolean_method_without_is_or_has(tmp_path: Path) -> None:
    content = (
        "public class Foo {\n\n"
        "\t@Input\n\tpublic boolean getEnabled() {\n\t\treturn true;\n\t}\n\n"
        "\tprivate boolean _existsPortlet(long plid) {\n\t\treturn true;\n\t}\n\n"
        "\tprivate boolean _hasPortlet(long plid) {\n\t\treturn true;\n\t}\n\n"
        "\tpublic boolean isEnabled() {\n\t\treturn true;\n\t}\n\n"
        '\t@Override\n\t@SuppressWarnings("x")\n\tpublic boolean equals(Object object) {\n'
        "\t\treturn true;\n\t}\n\n"
        "}\n"
    )

    assert scan(tmp_path, {"src/Foo.java": content}) == [
        ("JAVA_BOOLEAN_METHOD_NAME", "src/Foo.java:4"),
        ("JAVA_BOOLEAN_METHOD_NAME", "src/Foo.java:8"),
    ]


def test_message_without_verb(tmp_path: Path) -> None:
    body = (
        '\t\tthrow new Exception("Error processing GCS file", exception);\n'
        '\t\t_log.error("Cannot delete " + name);\n'
        '\t\t_log.warn("Operation not allowed for client " + clientId);\n'
        '\t\t_log.info("Resource created successfully");\n'
        '\t\t_log.debug("No entries found");\n'
        '\t\t_log.error("Unable to process GCS file");\n'
        '\t\t_log.warn("Operation is not allowed");\n'
        '\t\t_log.debug("No entries were found");\n'
    )

    assert scan(tmp_path, {"src/Foo.java": java(body), "src/test/FooTest.java": java(body)}) == [
        ("MESSAGE_WITHOUT_VERB", "src/Foo.java:4"),
        ("MESSAGE_WITHOUT_VERB", "src/Foo.java:5"),
        ("MESSAGE_WITHOUT_VERB", "src/Foo.java:6"),
        ("MESSAGE_WITHOUT_VERB", "src/Foo.java:7"),
        ("MESSAGE_WITHOUT_VERB", "src/Foo.java:8"),
    ]


def test_message_without_verb_in_language_values_and_shell_output(tmp_path: Path) -> None:
    files = {
        "content/Language.properties": (
            "entries-not-found=Entries not found.\nentries-were-not-found=Entries were not found.\n"
        ),
        "scripts/run.sh": "echo 'Failed to start the server'\necho \"Server is not running\"\n",
        "portal.properties": "message=Value not found\n",
    }

    assert scan(tmp_path, files) == [
        ("MESSAGE_WITHOUT_VERB", "content/Language.properties:1"),
        ("MESSAGE_WITHOUT_VERB", "scripts/run.sh:1"),
    ]


def test_osgi_configuration_and_openapi_files_are_skipped(tmp_path: Path) -> None:
    files = {
        "src/FooConfiguration.java": (
            '@Meta.OCD(id = "foo")\npublic interface FooConfiguration {\n\n'
            "\t@Meta.AD(required = false)\n\tpublic boolean enabled();\n\n}\n"
        ),
        "rest-openapi.yaml": 'description: "Items not found in the cart"\n',
        ".github/workflows/build.yml": "run: echo 'Failed to build'\n",
    }

    assert scan(tmp_path, files) == [("MESSAGE_WITHOUT_VERB", ".github/workflows/build.yml:1")]
