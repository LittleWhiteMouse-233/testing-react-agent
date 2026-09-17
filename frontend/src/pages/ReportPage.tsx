import { DownloadOutlined, ReloadOutlined } from "@ant-design/icons";
import { Alert, Button, Empty, Image, Spin, message } from "antd";
import { Link, useParams } from "react-router-dom";
import { $api, apiErrorMessage, artifactUrl } from "../api/client";
import { useRunReport } from "../api/useRunReport";
import { ElapsedTime, EventTimeline, formatTime, RunResources, StatusBadge, taskStatus } from "../runPresentation";

export default function ReportPage() {
  const { runId = "" } = useParams();
  return <RunReportContent key={runId} runId={runId} />;
}

export function RunReportContent({ runId }: { runId: string }) {
  const report = useRunReport(runId);
  const exportReport = $api.useMutation("post", "/api/runs/{test_run_id}/exports", {
    onSuccess: async (artifact) => {
      try {
        const response = await fetch(artifactUrl(artifact.id));
        if (!response.ok) throw new Error("下载失败");
        const url = URL.createObjectURL(await response.blob());
        const link = document.createElement("a");
        link.href = url; link.download = runId + (artifact.type === "html_export" ? ".html" : ".json");
        link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch { message.error("文件已创建，但下载失败，请重试"); }
    },
    onError: (error) => message.error(apiErrorMessage(error, "导出失败"))
  });
  if (report.isLoading) return <div className="page-empty"><Spin /></div>;
  if (!report.data) return <Alert type="error" title={apiErrorMessage(report.error, "报告不存在")} action={<Button onClick={() => void report.refetch()}>重试</Button>} />;
  const { detail, artifacts } = report.data;
  const { run, test_plan: plan, task_runs: taskRuns } = detail;
  const finished = run.status === "finished";
  const screenshots = artifacts.filter((artifact) => artifact.type === "screenshot");
  const messageCount = report.events.filter((stored) => stored.event.type === "message.appended").length;
  const completed = taskRuns.filter((task) => task.status === "passed" || task.status === "failed" || task.status === "blocked" || task.status === "cancelled").length;
  return <div className="run-report">
    <header className="report-heading"><div className="eyebrow">执行记录 <span className="mono">{runId}</span></div><h2>{plan.planning_context.test_case_content.name}</h2>{report.data.test_case_archived && <span className="pill archived">用例已归档</span>}</header>
    {report.contractError && <Alert type="warning" title="实时更新已停止：事件契约错误" />}
    {report.error && <Alert type="warning" title="运行详情刷新失败，保留最近一次读取的记录" action={<Button loading={report.isFetching} onClick={() => void report.refetch()}>重试</Button>} />}
    {report.awaitingFinalResult && <Alert type="info" title="已收到结束通知，等待服务端最终结果" />}
    <section className="report-summary"><h3>执行概要</h3><div className="summary-grid">
      <div><small>状态</small><StatusBadge status={run.verdict ?? run.status} /></div>
      <div><small>开始时间</small><strong>{formatTime(run.started_at)}</strong><small>Asia/Shanghai · +08</small></div>
      <div><small>时长</small><strong><ElapsedTime startedAt={run.started_at} finishedAt={run.finished_at} active={!finished} /></strong><small>分:秒</small></div>
      <div><small>已结束任务</small><strong>{completed} <em>/ {plan.content.tasks.length}</em></strong><small>不包含跳过和未执行</small></div>
    </div></section>
    <details className="report-plan"><summary>计划 v{plan.version_number} · {plan.content.title}</summary>
      <h4>原始用例</h4><p>{plan.planning_context.test_case_content.source_text}</p>
      <h4>前置假设</h4><ul>{plan.content.assumptions.map((text, index) => <li key={index}>{text}</li>)}</ul>
      <h4>准备步骤</h4><ol>{plan.content.setup_steps.map((text, index) => <li key={index}>{text}</li>)}</ol>
      <h4>规划额外输入</h4><p>{plan.planning_context.user_input ?? "无"}</p>
      <h4>版本来源</h4><p>{plan.origin} · {plan.derived_from_plan_id ?? "首次规划"}</p>
      <RunResources report={report.data} />
    </details>
    <div className="section-heading"><h3>执行记录 · 时间线</h3><span>{plan.content.tasks.length} 步任务 · {messageCount} 条消息 · {screenshots.length} 张截图</span></div>
    {report.events.some((stored) => !stored.event.task_run_id) && <details className="timeline-block"><summary>运行生命周期与错误</summary><EventTimeline events={report.events.filter((stored) => !stored.event.task_run_id)} /></details>}
    {plan.content.tasks.map((task, index) => {
      const taskRun = taskRuns.find((entry) => entry.test_task_id === task.test_task_id);
      const events = taskRun ? report.events.filter((stored) => stored.event.task_run_id === taskRun.id) : [];
      const taskScreenshots = screenshots.filter((artifact) => artifact.task_run_id === taskRun?.id);
      return <details className="timeline-block" key={task.test_task_id} open={index === 0 ? true : undefined}>
        <summary><span className={"task-number " + task.definition.type}>{index + 1}</span><div className="timeline-task-heading"><h3>{task.definition.title}</h3><small>{task.definition.type.toUpperCase()} · {events.filter((stored) => stored.event.type === "message.appended").length} 条消息 · {taskScreenshots.length} 张截图</small></div>
          <StatusBadge status={taskStatus(taskRun, finished)} />{taskRun && <ElapsedTime startedAt={taskRun.started_at} finishedAt={taskRun.finished_at} active={taskRun.status === "running"} />}
        </summary>
        <div className="timeline-task-body"><p><strong>目标：</strong>{task.definition.goal}</p><strong>成功标准</strong><ul>{task.definition.success_criteria.map((criterion, position) => <li key={position}>{criterion}</li>)}</ul>
          {taskRun?.result && <div className="result-note"><p>{taskRun.result.summary}</p><small>{taskRun.result.reason_code} · {taskRun.cycle_count} / {task.definition.max_cycles} 轮</small></div>}
          {events.length ? <EventTimeline events={events} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有任务事件" />}
          {taskScreenshots.length > 0 && <Image.PreviewGroup><div className="evidence-grid">{taskScreenshots.map((artifact) => <figure key={artifact.id}><Image src={artifactUrl(artifact.id)} alt={"截图 " + artifact.id} />
            <figcaption>{formatTime(artifact.created_at)} {taskRun?.result?.evidence_artifact_ids.includes(artifact.id) && <span className="pill blue">判定证据</span>}</figcaption></figure>)}</div></Image.PreviewGroup>}
        </div>
      </details>;
    })}
    <footer className="report-footer"><span className="mono">Run ID {runId}</span><div>
      {(["json", "html"] as const).map((format) => <Button key={format} icon={<DownloadOutlined />} disabled={!finished} loading={exportReport.isPending} onClick={() => exportReport.mutate({ params: { path: { test_run_id: runId } }, body: { format } })}>{format.toUpperCase()} 报告</Button>)}
      {report.data.test_case_archived ? <Button disabled icon={<ReloadOutlined />}>用例已归档，无法重新执行</Button> : <Link to={"/cases/" + plan.test_case_id + "/plan"}><Button type="primary" icon={<ReloadOutlined />}>重新执行 · 确认最新计划</Button></Link>}
    </div></footer>
  </div>;
}
