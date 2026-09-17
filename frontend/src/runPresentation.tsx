import { Button, Empty, Image, Select, Switch } from "antd";
import { useEffect, useRef, useState } from "react";
import type { StoredRunEvent, TaskRun, TestRunListResponse, TestRunReport } from "./api/contracts";
import { artifactUrl } from "./api/client";

export const statusLabels: Record<string, string> = {
  pending: "待启动", running: "运行中", finished: "已结束",
  passed: "通过", failed: "失败", blocked: "阻塞", skipped: "跳过", cancelled: "取消",
  PASS: "通过", FAIL: "失败", BLOCKED: "阻塞", CANCELLED: "取消"
};

export function StatusBadge({ status }: { status: string }) {
  return <span className={`status-badge status-${status.toLowerCase()}`}><span className="status-dot" />{statusLabels[status] ?? status}</span>;
}

export function formatTime(value: string | null | undefined, timeOnly = false) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", ...(timeOnly ? {} : { month: "2-digit", day: "2-digit" }),
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false
  }).format(new Date(value));
}

export function formatDuration(seconds: number | null | undefined) {
  if (seconds == null) return "—";
  const wholeSeconds = Math.max(0, Math.round(seconds));
  return `${Math.floor(wholeSeconds / 60).toString().padStart(2, "0")}:${(wholeSeconds % 60).toString().padStart(2, "0")}`;
}

export function ElapsedTime({ startedAt, finishedAt, active = false }: { startedAt: string | null; finishedAt: string | null; active?: boolean }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  const end = finishedAt ? new Date(finishedAt).getTime() : active ? now : null;
  return <span className="mono">{formatDuration(startedAt && end != null ? (end - new Date(startedAt).getTime()) / 1000 : null)}</span>;
}

export function runSummary(run: TestRunListResponse) {
  const counts = Object.entries(run.task_status_counts).map(([status, count]) => `${statusLabels[status]} ${count}`);
  return `${counts.join(" · ") || "尚未执行任务"} / ${run.task_count} 步 · ${run.screenshot_count} 张截图`;
}

export function taskStatus(taskRun: TaskRun | undefined, finished: boolean) {
  return taskRun?.status ?? (finished ? "未执行" : "待执行");
}

export function taskCycleCount(taskRun: TaskRun, events: StoredRunEvent[]) {
  return Math.max(taskRun.cycle_count, ...events.flatMap(({ event }) =>
    event.type === "cycle.started" && event.task_run_id === taskRun.id ? [event.cycle_count] : []));
}

const eventLabels: Record<StoredRunEvent["event"]["type"], string> = {
  "message.appended": "消息", "message.validation_failed": "消息校验失败", "run.started": "运行开始",
  "run.finished": "运行结束", "run.cancelled": "运行取消", "task.started": "任务开始", "task.finished": "任务结束",
  "cycle.started": "决策轮开始", "tool.started": "工具开始", "execution.error": "执行错误", "tasks.skipped": "任务跳过"
};

export function RunEventContent({ stored }: { stored: StoredRunEvent }) {
  const event = stored.event;
  if (event.type === "message.appended") {
    const message = event.message;
    return <div className="message-content">
      <div className="message-meta"><span>{message.role}</span><span className="mono">{message.message_id}</span>
        {message.role === "tool" && <><span>{message.name}</span><span>{message.status}</span><span className="mono">↳ {message.tool_call_id}</span></>}
      </div>
      {message.content.map((block, index) => block.type === "text"
        ? <div key={index} className="message-text">{block.text}</div>
        : <Image key={index} width={180} src={artifactUrl(block.artifact_id)} alt={`截图 ${block.artifact_id}`} />)}
      {message.role === "ai" && message.tool_calls.map((call) => <details className="tool-call" key={call.id}>
        <summary>{call.name} <span className="mono">{call.id}</span></summary><pre>{JSON.stringify(call.arguments, null, 2)}</pre>
      </details>)}
      {message.role === "ai" && message.invalid_tool_calls.map((call, index) => <div className="error-text" key={index}>
        无效调用 · {call.name} · {call.error}<pre>{call.arguments}</pre>
      </div>)}
    </div>;
  }
  switch (event.type) {
    case "cycle.started": return <>第 {event.cycle_count} 轮</>;
    case "tool.started": return <>调用 <span className="mono">{event.call_id}</span></>;
    case "execution.error": return <span className="error-text">{event.reason_code} · {event.message}</span>;
    case "message.validation_failed": return <span className="error-text">{event.message_id} · 尝试 {event.attempt} · {event.reason}</span>;
    case "tasks.skipped": return <>跳过 {event.task_run_ids.length} 个任务</>;
    default: return <>{eventLabels[event.type]}</>;
  }
}

export function EventTimeline({ events, controls = false }: { events: StoredRunEvent[]; controls?: boolean }) {
  const [filter, setFilter] = useState<string>("");
  const [stick, setStick] = useState(true);
  const scroller = useRef<HTMLDivElement>(null);
  const visible = filter ? events.filter((stored) => stored.event.type === filter) : events;
  useEffect(() => {
    if (controls && stick && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight;
  }, [events.length, controls, stick, filter]);
  return <section className="event-timeline">
    {controls && <div className="event-controls"><Select aria-label="筛选事件类型" value={filter} onChange={setFilter}
      options={[{ value: "", label: "全部事件" }, ...Object.entries(eventLabels).map(([value, label]) => ({ value, label }))]} />
      <span><Switch size="small" checked={stick} onChange={setStick} /> 自动贴底</span>
      {!stick && <Button size="small" onClick={() => setStick(true)}>回到最新</Button>}
    </div>}
    <div ref={scroller} className={controls ? "event-scroll" : "event-rows"} onScroll={() => {
      const element = scroller.current;
      if (controls && element && element.scrollHeight - element.clientHeight - element.scrollTop > 40) setStick(false);
    }}>
      {visible.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无事件" /> : visible.map((stored) =>
        <div className="event-row" key={stored.sequence}>
          <time className="mono">{formatTime(stored.occurred_at, true)}</time>
          <div className="event-kind"><span>#{stored.sequence}</span><strong>{eventLabels[stored.event.type]}</strong>
            {stored.event.task_run_id && <span className="mono" title={stored.event.task_run_id}>{stored.event.task_run_id.slice(0, 8)}</span>}
          </div><RunEventContent stored={stored} />
        </div>)}
    </div>
  </section>;
}

export function RunResources({ report }: { report: TestRunReport }) {
  const { environment, test_plan: plan } = report.detail;
  return <div className="resource-grid">
    <section className="resource-card"><h3>工具目录 <span>{environment.tool_catalog.tools.length} 个</span></h3>
      <div className="resource-tools">{environment.tool_catalog.tools.map((tool) => <details key={tool.name}><summary>{tool.name}<small>{tool.source}</small></summary>
        <p>{tool.description}</p><pre>{JSON.stringify(tool.input_schema, null, 2)}</pre>
      </details>)}</div>
    </section>
    <section className="resource-card"><h3>模型与提示词</h3><dl>
      <dt>执行模型</dt><dd>{environment.execution_model.model}</dd>
      <dt>配置</dt><dd>{environment.execution_model.profile_id}</dd>
      <dt>Act</dt><dd>{environment.act_prompt_version}</dd><dt>Judge</dt><dd>{environment.judge_prompt_version}</dd>
      <dt>规划</dt><dd>{plan.planning_context.planning_prompt_version}</dd>
    </dl></section>
  </div>;
}
