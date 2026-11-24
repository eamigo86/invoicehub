"""Shared pytest configuration: the Hypothesis profiles."""

import os

from hypothesis import settings

settings.register_profile("default", deadline=None)
settings.register_profile("ci", derandomize=True, deadline=None)
settings.load_profile("ci" if os.environ.get("CI") else "default")
