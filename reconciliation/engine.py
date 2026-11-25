"""The one module that wraps remitmatch, so an API change in the alpha touches a single file."""

import remitmatch


def engine_version() -> str:
    """Return the version of the installed matching engine.

    Returns:
        The remitmatch version string.
    """
    return remitmatch.__version__


def default_config() -> dict[str, object]:
    """Return the engine default match configuration as plain JSON values.

    Returns:
        The tolerance, window, group size, thresholds, margin and signal weights.
    """
    config = remitmatch.MatchConfig()
    return {
        "amount_tolerance_minor": config.amount_tolerance_minor,
        "date_window_days": config.date_window_days,
        "max_group_size": config.max_group_size,
        "auto_threshold": config.auto_threshold,
        "suggest_threshold": config.suggest_threshold,
        "ambiguity_margin": config.ambiguity_margin,
        "weights": config.weights,
    }
