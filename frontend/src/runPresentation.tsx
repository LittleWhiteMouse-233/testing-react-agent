import { Button, Empty, Image, Select, Tooltip } from "antd";
import { Fragment, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { StoredRunEvent, TaskRun, TestRunListResponse, TestRunReport, TestTaskDefinition } from "./api/contracts";
import { artifactUrl } from "./api/client";
import imageUnavailableUrl from "../../backend/app/templates/image-unavailable.svg?inline";

export { imageUnavailableUrl };

export const statusLabels: Record<string, string> = {
  pending: "待启动", running: "运行中", finished: "已结束",
  passed: "通过", failed: "失败", blocked: "阻塞", skipped: "跳过", cancelled: "取消",
  PASS: "通过", FAIL: "失败", BLOCKED: "阻塞", CANCELLED: "取消"
};

export function StatusBadge({ status, label }: { status: string; label?: string }) {
  return <span className={`status-badge status-${status.toLowerCase()}`}><span className="status-dot" />{label ?? statusLabels[status] ?? status}</span>;
}

export function TaskStatusBadge({ task, taskRun, finished }: { task: TestTaskDefinition; taskRun?: TaskRun; finished: boolean }) {
  return <StatusBadge status={taskStatus(taskRun, finished)} label={task.type === "goal" && taskRun?.status === "failed" ? "条件未满足" : undefined} />;
}

export function formatTime(value: string | null | undefined, timeOnly = false) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    ...(timeOnly ? {} : { month: "2-digit", day: "2-digit" }),
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
  const counts = Object.entries(run.task_status_counts).map(([status, count]) => `${status === "failed" ? "失败/条件未满足" : statusLabels[status]} ${count}`);
  return `${run.task_count} 项计划 · 执行记录：${counts.join(" · ") || "尚未执行任务"} · ${run.image_count} 张图片`;
}

export function taskStatus(taskRun: TaskRun | undefined, finished: boolean) {
  return taskRun?.status ?? (finished ? "未执行" : "待执行");
}

export function taskMaxCycles(type: TestTaskDefinition["type"], environment: TestRunReport["detail"]["environment"]) {
  // Snapshot validation requires a budget for every registered task type.
  return environment.execution.max_cycles_by_task_type[type]!;
}

export function taskCycleCount(taskRun: TaskRun, events: StoredRunEvent[]) {
  return Math.max(taskRun.cycle_count, ...events.flatMap(({ event }) =>
    event.type === "cycle.started" && event.task_run_id === taskRun.id ? [event.cycle_count] : []));
}

const eventLabels: Record<StoredRunEvent["event"]["type"], string> = {
  "message.appended": "消息", "message.validation_failed": "消息校验失败", "run.started": "运行开始",
  "run.finished": "运行结束", "run.cancelled": "运行取消", "task.started": "任务开始", "task.finished": "任务结束",
  "cycle.started": "决策轮开始", "tool.started": "工具开始", "tool.succeeded": "工具成功", "tool.withdrawn": "工具发起已撤回",
  "context.updated": "上下文更新", "node.execution": "节点执行", "execution.error": "执行错误", "tasks.skipped": "任务跳过"
};

export function TaskCard({ task, status, progress, children, jumpTargetTitle, active = false }: {
  task: TestTaskDefinition; status?: ReactNode; progress?: ReactNode; children?: ReactNode;
  jumpTargetTitle?: string; active?: boolean;
}) {
  return <article className={"task-card" + (active ? " active" : "")}><details>
    <summary><span className={"task-number " + task.type}>{task.task_no}</span><div className="task-card-summary">
      <div className="task-title"><h3>{task.title}</h3><span className={"pill " + task.type}>{task.type.toUpperCase()}</span>{status}</div>
      {progress}
    </div></summary>
    <div className="task-card-details"><h4>目标</h4><p className="task-text">{task.goal}</p>
      <h4>成功标准</h4><ul className="task-text">{task.success_criteria.map((criterion, index) => <li key={index}>{criterion}</li>)}</ul>
      {task.type === "goal" && <p className="muted">未满足时回到任务 {task.jump_back_to_task_no ?? "待选择"}{jumpTargetTitle && " · " + jumpTargetTitle}</p>}
      {children}
    </div>
  </details></article>;
}

