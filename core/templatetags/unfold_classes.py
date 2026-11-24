"""Template tag exposing Unfold's widget class lists to project templates."""

from django import template
from unfold import widgets  # type: ignore[import-untyped]

register = template.Library()

CLASS_LISTS: dict[str, list[str]] = {
    "input": widgets.INPUT_CLASSES,
    "textarea": widgets.TEXTAREA_CLASSES,
    "select": widgets.SELECT_CLASSES,
    "checkbox": widgets.CHECKBOX_CLASSES,
    "radio": widgets.RADIO_CLASSES,
    "label": widgets.LABEL_CLASSES,
    "checkbox_label": widgets.CHECKBOX_LABEL_CLASSES,
}


@register.simple_tag
def unfold_classes(kind: str) -> str:
    """Return the Unfold class list for a kind of form control.

    Args:
        kind: Name of the control kind: input, textarea, select, checkbox, radio, label or
            checkbox_label.

    Returns:
        The classes joined by single spaces.

    Raises:
        KeyError: When the kind is unknown, so a typo in a template fails loudly.
    """
    try:
        return " ".join(CLASS_LISTS[kind])
    except KeyError:
        raise KeyError(f"Unknown Unfold control kind: {kind}") from None
