from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING

from . import common as common_alias
from .common import normalize

if TYPE_CHECKING:
    from .models import Overloaded  # noqa: F401

if os.name == "nt":
    from .platform_win import platform_name
else:
    from .platform_posix import platform_name  # noqa: F401


def load_optional(module_name: str):
    return importlib.import_module(module_name)


def use_imports(value: str) -> str:
    return common_alias.normalize(normalize(value))
