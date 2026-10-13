#!/usr/bin/env python3
"""便捷入口：`python run.py <命令>`，等价于 `python -m ai_drama.cli <命令>`。

之所以留这个文件：很多人（和很多 CI）第一反应是 `python run.py ...`，
与其让他们踩 ModuleNotFoundError，不如直接给一个能跑的入口。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ai_drama.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
