"""Report presentation derives from frozen run facts without changing exports' source data."""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import pytest

from app import reporting
from app.domain.resources import ResourceCatalogSnapshot, ResourceDefinitionSnapshot
from mcp_support import build_client, create_plan, finish_turn, replay_call, start_run, text_block, tool_turn, wait_for_run


class ReportEventsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.groups: dict[str, list[int]] = {}
        self.open_groups: list[str] = []
        self.details: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "details":
            group_id = attributes.get("data-task-run-id")
            self.details.append(group_id)
            if group_id is not None:
                self.groups[group_id] = []
                if "open" in attributes:
                    self.open_groups.append(group_id)
        if "data-event-sequence" in attributes:
            group_id = next((group for group in reversed(self.details) if group is not None), None)
            assert group_id is not None, "Every event must belong to one visible report group"
            self.groups[group_id].append(int(attributes["data-event-sequence"] or "0"))

    def handle_endtag(self, tag: str) -> None:
        if tag == "details":
            self.details.pop()


@pytest.mark.parametrize("seconds,expected", [(0, "00:00"), (60.5, "01:01"), (5400, "90:00")])
def test_elapsed_time_matches_online_rounding(seconds: float, expected: str) -> None:
    started_at = datetime(2026, 11, 17, tzinfo=UTC)
    assert reporting.format_elapsed(started_at, started_at + timedelta(seconds=seconds)) == expected


def test_elapsed_time_requires_both_saved_timestamps() -> None:
    finished_at = datetime(2026, 11, 17, tzinfo=UTC)
    assert reporting.format_elapsed(None, finished_at) == "—"
    assert reporting.format_elapsed(finished_at, None) == "—"


def test_elapsed_time_matches_browser_timestamp_millisecond_precision() -> None:
    started_at = datetime(2026, 11, 17, microsecond=999, tzinfo=UTC)
    finished_at = datetime(2026, 11, 17, microsecond=500001, tzinfo=UTC)
    assert reporting.format_elapsed(started_at, finished_at) == "00:01"


def test_skill_resource_groups_reference_each_source_resource_once() -> None:
    first_skill = ResourceDefinitionSnapshot(
        source_id="mcp:first", uri="skill://guide/SKILL.md", name="First guide", is_skill_manifest=True,
    )
    first_reference = ResourceDefinitionSnapshot(
        source_id="mcp:first", uri="skill://guide/references/watch.md", name="First reference",
    )
    nested_skill = ResourceDefinitionSnapshot(
        source_id="mcp:first", uri="skill://guide/child/SKILL.md", name="Nested guide", is_skill_manifest=True,
    )
    second_skill = ResourceDefinitionSnapshot(
        source_id="mcp:second", uri="skill://guide/SKILL.md", name="Second guide", is_skill_manifest=True,
    )
    second_reference = ResourceDefinitionSnapshot(
        source_id="mcp:second", uri="skill://guide/references/watch.md", name="Second reference",
    )
    empty_skill = ResourceDefinitionSnapshot(
        source_id="mcp:first", uri="skill://empty/SKILL.md", name="Empty guide", is_skill_manifest=True,
    )
    ordinary_resource = ResourceDefinitionSnapshot(
        source_id="mcp:first", uri="resource://standalone/notes.txt", name="Notes",
    )
    unrelated_reference = ResourceDefinitionSnapshot(
        source_id="mcp:other", uri="skill://guide/references/watch.md", name="Unrelated reference",
    )
    catalog = ResourceCatalogSnapshot(resources=[
        first_reference, first_skill, nested_skill, second_skill, second_reference,
        empty_skill, ordinary_resource, unrelated_reference,
    ])
    original_catalog = catalog.model_dump(mode="json")

    resource_groups = reporting.group_resources_by_skill(catalog)

    assert list(resource_groups) == [first_skill, second_skill, empty_skill, None]
    assert resource_groups[first_skill] == [first_reference, nested_skill]
    assert resource_groups[second_skill] == [second_reference]
    assert resource_groups[empty_skill] == []
    assert resource_groups[None] == [ordinary_resource, unrelated_reference]
    grouped_resources = [skill for skill in resource_groups if skill is not None]
    grouped_resources.extend(resource for references in resource_groups.values() for resource in references)
    assert len(grouped_resources) == len(catalog.resources)
    assert {id(resource) for resource in grouped_resources} == {id(resource) for resource in catalog.resources}
    assert catalog.model_dump(mode="json") == original_catalog


