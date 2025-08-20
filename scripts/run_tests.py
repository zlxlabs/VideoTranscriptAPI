#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import unittest

# 添加项目根目录到导入路径
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

def run_all_tests():
    """
    运行所有测试用例
    """
    tests_dir = os.path.join(os.path.dirname(__file__), 'tests')
    test_loader = unittest.TestLoader()
    test_suite = test_loader.discover(tests_dir, pattern='test_*.py')
    
    test_runner = unittest.TextTestRunner(verbosity=2)
    result = test_runner.run(test_suite)
    
    return result.wasSuccessful()

if __name__ == '__main__':
    print("开始运行测试...")
    success = run_all_tests()
    print("测试完成。")
    
    if not success:
        sys.exit(1)
    sys.exit(0) 