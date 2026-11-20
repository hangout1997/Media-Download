# -*- coding: utf-8 -*-
"""向後相容單例別名門面：實際實作位於 services/ramdisk_manager.py。"""

import sys
from services import ramdisk_manager as _mod

sys.modules[__name__] = _mod
