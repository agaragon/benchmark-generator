"""Built-in checks. Importing this package registers them."""

from . import architecture, business, performance  # noqa: F401  (registration side effects)
from .base import (CATEGORIES, REGISTRY, SNAPSHOT_TAG, Change, Check, CheckContext, CheckResult,
                   CommandResult, build_check, compute_changes, load_plugins, register)

__all__ = ["CATEGORIES", "REGISTRY", "SNAPSHOT_TAG", "Change", "Check", "CheckContext", "CheckResult",
           "CommandResult", "build_check", "compute_changes", "load_plugins", "register"]
