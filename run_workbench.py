#!/usr/bin/env python3
"""工作台启动器。

隔离模式（python3 -I）会忽略当前目录、PYTHONPATH 与脚本目录，因此这里显式把
**本启动器所在的可信代码目录**（基于 __file__，不依赖 cwd）加入 sys.path。
标准库在路径注入前先导入，避免同名模块（如伪造的 json.py）遮蔽。
合成资料目录 fixtures/ 从不加入 sys.path，也不在其中运行解释器。
"""
import json  # noqa: F401  预载标准库，防止路径注入后被同名文件遮蔽
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fundbench.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
