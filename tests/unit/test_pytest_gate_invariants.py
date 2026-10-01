"""金丝雀：守住 pytest 门禁本身的配置项，防止被悄悄删掉。

承诺边界：这条配置只能保证「没有测试通过返回值表达判定」。
它抓不到：吞异常后隐式返回 None 的 print 型测试、写在 except 里的 assert、
unittest.TestCase 方法的返回值、本身无意义的断言（assert True 等）。
"""


def test_return_value_verdicts_are_errors(pytestconfig):
    """用返回值表达判定的测试必须让门禁变红（PytestReturnNotNoneWarning → error）。"""
    filterwarnings = pytestconfig.getini("filterwarnings")
    assert "error::pytest.PytestReturnNotNoneWarning" in filterwarnings, (
        "pytest 门禁必须把 PytestReturnNotNoneWarning 升级为 error，"
        "否则测试写 `return False` 会被静默判绿"
    )