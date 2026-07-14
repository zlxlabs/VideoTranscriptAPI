"""
针对 tests/conftest.py 中 `_pytest_targets_tests_manual` 纯函数的单元测试。

背景：早期实现用 sys.argv 字符串子串匹配判断"本次 pytest 调用是否显式指向
tests/manual"，codex review 抓出两个真实的绕过/误判场景：
  1. 误判（false positive）：`pytest --ignore=tests/manual -s` 本意是排除
     manual、跑默认套件，但字符串里含 "tests/manual" 子串会被误判为
     "目标是 manual"，导致默认套件的占位配置预热被错误跳过。
  2. 漏判（false negative）：在 tests/manual 目录内部用相对路径调用
     （如 `cd tests/manual && pytest test_x.py`），命令行参数字符串里根本
     不含 "tests/manual"，会被误判为不是 manual，预热仍会介入，掩盖手动
     测试本该看到的"缺真实配置"状态。

修复后的实现改为基于路径解析（跳过 "-" 开头的选项参数，对剩余位置参数
去掉 "::" node-id 后缀、按调用时的 cwd 解析为绝对路径并规范化，再用
pathlib 的路径语义判断是否落在 tests/manual 目录内）。本文件直接对这个
纯函数传入构造好的 argv/cwd/manual_dir 参数验证，不需要真的启动一次
pytest 子进程。
"""
import pytest

from tests.conftest import _pytest_targets_tests_manual


@pytest.fixture
def repo_layout(tmp_path):
    """构造一个最小的合成仓库目录结构，覆盖判断函数需要用到的路径关系：

    repo_root/
      tests/
        unit/
        manual/
          test_summary_e2e_simple.py
        manual_backup/          # 前缀相似但不是 tests/manual 子目录

    使用 tmp_path 而不是硬编码字符串路径，是为了让 Path.resolve() 在真实
    文件系统上正确规范化（包括潜在的符号链接，例如某些平台上 /tmp 本身就
    是到其他目录的符号链接），避免测试断言依赖于未被 resolve 的路径字符串
    是否恰好相等。
    """
    repo_root = tmp_path / "repo"
    manual_dir = repo_root / "tests" / "manual"
    manual_dir.mkdir(parents=True)
    (manual_dir / "test_summary_e2e_simple.py").write_text("# placeholder\n", encoding="utf-8")
    (repo_root / "tests" / "unit").mkdir(parents=True)
    (repo_root / "tests" / "manual_backup").mkdir(parents=True)
    return repo_root, manual_dir


class TestPytestTargetsTestsManual:
    """覆盖 codex review 抓出的两个绕过场景，以及常规/边界用例。"""

    def test_ignore_flag_is_not_treated_as_manual_target(self, repo_layout):
        """场景 1（误判）：--ignore=tests/manual 是选项参数，不是位置参数目标。"""
        repo_root, manual_dir = repo_layout
        argv = ["--ignore=tests/manual", "-s"]
        assert _pytest_targets_tests_manual(argv, str(repo_root), str(manual_dir)) is False

    def test_relative_call_from_inside_manual_dir_is_detected(self, repo_layout):
        """场景 2（漏判）：cwd 就在 tests/manual 内部，用相对路径调用也要能识别。"""
        _repo_root, manual_dir = repo_layout
        argv = ["test_summary_e2e_simple.py"]
        assert _pytest_targets_tests_manual(argv, str(manual_dir), str(manual_dir)) is True

    def test_explicit_path_from_repo_root_is_detected(self, repo_layout):
        """常规场景：从仓库根目录显式指定 tests/manual 下的测试文件。"""
        repo_root, manual_dir = repo_layout
        argv = ["tests/manual/test_summary_e2e_simple.py"]
        assert _pytest_targets_tests_manual(argv, str(repo_root), str(manual_dir)) is True

    def test_explicit_path_with_node_id_is_detected(self, repo_layout):
        """常规场景：显式路径带 pytest node-id 后缀（::TestClass::test_method）。"""
        repo_root, manual_dir = repo_layout
        argv = ["tests/manual/test_summary_e2e_simple.py::TestClass::test_method"]
        assert _pytest_targets_tests_manual(argv, str(repo_root), str(manual_dir)) is True

    def test_bare_invocation_without_positional_args_is_not_manual(self, repo_layout):
        """裸 `pytest -q`（无位置参数）走默认发现，不算 manual。"""
        repo_root, manual_dir = repo_layout
        assert _pytest_targets_tests_manual(["-q"], str(repo_root), str(manual_dir)) is False
        assert _pytest_targets_tests_manual([], str(repo_root), str(manual_dir)) is False

    def test_mixed_targets_with_one_manual_path_is_detected(self, repo_layout):
        """混合目标：只要有一个位置参数落在 manual 内，整体判定为 manual（保守策略）。"""
        repo_root, manual_dir = repo_layout
        argv = ["tests/unit/", "tests/manual/"]
        assert _pytest_targets_tests_manual(argv, str(repo_root), str(manual_dir)) is True

    def test_similarly_prefixed_sibling_dir_is_not_misdetected(self, repo_layout):
        """边界场景：tests/manual_backup 前缀相似但不是 tests/manual 的子目录。"""
        repo_root, manual_dir = repo_layout
        argv = ["tests/manual_backup/"]
        assert _pytest_targets_tests_manual(argv, str(repo_root), str(manual_dir)) is False
