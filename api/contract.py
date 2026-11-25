"""Schema contract: which changes between two versions of the SDL break API consumers."""

import re

from graphql import (
    BreakingChangeType,
    GraphQLEnumType,
    GraphQLSchema,
    build_schema,
    find_breaking_changes,
    find_dangerous_changes,
)

_FIELD_REMOVED = re.compile(r"^Field (\w+)\.(\w+) was removed\.$")
_ARG_REMOVED = re.compile(r"^Argument (\w+)\.(\w+)\((\w+):\) was removed\.$")
_VALUE_REMOVED = re.compile(r"^Enum value (\w+)\.(\w+) was removed\.$")


def _was_deprecated(old: GraphQLSchema, change_type: BreakingChangeType, description: str) -> bool:
    """Tell whether a removal in a breaking change targets an element deprecated in the old schema.

    Args:
        old: The previous schema.
        change_type: Kind of breaking change reported by graphql-core.
        description: Description of the change, which names the removed element.

    Returns:
        True only for the removal of a field, argument or enum value that had a deprecation reason.
    """
    try:
        if change_type is BreakingChangeType.FIELD_REMOVED:
            type_name, field = _FIELD_REMOVED.match(description).groups()  # type: ignore[union-attr]
            return bool(old.type_map[type_name].fields[field].deprecation_reason)  # type: ignore[attr-defined]
        if change_type is BreakingChangeType.ARG_REMOVED:
            type_name, field, arg = _ARG_REMOVED.match(description).groups()  # type: ignore[union-attr]
            field_def = old.type_map[type_name].fields[field]  # type: ignore[attr-defined]
            return bool(field_def.args[arg].deprecation_reason)
        if change_type is BreakingChangeType.VALUE_REMOVED_FROM_ENUM:
            type_name, value = _VALUE_REMOVED.match(description).groups()  # type: ignore[union-attr]
            enum = old.type_map[type_name]
            assert isinstance(enum, GraphQLEnumType)
            return bool(enum.values[value].deprecation_reason)
    except (AttributeError, KeyError):
        return False
    return False


def breaking_changes(old_sdl: str, new_sdl: str) -> list[str]:
    """List the changes that break consumers of the old schema.

    Removing a field, argument or enum value that the old schema already deprecated is allowed;
    every other breaking change is reported.

    Args:
        old_sdl: SDL of the previous version of the schema.
        new_sdl: SDL of the new version of the schema.

    Returns:
        One description per breaking change, empty when the new schema is compatible.
    """
    old = build_schema(old_sdl)
    changes = find_breaking_changes(old, build_schema(new_sdl))
    return [c.description for c in changes if not _was_deprecated(old, c.type, c.description)]


def dangerous_changes(old_sdl: str, new_sdl: str) -> list[str]:
    """List the changes that may affect consumers without breaking them.

    Args:
        old_sdl: SDL of the previous version of the schema.
        new_sdl: SDL of the new version of the schema.

    Returns:
        One description per dangerous change.
    """
    changes = find_dangerous_changes(build_schema(old_sdl), build_schema(new_sdl))
    return [c.description for c in changes]
