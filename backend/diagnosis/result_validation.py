from backend.domain.multi_agent import ResultValidationCategory


class AgentResultValidationError(ValueError):
    """仅携带固定合同边界，禁止附加模型输出或动态异常文本。"""

    def __init__(self, category: ResultValidationCategory) -> None:
        self.category = category
        super().__init__(category.value)
