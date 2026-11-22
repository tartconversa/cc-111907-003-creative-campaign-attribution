"""统一领域错误类型。

CLI 与业务测试只需要捕获一种错误：``DomainError``。
来源校验、设计、观察、预算的拒绝都属于同一个可预期的业务拒绝类别。
"""


class DomainError(ValueError):
    """业务规则拒绝。"""
