from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
import sys


def _load_real_module():
    current_dir = Path(__file__).resolve().parents[1]
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


_real_module = _load_real_module()
if _real_module is not None:
    globals().update(_real_module.__dict__)
else:
    class VARIANT:
        def __init__(self, vt, value):
            self.vt = vt
            self.value = value


    def Dispatch(*args, **kwargs):
        raise ImportError("win32com is unavailable on this platform")


    def GetActiveObject(*args, **kwargs):
        raise ImportError("win32com is unavailable on this platform")
