"""Contract tests: the committed schema, the breaking change check and the documented example."""

from io import StringIO
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command

from api.contract import breaking_changes, dangerous_changes

OLD = """
type Query {
  kept: Int
  gone: Int @deprecated(reason: "use kept")
  loud(old: Int @deprecated(reason: "unused"), other: Int): Int
  status: Status
  count: Int
}

enum Status {
  OPEN
  LEGACY @deprecated(reason: "use OPEN")
  CLOSED
}
"""


def edit(*pairs: tuple[str, str]) -> str:
    """Build a new schema from the old one by textual replacement.

    Args:
        *pairs: Pairs of the fragment to replace and its replacement.

    Returns:
        The edited schema text.
    """
    text = OLD
    for before, after in pairs:
        assert before in text
        text = text.replace(before, after)
    return text


def test_an_identical_schema_has_no_breaking_changes() -> None:
    """Comparing a schema with itself reports nothing."""
    assert breaking_changes(OLD, OLD) == []


def test_removing_a_deprecated_field_is_allowed() -> None:
    """A field that carried a deprecation in the old schema may be removed."""
    assert breaking_changes(OLD, edit(('  gone: Int @deprecated(reason: "use kept")\n', ""))) == []


def test_removing_a_field_that_was_not_deprecated_is_breaking() -> None:
    """A live field cannot disappear without a deprecation first."""
    result = breaking_changes(OLD, edit(("  kept: Int\n", "")))
    assert result == ["Field Query.kept was removed."]


def test_removing_a_deprecated_enum_value_is_allowed() -> None:
    """A deprecated enum value may be removed."""
    new = edit(('  LEGACY @deprecated(reason: "use OPEN")\n', ""))
    assert breaking_changes(OLD, new) == []


def test_removing_a_live_enum_value_is_breaking() -> None:
    """An enum value without a deprecation cannot be removed."""
    result = breaking_changes(OLD, edit(("  CLOSED\n", "")))
    assert result == ["Enum value Status.CLOSED was removed."]


def test_removing_a_deprecated_argument_is_allowed() -> None:
    """A deprecated argument may be removed while its field stays."""
    new = edit(('old: Int @deprecated(reason: "unused"), ', ""))
    assert breaking_changes(OLD, new) == []


def test_removing_a_live_argument_is_breaking() -> None:
    """An argument without a deprecation cannot be removed."""
    result = breaking_changes(OLD, edit((", other: Int", "")))
    assert result == ["Argument Query.loud(other:) was removed."]


def test_a_type_change_is_always_breaking() -> None:
    """Changing a field type fails even though the field was kept."""
    result = breaking_changes(OLD, edit(("count: Int", "count: String")))
    assert result == ["Field Query.count changed type from Int to String."]


def test_removing_a_type_is_breaking() -> None:
    """A removed enum fails: both the type and its uses are reported."""
    result = breaking_changes(
        OLD, edit(("  status: Status\n", ""), ("enum Status {", "enum Other {"))
    )
    assert "Status was removed." in result


def test_additions_are_not_breaking_but_enum_additions_are_dangerous() -> None:
    """New fields pass silently; a new enum value is reported as dangerous."""
    new = edit(
        ("  count: Int\n", "  count: Int\n  extra: Int\n"), ("  CLOSED\n", "  CLOSED\n  NEW\n")
    )
    assert breaking_changes(OLD, new) == []
    assert dangerous_changes(OLD, new) == ["Enum value Status.NEW was added."]


ROOT = Path(__file__).resolve().parents[1]


def export_schema(path: Path) -> str:
    """Export the SDL the way make schema does: the command writes the file, then a newline.

    Args:
        path: Destination file for the export.

    Returns:
        The exported SDL, ending in a newline.
    """
    call_command("graphql_schema", out=str(path), stdout=StringIO())
    return path.read_text(encoding="utf-8") + "\n"


def test_the_export_is_deterministic(tmp_path: Path) -> None:
    """Two exports of the same schema are byte for byte equal.

    Args:
        tmp_path: Pytest temporary directory.
    """
    assert export_schema(tmp_path / "a.graphql") == export_schema(tmp_path / "b.graphql")


def test_the_committed_schema_is_fresh(tmp_path: Path) -> None:
    """schema.graphql matches the schema the code generates.

    Args:
        tmp_path: Pytest temporary directory.
    """
    committed = (ROOT / "schema.graphql").read_text(encoding="utf-8")
    assert export_schema(tmp_path / "schema.graphql") == committed, (
        "schema.graphql is stale: run make schema and commit it"
    )


def run_contract(tmp_path: Path, base: str) -> tuple[str, str]:
    """Run check_schema_contract against a base schema text.

    Args:
        tmp_path: Directory where the base file is written.
        base: SDL of the base schema.

    Returns:
        The standard output and the standard error of the command.
    """
    base_file = tmp_path / "base.graphql"
    base_file.write_text(base, encoding="utf-8")
    out, err = StringIO(), StringIO()
    call_command("check_schema_contract", base=str(base_file), stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


def test_the_command_passes_against_the_same_schema(tmp_path: Path) -> None:
    """A base equal to the current schema is compatible.

    Args:
        tmp_path: Pytest temporary directory.
    """
    out, _ = run_contract(tmp_path, export_schema(tmp_path / "current.graphql"))
    assert "compatible" in out


def test_the_command_fails_and_prints_each_breaking_change(tmp_path: Path) -> None:
    """A field of the base that the code no longer has fails the command.

    Args:
        tmp_path: Pytest temporary directory.
    """
    base = export_schema(tmp_path / "current.graphql").replace(
        "type Query {", "type Query {\n  legacy: Int\n  old: Int"
    )
    base_file = tmp_path / "base.graphql"
    base_file.write_text(base, encoding="utf-8")
    err = StringIO()
    with pytest.raises(CommandError, match="2 breaking changes"):
        call_command("check_schema_contract", base=str(base_file), stderr=err)
    assert "breaking: Field Query.legacy was removed." in err.getvalue()
    assert "breaking: Field Query.old was removed." in err.getvalue()


def test_the_command_allows_removing_what_the_base_deprecated(tmp_path: Path) -> None:
    """A base field that was deprecated may be gone in the current schema.

    Args:
        tmp_path: Pytest temporary directory.
    """
    base = export_schema(tmp_path / "current.graphql").replace(
        "type Query {", 'type Query {\n  legacy: Int @deprecated(reason: "use invoices")'
    )
    out, _ = run_contract(tmp_path, base)
    assert "compatible" in out


def test_the_command_prints_dangerous_changes_without_failing(tmp_path: Path) -> None:
    """An enum value added since the base is reported and does not fail.

    Args:
        tmp_path: Pytest temporary directory.
    """
    base = export_schema(tmp_path / "current.graphql").replace(
        '  """Credit Note"""\n  CREDIT_NOTE\n', ""
    )
    out, _ = run_contract(tmp_path, base)
    assert "Enum value billingInvoiceKindEnum.CREDIT_NOTE was added." in out
    assert "compatible" in out
