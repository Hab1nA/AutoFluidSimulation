from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
import sys


def _load_real_package():
    current_dir = Path(__file__).resolve().parent.parent
    search_paths = [
        path for path in sys.path
        if Path(path or ".").resolve() != current_dir
    ]
    spec = importlib.machinery.PathFinder.find_spec(__name__, search_paths)
    if spec is None or spec.loader is None or getattr(spec, "origin", None) == __file__:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_real_package = _load_real_package()
if _real_package is not None:
    globals().update(_real_package.__dict__)
