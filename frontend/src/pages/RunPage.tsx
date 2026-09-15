import { StopOutlined } from "@ant-design/icons";
import { Alert, Button, Empty, Image, Spin, message } from "antd";
import { useState } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { $api, apiErrorMessage, artifactUrl } from "../api/client";
import { latestScreenshotArtifactId } from "../api/runEvents";
import { useRunReport } from "../api/useRunReport";
import { ElapsedTime, EventTimeline, RunResources, StatusBadge, taskCycleCount, taskStatus } from "../runPresentation";

export function ActiveRunPage() {
  const runs = $api.useQuery("get", "/api/runs", { params: { query: { status: ["pending", "running"], limit: 1 } } }, { refetchInterval: 3000 });
  if (runs.isLoading) return <div className="page-empty"><Spin /></div>;
  if (runs.error) return <div className="page-empty"><Alert type="error" title={apiErrorMessage(runs.error, "运行加载失败")} /></div>;
  const run = runs.data?.items[0];
  if (run) return <Navigate to={"/runs/" + run.id} replace />;
  return <div className="page-empty"><Empty description="当前没有活动运行"><Link to="/"><Button type="primary">前往用例规划</Button></Link></Empty></div>;
}

export default function RunPage() {
  const { runId = "" } = useParams();
  return <RunExecution key={runId} runId={runId} />;
}

function RunExecution({ runId }: { runId: string }) {
  const report = useRunReport(runId);
  const [cancelRequested, setCancelRequested] = useState(false);
  const cancel = $api.useMutation("post", "/api/runs/{test_run_id}/cancel", {
    onSuccess: () => { setCancelRequested(true); message.info("取消请求已提交，将在安全边界生效"); },
    onError: (error) => message.error(apiErrorMessage(error, "取消请求失败"))
  });
  if (report.isLoading) return <div className="page-empty"><Spin /></div>;
  if (!report.data) return <div className="page-empty"><Alert type="error" title={apiErrorMessage(report.error, "运行不存在")} action={<Button onClick={() => void report.refetch()}>重试</Button>} /></div>;
  const { run, test_plan: plan, task_runs: taskRuns } = report.data.detail;
  const finished = run.status === "finished";
  const activeTask = taskRuns.find((task) => task.status === "running");
  const activeIndex = plan.content.tasks.findIndex((task) => task.test_task_id === activeTask?.test_task_id);
  const passed = taskRuns.filter((task) => task.status === "passed").length;
  const screenshotId = latestScreenshotArtifactId(report.events);
  return <>
    <header className="page-heading"><div><div className="eyebrow"><Link to={"/cases/" + plan.test_case_id + "/plan"}>用例库</Link> / <span className="mono">{plan.test_case_id.slice(0, 8)}</span> / 执行</div>
      <h1>{plan.planning_context.test_case_content.name}</h1><p><StatusBadge status={run.verdict ?? run.status} /> <span className="mono">{run.id}</span></p></div>
      <div className="heading-actions"><Link to={"/cases/" + plan.test_case_id + "/plan"}><Button>查看最新计划</Button></Link>
        {finished ? <Link to={"/runs/" + runId + "/report"}><Button type="primary">查看执行记录</Button></Link> :
          <Button danger icon={<StopOutlined />} disabled={cancelRequested} loading={cancel.isPending} onClick={() => cancel.mutate({ params: { path: { test_run_id: runId } } })}>{cancelRequested ? "等待取消生效" : "终止"}</Button>}
      </div>
    </header>
    {report.contractError && <Alert type="error" title="事件流违反 OpenAPI 契约，实时更新已停止" />}
    {report.error && <Alert type="warning" title="运行详情刷新失败，保留最近一次读取的记录" action={<Button onClick={() => void report.refetch()}>重试</Button>} />}
    <div className="execution-layout">
      <aside className="task-queue"><div className="section-heading"><div><small>任务队列</small><h2>执行进度</h2></div><span>{passed}/{plan.content.tasks.length} 通过</span></div>
        <div className="queue-meta"><span>已用 <ElapsedTime startedAt={run.started_at} finishedAt={run.finished_at} active={!finished} /></span><span>{activeIndex >= 0 ? "当前第 " + (activeIndex + 1) + " 步" : finished ? "已结束" : "等待启动"}</span></div>
        {plan.content.tasks.map((task, index) => {
          const taskRun = taskRuns.find((entry) => entry.test_task_id === task.test_task_id);
          return <article key={task.test_task_id} className={"queue-task " + (taskRun?.status === "running" ? "active" : "")}>
            <span className={"task-number " + task.definition.type}>{index + 1}</span><div><div className="task-title"><h3>{task.definition.title}</h3><StatusBadge status={taskStatus(taskRun, finished)} /></div>
              <small>{task.definition.type.toUpperCase()}</small><p>{task.definition.goal}</p>
              <details><summary>成功标准</summary><ul>{task.definition.success_criteria.map((criterion, position) => <li key={position}>{criterion}</li>)}</ul></details>
              <div className="task-runtime"><span>{taskRun ? taskCycleCount(taskRun, report.events) : "—"} / {task.definition.max_cycles} 轮</span><ElapsedTime startedAt={taskRun?.started_at ?? null} finishedAt={taskRun?.finished_at ?? null} active={taskRun?.status === "running"} /></div>
              {taskRun?.result && <p className="task-result">{taskRun.result.summary}</p>}
            </div>
          </article>;
        })}
      </aside>
      <section className="execution-observation"><div className="section-heading"><div><small>观察与证据</small><h2>最新截图</h2></div><span>v{plan.version_number}</span></div>
        <div className="latest-screenshot">{screenshotId ? <Image src={artifactUrl(screenshotId)} alt="最新电视截图" /> : <Empty description={finished ? "本次运行没有截图" : "等待首次截图…"} />}</div>
        <div className="section-heading"><div><small>本次运行冻结的资源</small><h2>工具与模型</h2></div></div><RunResources report={report.data} />
      </section>
      <section className="execution-events"><div className="section-heading"><div><small>持久化运行事实</small><h2>事件流</h2></div><span className={"connection " + report.connection}>{report.contractError ? "已停止" : ({ connecting: "连接中", connected: "实时连接", reconnecting: "正在重连", finished: "已结束" }[report.connection])}</span></div>
        <EventTimeline events={report.events} controls /><footer className="event-footer">{report.events.length} 条事件 · 按保存顺序显示</footer>
      </section>
    </div>
  </>;
}
