"""夸克中转站 / QuarkRelay 启动脚本。"""

from __future__ import annotations

import sys

from quarkrelay.app import main

if __name__ == "__main__":
    sys.exit(main())