function MessageText({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const textElement = useRef<HTMLDivElement>(null);
  const contentId = useId();
  const characterCount = useMemo(() => Array.from(text).length, [text]);
  useLayoutEffect(() => {
    const element = textElement.current!;
    const measure = () => {
      // Closed report sections have no layout; ResizeObserver measures them when opened.
      if (!element.clientWidth) return;
      setOverflows(element.scrollHeight > Number.parseFloat(getComputedStyle(element).lineHeight) * 3 + 1);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [text]);
  return <div className="message-block"><div ref={textElement} id={contentId} className={"message-text" + (expanded ? "" : " message-preview")}>{text}</div>
    {overflows && <div className="message-actions"><button type="button" className="message-toggle" aria-expanded={expanded} aria-controls={contentId} onClick={() => setExpanded(!expanded)}>{expanded ? "收起" : "展开"}</button><span className="message-count">共 {characterCount} 字</span></div>}
  </div>;
}

export function RunEventContent({ stored, collapseImages = false }: { stored: StoredRunEvent; collapseImages?: boolean }) {
  const event = stored.event;
  if (event.type === "message.appended") {
    const message = event.message;
    return <div className="message-content">
      <div className="message-meta"><span className={"event-role role-" + message.role}>{message.role}</span><span className="mono">{message.message_id}</span>
        {message.role === "tool" && <><span>{message.name}</span><span>{message.status}</span><span className="mono">↳ {message.tool_call_id}</span></>}
      </div>
      {message.content.map((block, index) => block.type === "text"
        ? <MessageText key={index} text={block.text} />
        : collapseImages ? <details key={index} className="message-block image-block"><summary>图片 <span className="mono">{block.artifact_id}</span></summary>
          <Image width={180} src={artifactUrl(block.artifact_id)} fallback={imageUnavailableUrl} alt={`图片 ${block.artifact_id}`} />
        </details> : <Image key={index} width={180} src={artifactUrl(block.artifact_id)} fallback={imageUnavailableUrl} alt={`图片 ${block.artifact_id}`} />)}
      {message.role === "ai" && message.tool_calls.map((call) => <details className="tool-call" key={call.id}>
        <summary>{call.name} <span className="mono">{call.id}</span></summary><pre>{JSON.stringify(call.arguments, null, 2)}</pre>
      </details>)}
      {message.role === "ai" && message.invalid_tool_calls.map((call, index) => <div className="error-text" key={index}>
        <MessageText text={`无效调用 · ${call.name} · ${call.error}\n${call.arguments}`} />
      </div>)}
    </div>;
  }
  switch (event.type) {
    case "cycle.started": return <>第 {event.cycle_count} 轮</>;
    case "context.updated": return <>输入约 {Object.values(event.context.tokens).reduce((sum, value) => sum + value, 0)} token</>;
    case "node.execution": return <>{event.node_name}</>;
    case "tool.started":
    case "tool.withdrawn":
    case "tool.succeeded": return <>{event.tool_name} <span className="mono">{event.call_id}</span></>;
    case "execution.error": return <div className="error-text"><MessageText text={`${event.reason_code} · ${event.message}`} /></div>;
    case "message.validation_failed": return <div className="error-text"><MessageText text={`${event.message_id} · 尝试 ${event.attempt} · ${event.reason}`} /></div>;
    case "tasks.skipped": return <>跳过 {event.task_run_ids.length} 个任务</>;
    default: return <>{eventLabels[event.type]}</>;
  }
}

export function EventTimeline({ events, controls = false }: { events: StoredRunEvent[]; controls?: boolean }) {
  const [filter, setFilter] = useState<string>("");
  const followLatest = useRef(true);
  const lastScrollTop = useRef(0);
  const previousScrollExtent = useRef(0);
  const scrollViewport = useRef({ width: 0, height: 0 });
  const userScrolling = useRef(false);
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const visible = filter ? events.filter((stored) => stored.event.type === filter) : events;
  const updateScrollPosition = useCallback(() => {
    const element = scroller.current;
    if (!element) return;
    if (element.clientWidth !== scrollViewport.current.width || element.clientHeight !== scrollViewport.current.height) userScrolling.current = false;
    // Resolve user movement before a queued scroll event can be overtaken by layout.
    if (userScrolling.current) {
      if (element.scrollTop < lastScrollTop.current - 0.5) followLatest.current = false;
      else if (element.scrollTop > lastScrollTop.current && element.scrollTop >= previousScrollExtent.current - 2) followLatest.current = true;
    }
    if (followLatest.current) element.scrollTop = element.scrollHeight;
    // Layout and programmatic scrolling establish a new baseline, not user intent.
    lastScrollTop.current = element.scrollTop;
    previousScrollExtent.current = element.scrollHeight - element.clientHeight;
    scrollViewport.current = { width: element.clientWidth, height: element.clientHeight };
    userScrolling.current = false;
  }, []);
  useLayoutEffect(() => {
    if (controls) updateScrollPosition();
  }, [events, controls, filter, updateScrollPosition]);
  useEffect(() => {
    if (!controls || !scroller.current || !content.current) return;
    const observer = new ResizeObserver(updateScrollPosition);
    observer.observe(scroller.current);
    observer.observe(content.current);
    return () => observer.disconnect();
  }, [controls, updateScrollPosition]);
  return <section className={"event-timeline" + (controls ? " live-timeline" : "")}>
    {controls && <div className="event-controls"><Select aria-label="筛选事件类型" value={filter} onChange={(value) => { userScrolling.current = false; setFilter(value); }}
      options={[{ value: "", label: "全部事件" }, ...Object.entries(eventLabels).map(([value, label]) => ({ value, label }))]} />
      <Button size="small" onClick={() => { followLatest.current = true; updateScrollPosition(); }}>回到最新</Button>
    </div>}
    <div ref={scroller} className={controls ? "event-scroll" : "event-rows"} tabIndex={controls ? 0 : undefined} aria-label={controls ? "事件列表" : undefined}
      onPointerDown={(event) => { if (controls) userScrolling.current = !(event.target instanceof Element && event.target.closest("button, summary, a, input, textarea, select")); }}
      onTouchMove={() => { if (controls) userScrolling.current = true; }}
      onWheel={(event) => { if (controls) {
        userScrolling.current = true;
        if (event.deltaY < 0) followLatest.current = false;
        else if (event.deltaY > 0 && scroller.current && scroller.current.scrollHeight - scroller.current.clientHeight - scroller.current.scrollTop <= 2) followLatest.current = true;
      } }}
      onKeyDown={(event) => { if (controls && ["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " ", "Enter"].includes(event.key)) {
        if ([" ", "Enter"].includes(event.key) && event.target instanceof Element && event.target.closest("button, summary, a")) { userScrolling.current = false; return; }
        userScrolling.current = true;
        if (["ArrowUp", "PageUp", "Home"].includes(event.key)) followLatest.current = false;
        else if (event.key === "End") followLatest.current = true;
      } }}
      onScroll={() => {
      const element = scroller.current;
      if (!controls || !element) return;
      if (element.scrollHeight - element.clientHeight !== previousScrollExtent.current
        || element.clientWidth !== scrollViewport.current.width || element.clientHeight !== scrollViewport.current.height) {
        updateScrollPosition();
        return;
      }
      const moved = element.scrollTop - lastScrollTop.current;
      if (moved < -0.5) followLatest.current = false;
      else if (moved > 0.5 && element.scrollHeight - element.clientHeight - element.scrollTop <= 2) followLatest.current = true;
      lastScrollTop.current = element.scrollTop;
    }}>
      <div ref={content}>
      {visible.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无事件" /> : visible.map((stored) =>
        <div className="event-row" key={stored.sequence}>
          <time className="mono">{formatTime(stored.occurred_at, true)}</time>
          <div className="event-kind"><span>#{stored.sequence}</span><strong>{eventLabels[stored.event.type]}</strong>
            {stored.event.task_run_id && <span className="mono" title={stored.event.task_run_id}>{stored.event.task_run_id.slice(0, 8)}</span>}
          </div><div className="event-body">{stored.event.type !== "message.appended" && <span className="event-role role-application">application</span>}<RunEventContent stored={stored} collapseImages={controls} /></div>
        </div>)}
      </div>
    </div>
  </section>;
}

type ContextSnapshot = Extract<StoredRunEvent["event"], { type: "context.updated" }>["context"];

const contextCategories = {
  system_prompt: ["系统提示词", "#91672d"], tool_definitions: ["工具定义", "#24658a"],
  user_messages: ["用户消息", "#388070"], skill_body: ["Skill 正文", "#866295"],
  tool_calls: ["工具调用", "#a2513c"], tool_return_text: ["工具返回文字", "#4a6a37"],
  tool_return_nontext: ["工具返回非文字", "#525ba9"], assistant_messages: ["助手消息", "#8b456e"]
} as const satisfies Record<keyof ContextSnapshot["tokens"], [string, string]>;

export function ContextUsage({ context, finished }: { context?: ContextSnapshot; finished: boolean }) {
  const parts = (Object.keys(contextCategories) as (keyof typeof contextCategories)[]).map((key) => {
    const [label, color] = contextCategories[key];
    const tokens = context?.tokens[key] ?? 0;
    return { label, color, tokens, percent: context ? tokens * 100 / context.input_budget_tokens : 0 };
  });
  return <section className="context-usage"><div className="section-heading"><h2>上下文占用</h2>
    <small>{context ? finished ? "最后一次模型请求" : "最近一次模型请求" : finished ? "没有模型请求" : "等待首次模型请求"}</small></div>
    <Tooltip trigger={["hover", "focus"]} color="#ffffff" title={context && <div className="context-tooltip">
      {parts.map(({ label, color, tokens, percent }) => <div key={label} style={{ color }}>
        {label} · 约 {tokens.toLocaleString()} token · {percent.toFixed(1)}%
      </div>)}</div>}>
      <div className="context-bar" tabIndex={0} role="meter" aria-label="上下文占用"
        aria-valuemin={0} aria-valuemax={context?.input_budget_tokens} aria-valuenow={context && parts.reduce((n, p) => n + p.tokens, 0)}>
        {parts.map(({ label, color, percent }) => <span key={label} style={{ backgroundColor: color, width: percent + "%" }} />)}
      </div>
    </Tooltip>
  </section>;
}

export function taskMonitor(events: StoredRunEvent[], lastStartedId?: string) {
  const latestStart = events.filter(({ event }) => event.type === "task.started").at(-1);
  const taskId = latestStart?.event.task_run_id ?? lastStartedId;
  let context: ContextSnapshot | undefined;
  const tools = new Map<string, { calls: number; successes: number }>();
  for (const { event } of events) {
    if (event.task_run_id !== taskId) continue;
    if (event.type === "context.updated") context = event.context;
    if (event.type === "tool.started" || event.type === "tool.succeeded" || event.type === "tool.withdrawn") {
      const count = tools.get(event.tool_name) ?? { calls: 0, successes: 0 };
      if (event.type === "tool.started") count.calls++;
      else if (event.type === "tool.withdrawn") count.calls--;
      else count.successes++;
      tools.set(event.tool_name, count);
    }
  }
  return { context, tools };
}

export function RunResources({ report, monitor }: { report: TestRunReport; monitor?: ReturnType<typeof taskMonitor> }) {
  const { environment, test_plan: plan } = report.detail;
  const catalog = environment.capability_catalog;
  const availableTools = catalog.tool_catalog.tools.filter((tool) => tool.source_id.startsWith("builtin:") ||
    catalog.columns.some((column) => column.id === tool.source_id && column.selected));
  return <div className="resource-grid">
    <section className="resource-card"><h3>工具目录 <span>{availableTools.length} 个可用 / {catalog.tool_catalog.tools.length} 个已发现</span></h3>
      <div className="resource-tools">{catalog.tool_catalog.tools.map((tool) => <details key={tool.name}><summary>{tool.name}<small>{monitor && <span>调用 {monitor.tools.get(tool.name)?.calls ?? 0} 次 · 成功 {monitor.tools.get(tool.name)?.successes ?? 0} 次 · </span>}{tool.source_id} · {availableTools.includes(tool) ? "可用" : "未启用"}</small></summary>
        <p>{tool.description}</p><pre>{JSON.stringify(tool.input_schema, null, 2)}</pre>
      </details>)}</div>
    </section>
    <section className="resource-card"><h3>Skill 与静态资源</h3>
      {catalog.resource_catalog.local_root && <p>本地根目录：<code>{catalog.resource_catalog.local_root}</code></p>}
      {catalog.columns.map((column) => <p key={column.id}><strong>{column.selected ? "已选" : "未选"}</strong> · <code>{column.id}</code></p>)}
      <div className="resource-tools">{catalog.resource_catalog.resources.map((resource) => <details key={`${resource.source_id}:${resource.uri}`}>
        <summary>{resource.name} {monitor?.context?.active_resources[resource.source_id]?.includes(resource.uri) && <b className="resource-active">激活</b>}<small>{resource.source_id}</small></summary>
        <code>{resource.uri}</code>{resource.description && <p>{resource.description}</p>}
        {resource.mime_type && <p>{resource.mime_type}</p>}
      </details>)}
      {catalog.resource_catalog.templates.map((template) => <details key={`${template.source_id}:${template.uri_template}`}>
        <summary>{template.name}<small>{template.source_id}</small></summary>
        <code>{template.uri_template}</code>{template.description && <p>{template.description}</p>}
      </details>)}</div>
    </section>
    <section className="resource-card"><h3>模型与提示词</h3><dl>
      <dt>执行模型</dt><dd>{environment.execution_model.model}</dd>
      <dt>配置</dt><dd>{environment.execution_model.id}</dd>
      {Object.entries(environment.prompt_fingerprints_by_task_type).map(([taskType, fingerprint]) => <Fragment key={taskType}>
        <dt>{taskType.charAt(0).toUpperCase() + taskType.slice(1)}</dt><dd>{fingerprint ?? "未定义"}</dd>
      </Fragment>)}
      <dt>规划</dt><dd>{plan.planning_context.planning_prompt_fingerprint ?? "未定义"}</dd>
    </dl></section>
  </div>;
}
