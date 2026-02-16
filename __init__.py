# your_plugin/__init__.py
"""
Top-level plugin package for the Docker manager plugin.
"""

import importlib
import sys
from typing import Optional

# package version
__version__ = "0.1.0"

# import subpackages so they are attributes of the package
from . import core 
from . import utils 
from . import routes 

# delegate plugin lifecycle to routes.admin
# this keeps the "public" load/unload at package root (CTFd expects that)
try:
    from .routes.admin import load as _routes_load, unload as _routes_unload
except Exception:
    # Keep import-time failure visible but don't crash import
    _routes_load = None
    _routes_unload = None



def load(app):
    """Entry point used by CTFd to enable the plugin."""
    if _routes_load is None:
        raise RuntimeError("Plugin loader not available: failed to import routes.admin")
    return _routes_load(app)



def unload(app):
    """Optional unload entrypoint - delegates to routes.admin.unload."""
    if _routes_unload is None:
        return None
    return _routes_unload(app)
