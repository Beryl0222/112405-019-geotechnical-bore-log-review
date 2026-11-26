#!/usr/bin/env python3
"""合成资料演示入口（无第三方依赖）。

推荐以隔离模式运行，避免资料目录中的同名模块干扰：
    python3 -I run_demo.py data/case.json --outdir out
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from geotech_ledger.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
