# -*- coding: utf-8 -*-
"""向後相容單例別名門面：實際實作位於 services/media_converter.py。"""

import sys
from services import media_converter as _mod

sys.modules[__name__] = _mod
