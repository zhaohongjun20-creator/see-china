# -*- coding: utf-8 -*-
# 让 pytest 从任意目录运行时都能 import 项目内的 src 包
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
