# your_plugin/__init__.py
"""
Top-level plugin package for the Docker manager plugin.
"""

import importlib
import sys
from typing import Optional

from CTFd.plugins import register_plugin_assets_directory

# package version
__version__ = "0.1.0"

# import subpackages so they are attributes of the package
from . import core 
from . import utils 
from . import routes 

# delegate plugin lifecycle to routes.admin
# this keeps the "public" load/unload at package root (CTFd expects that)
#try:
from .routes.admin import load as routes_load, unload as routes_unload
from .models.challenges import load as challenges_load
#except Exception:
    # Keep import-time failure visible but don't crash import
 #   routes_load = None
  #  routes_unload = None



def load(app):
    """Entry point used by CTFd to enable the plugin."""
    if routes_load is None:
        raise RuntimeError("Plugin loader not available: failed to import routes.admin")

    register_plugin_assets_directory(app, base_path='/plugins/my-plugin/assets/')
    challenges_load(app)
    routes_load(app)



def unload(app):
    if routes_unload is None:
        return None
    return routes_unload(app)
