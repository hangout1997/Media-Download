# -*- coding: utf-8 -*-
"""向後相容單例別名門面：實際實作位於 services/gdrive_service.py。"""

import sys
from services import gdrive_service as _mod

sys.modules[__name__] = _mod
