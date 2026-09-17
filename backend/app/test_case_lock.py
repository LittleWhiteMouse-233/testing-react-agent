"""单 worker 下的用例操作互斥，取得占用时不执行任何异步等待。

这里只拥有进程内占用，不保存业务状态或执行数据库命令。消费者必须在
首个副作用前取得占用，并在操作结束时释放；历史运行读取不经过此锁。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from app.domain.errors import TestCaseBusy, TestCaseHasActiveRun, TestCasePlanning


TestCaseOccupation = Literal["editing", "planning", "revising", "archiving", "running"]


class TestCaseLock:
    """规划页写操作互斥并阻止新运行；已有运行期间仍可修改用例/计划。"""

    def __init__(self) -> None:
        self._occupations: dict[str, set[TestCaseOccupation]] = {}

    def ensure_not_archiving(self, test_case_id: str) -> None:
        occupied = self._occupations.get(test_case_id)
        if occupied and "archiving" in occupied:
            raise TestCaseBusy("用例正在归档，暂不能访问、编辑、规划或执行")

    def ensure_available(self, test_case_id: str, operation: TestCaseOccupation) -> None:
        self.ensure_not_archiving(test_case_id)
        occupied = self._occupations.get(test_case_id)
        if not occupied:
            return
        if operation in {"editing", "archiving"} and "planning" in occupied:
            raise TestCasePlanning("用例正在生成计划，请等待完成后再修改或归档")
        if operation in {"archiving", "running"} and "running" in occupied:
            raise TestCaseHasActiveRun("用例正在启动或运行，请等待运行和资源释放结束")
        if operation == "archiving" or occupied.intersection({"editing", "planning", "revising"}):
            raise TestCaseBusy("用例正在处理其他操作，请完成后重试")

    def acquire(self, test_case_id: str, operation: TestCaseOccupation) -> None:
        # No await between check and registration: atomic on the single loop.
        self.ensure_available(test_case_id, operation)
        self._occupations.setdefault(test_case_id, set()).add(operation)

    def release(self, test_case_id: str, operation: TestCaseOccupation) -> None:
        occupied = self._occupations[test_case_id]
        occupied.remove(operation)
        if not occupied:
            del self._occupations[test_case_id]

    @contextmanager
    def hold(self, test_case_id: str, operation: TestCaseOccupation) -> Iterator[None]:
        self.acquire(test_case_id, operation)
        try:
            yield
        finally:
            self.release(test_case_id, operation)
