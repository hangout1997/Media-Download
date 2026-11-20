# -*- coding: utf-8 -*-
"""向後相容單例別名門面：實際實作位於 services/gdrive_bridge.py。"""

import sys
from services import gdrive_bridge as _mod

sys.modules[__name__] = _mod
