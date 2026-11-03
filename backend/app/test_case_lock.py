"""单 worker 下的用例操作互斥，取得占用时不执行任何异步等待。

这里只拥有进程内占用，不保存业务状态或执行数据库命令。消费者必须在
首个副作用前取得占用，并在操作结束时释放；历史运行读取不经过此锁。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from app.domain.errors import TestCaseBusy, TestCaseHasActiveRun, TestCasePlanning


TestCaseOccupation = Literal[
    "editing", "planning", "pre_planning", "revising", "archiving", "running", "pre_running",
]

_INCOMPATIBLE_OCCUPATIONS: dict[TestCaseOccupation, frozenset[TestCaseOccupation]] = {
    "archiving": frozenset({
        "editing", "revising", "pre_planning", "planning", "pre_running", "running",
    }),
    "editing": frozenset({"archiving", "pre_planning"}),
    "revising": frozenset({"archiving", "planning", "pre_running"}),
    "pre_planning": frozenset({"archiving", "editing"}),
    "planning": frozenset({"archiving", "revising", "pre_running"}),
    "pre_running": frozenset({"archiving", "revising", "planning"}),
    "running": frozenset({"archiving"}),
}


class TestCaseLock:
    """按全部叠加占用准入；准备结束只解除该流程的附加限制。"""

    def __init__(self) -> None:
        self._occupations: dict[str, set[TestCaseOccupation]] = {}

    def has_preparation(self) -> bool:
        """任意用例仍在固定规划输入或准备运行时阻止修改全局配置。"""
        return any(not occupied.isdisjoint({"pre_planning", "pre_running"})
                   for occupied in self._occupations.values())

    def ensure_available(
        self, test_case_id: str, *operations: TestCaseOccupation,
    ) -> None:
        """无操作参数时只检查归档；传入操作时检查整批冲突，不登记占用。"""
        occupied = self._occupations.get(test_case_id, set()).copy()
        if "archiving" in occupied:
            raise TestCaseBusy("用例正在归档，暂不能访问、编辑、规划或执行")
        for requested_operation in operations:
            if (
                requested_operation not in occupied
                and occupied.isdisjoint(_INCOMPATIBLE_OCCUPATIONS[requested_operation])
            ):
                occupied.add(requested_operation)
                continue
            if requested_operation == "editing" and "pre_planning" in occupied:
                raise TestCasePlanning("用例正在准备规划输入，请稍后保存用例")
            if requested_operation == "archiving" and "planning" in occupied:
                raise TestCasePlanning("用例正在规划，请等待完成后再归档")
            if requested_operation in {"archiving", "running"} and "running" in occupied:
                raise TestCaseHasActiveRun("用例正在启动或运行，请等待运行和资源释放结束")
            raise TestCaseBusy("用例正在处理不兼容的操作，请完成后重试")

    def acquire(
        self, test_case_id: str, operation: TestCaseOccupation,
        *additional_operations: TestCaseOccupation,
    ) -> None:
        # Validation and registration cannot yield between them on the single loop.
        self.ensure_available(test_case_id, operation, *additional_operations)
        self._occupations.setdefault(test_case_id, set()).update((operation, *additional_operations))

    def release(
        self, test_case_id: str, operation: TestCaseOccupation,
        *additional_operations: TestCaseOccupation,
    ) -> None:
        occupied = self._occupations.get(test_case_id)
        if occupied is None:
            return
        occupied.difference_update((operation, *additional_operations))
        if not occupied:
            del self._occupations[test_case_id]

    @contextmanager
    def hold(
        self, test_case_id: str, operation: TestCaseOccupation,
        *additional_operations: TestCaseOccupation,
    ) -> Iterator[None]:
        self.acquire(test_case_id, operation, *additional_operations)
        try:
            yield
        finally:
            self.release(test_case_id, operation, *additional_operations)