def test_html_preserves_plan_reentries_events_and_skill_hierarchy(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills" / "base" / "live-tv"
    references = skill_root / "references"
    references.mkdir(parents=True)
    description = "现场观察、选择证据并及时结束任务"
    manifest = skill_root / "SKILL.md"
    manifest.write_text(f"---\nname: live-tv\ndescription: {description}\n---\nSkill instructions", encoding="utf-8")
    reference = references / "camera-picture.md"
    reference.write_text("Camera reference", encoding="utf-8")
    observation = '首段 😀 \r\n第二行\n第三行\n第四行 <script>alert("text")</script>'
    with build_client(tmp_path, skills_dir=tmp_path / "skills",
                      calls=[replay_call(content=[text_block(observation)])],
                      turns=[tool_turn(1), finish_turn(2), finish_turn(3, "failed"), finish_turn(4), finish_turn(5)]) as client:
        plan = create_plan(client)
        definitions = [
            {"task_no": 1, "type": "act", "title": "观察现场", "goal": "读取当前状态", "success_criteria": ["状态可读"]},
            {"task_no": 2, "type": "goal", "title": "检查结束条件", "goal": "确认已结束", "success_criteria": ["遍历完成"], "jump_back_to_task_no": 1},
        ]
        revision = client.post(f"/api/test-plans/{plan['id']}/revisions",
                               json={"content": {**plan["content"], "tasks": definitions}})
        assert revision.status_code == 201, revision.text
        run_id = start_run(client, revision.json())
        detail = wait_for_run(client, run_id)
        assert detail["run"]["verdict"] == "PASS"
        report = client.get(f"/api/runs/{run_id}").json()
        statistics = report["task_statistics"]
        first, _, reentry, _ = [statistics[task["id"]] for task in detail["task_runs"]]
        assert first["tools"]["finish_task"] == reentry["tools"]["finish_task"] == {"calls": 1, "successes": 1}
        assert len(first["tools"]) == 2 and len(reentry["tools"]) == 1
        assert {"initialize", "cycle_guard", "model", "validate", "announce_tool", "tools", "after_tools"} == set(first["nodes"])
        assert all(node["elapsed_seconds"] >= 0 and node["task_percent"] >= 0 for node in first["nodes"].values())
        assert 0 < sum(node["task_percent"] for node in first["nodes"].values()) <= 100
        incremental = client.get(f"/api/runs/{run_id}", params={"events_after": report["events"][-1]["sequence"]}).json()
        assert incremental["events"] == [] and incremental["task_statistics"] == statistics
        # Export must use the frozen catalog, even after source files change.
        manifest.write_text("Changed on disk", encoding="utf-8")
        exported = client.post(f"/api/runs/{run_id}/exports", json={"format": "html"})
        assert exported.status_code == 201, exported.text
        html = client.get(f"/api/artifacts/{exported.json()['id']}").text
        assert "finish_task · 调用 1 次 · 成功 1 次" in html and "节点耗时" in html
        assert "总耗时" in html and "分:秒" in html
        assert "确认的任务计划" in html and "原始顺序" in html and "实际执行顺序" in html
        assert "第 2 次执行" in html and "条件未满足" in html
        assert "结论：</strong>通过" in html
        assert "分析：</strong>Observed result against the success criteria" in html
        assert "正常完成（completed）" in html
        assert f"共 {len(observation)} 字" in html
        assert observation in unescape(html)
        assert "&#13;" in html, "HTML text must preserve carriage returns through browser parsing"
        assert '<script>alert("text")</script>' not in html
        parser = ReportEventsParser()
        parser.feed(html)
        assert not parser.open_groups
        assert list(parser.groups) == ["run", *[task["id"] for task in detail["task_runs"]]]
        for group_id, sequences in parser.groups.items():
            assert sequences == [entry["sequence"] for entry in report["events"]
                                 if (entry["event"]["task_run_id"] or "run") == group_id]
        assert sum(map(len, parser.groups.values())) == len(report["events"])
        package = re.search(r'<article class="skill-package"[^>]*>(.*?)</article>', html, re.S)
        assert package is not None
        assert "已选" in package[1] and description in package[1]
        assert package[1].index(manifest.as_uri()) < package[1].index("references/camera-picture.md")
        assert package[1].index(description) < package[1].index("references/camera-picture.md")
        assert manifest.as_uri() in package[1] and reference.as_uri() in package[1]
        after_export = client.get(f"/api/runs/{run_id}").json()
        assert after_export["detail"] == report["detail"] and after_export["events"] == report["events"]
        assert after_export["artifacts"] == [exported.json()]
        exported_json = client.post(f"/api/runs/{run_id}/exports", json={"format": "json"}).json()
        assert client.get(f"/api/artifacts/{exported_json['id']}").json() == after_export
