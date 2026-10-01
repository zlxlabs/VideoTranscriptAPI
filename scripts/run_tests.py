#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""pytest 薄包装。

历史上这个脚本用 unittest.TestLoader 找 scripts/tests，从未 work 过；
现在改成 pytest 薄包装。推荐入口仍然是 make test。
"""

import os
import sys

import pytest


def main() -> int:
    """从仓库根目录执行 ``pytest -q tests``，非 0 一律映射为退出码 1。"""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    os.chdir(repo_root)
    return 0 if pytest.main(["-q", "tests"]) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
