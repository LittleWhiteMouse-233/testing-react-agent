import { ReloadOutlined } from "@ant-design/icons";
import { Alert, Button, Input, Modal, Select, Table } from "antd";
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { $api, apiErrorMessage } from "../api/client";
import type { TestRunListResponse, TestRunStatisticsResponse, TestRunVerdict } from "../api/contracts";
import { ElapsedTime, formatDuration, formatTime, runSummary, StatusBadge, statusLabels } from "../runPresentation";
import ReportPage from "./ReportPage";

export function shanghaiToday() { return new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10); }
export function addDays(date: string, count: number) { return new Date(Date.parse(date + "T00:00:00Z") + count * 86400000).toISOString().slice(0, 10); }
const verdicts: TestRunVerdict[] = ["PASS", "FAIL", "BLOCKED", "CANCELLED"];

function HistoryCharts({ statistics, from, to }: { statistics?: TestRunStatisticsResponse; from: string; to: string }) {
  const days: TestRunStatisticsResponse["daily"] = [];
  const byDate = new Map(statistics?.daily.map((entry) => [entry.date, entry]));
  for (let date = from; date <= to; date = addDays(date, 1)) {
    days.push(byDate.get(date) ?? { date, run_count: 0, average_duration_seconds: null, total_duration_seconds: 0 });
  }
  const maxRuns = Math.max(1, ...days.map((day) => day.run_count));
  const maxAverage = Math.max(1, ...days.map((day) => day.average_duration_seconds ?? 0));
  const maxTotal = Math.max(1, ...days.map((day) => day.total_duration_seconds));
  return <div className="history-statistics">
    <section className="stat-card"><small>总运行</small><div className="stat-value">{statistics?.run_count ?? "—"}<em>次</em></div>
      <svg className="history-chart" viewBox="0 0 320 86" role="img" aria-label="每日运行次数"><line x1="0" x2="320" y1="80" y2="80" stroke="#e8e8ed" />
        {days.map((day, index) => <rect key={day.date} x={index * 320 / days.length + 1} y={80 - day.run_count / maxRuns * 70} width={Math.max(1, 320 / days.length - 3)} height={day.run_count / maxRuns * 70} rx="2" fill="#9bbfeb"><title>{day.date}：{day.run_count} 次</title></rect>)}
      </svg><div className="chart-caption"><span>{from}</span><span>{to}</span></div>
    </section>
    <section className="stat-card"><small>结果分布</small><div className="verdict-distribution">{verdicts.map((verdict) => <div key={verdict}><StatusBadge status={verdict} /><strong>{statistics?.verdict_counts[verdict] ?? "—"}</strong></div>)}</div></section>
    <section className="stat-card"><small>平均时长</small><div className="stat-value">{formatDuration(statistics?.average_duration_seconds)}<em>分:秒</em></div>
      <svg className="history-chart" viewBox="0 0 320 86" role="img" aria-label="每日平均时长与总运行时长，各自归一化；悬停查看实际值">
        {[0, 1].map((series) => <g key={series}>{days.map((day, index) => {
          const value = series ? day.total_duration_seconds : day.average_duration_seconds;
          if (value == null) return null;
          const x = (index + 0.5) * 320 / days.length;
          const y = 78 - value / (series ? maxTotal : maxAverage) * 68;
          const previous = days[index - 1];
          const previousValue = series ? previous?.total_duration_seconds : previous?.average_duration_seconds;
          const color = series ? "#7c3aed" : "#0066cc";
          return <g key={day.date}>{previousValue != null && <line x1={(index - 0.5) * 320 / days.length} y1={78 - previousValue / (series ? maxTotal : maxAverage) * 68} x2={x} y2={y} stroke={color} strokeWidth="1.6" />}
            <circle cx={x} cy={y} r="3" fill={color}><title>{day.date} · {series ? "总时长" : "平均时长"} {formatDuration(value)}</title></circle></g>;
        })}</g>)}
      </svg><div className="chart-caption"><span>● 平均时长</span><span className="purple-text">● 总时长</span><span>独立刻度</span></div>
    </section>
  </div>;
}

export default function HistoryPage() {
  const { runId } = useParams();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [caseSearch, setCaseSearch] = useState("");
  const today = shanghaiToday();
  const from = params.get("from") ?? addDays(today, -29);
  const to = params.get("to") ?? today;
  const validRange = /^\d{4}-\d{2}-\d{2}$/.test(from) && /^\d{4}-\d{2}-\d{2}$/.test(to) && Number.isFinite(Date.parse(from)) && Number.isFinite(Date.parse(to)) && from <= to;
  const page = Math.max(1, Number(params.get("page")) || 1);
  const pageSize = [8, 20, 50].includes(Number(params.get("size"))) ? Number(params.get("size")) : 8;
  const selectedVerdict = verdicts.find((value) => value === params.get("verdict"));
  const filters = { search: params.get("search") ?? "", test_case_id: params.get("case") || undefined,
    created_from: validRange ? from + "T00:00:00+08:00" : undefined,
    created_before: validRange ? addDays(to, 1) + "T00:00:00+08:00" : undefined };
  const runs = $api.useQuery("get", "/api/runs", { params: { query: { ...filters, verdict: selectedVerdict, limit: pageSize, offset: (page - 1) * pageSize } } }, { enabled: validRange });
  const statistics = $api.useQuery("get", "/api/runs/statistics", { params: { query: filters } }, { enabled: validRange });
  const cases = $api.useQuery("get", "/api/test-cases", { params: { query: { search: caseSearch, limit: 200 } } });
  const update = (changes: Record<string, string | undefined>) => {
    const next = new URLSearchParams(params); next.delete("page");
    for (const [key, value] of Object.entries(changes)) { if (value) next.set(key, value); else next.delete(key); }
    setParams(next);
  };
  const reportUrl = (id: string) => "/runs/" + id + "/report?" + params;
  return <>
    <header className="page-heading"><div><h1>历史记录 <span className="pill blue">{runs.data?.total ?? "—"} 条</span></h1><p>所有运行条目 · 按时间倒序 · 点击查看执行详情</p></div><Button icon={<ReloadOutlined />} onClick={() => { void runs.refetch(); void statistics.refetch(); }}>刷新</Button></header>
    <div className="history-content">
      {validRange ? <HistoryCharts statistics={statistics.data} from={from} to={to} /> : <Alert type="error" title="请选择有效的日期范围，开始日期不得晚于结束日期" />}
      {statistics.error && <Alert type="error" title={apiErrorMessage(statistics.error, "统计加载失败")} />}
      <div className="history-filters"><div className="verdict-tabs"><button className={!selectedVerdict ? "selected" : ""} onClick={() => update({ verdict: undefined })}>全部 · {statistics.data?.run_count ?? "—"}</button>
        {verdicts.map((verdict) => <button key={verdict} className={selectedVerdict === verdict ? "selected" : ""} onClick={() => update({ verdict })}>{statusLabels[verdict]} · {statistics.data?.verdict_counts[verdict] ?? "—"}</button>)}</div>
        <div className="filter-controls"><Input.Search key={filters.search} aria-label="搜索历史运行" placeholder="运行编号或用例关键字" defaultValue={filters.search} allowClear onSearch={(search) => update({ search })} />
          <Select aria-label="筛选用例" placeholder="所有用例" allowClear value={filters.test_case_id} showSearch={{ filterOption: false, onSearch: setCaseSearch }}
            onChange={(id) => update({ case: id })} options={cases.data?.items.map((entry) => ({ value: entry.id, label: entry.content.name }))} loading={cases.isLoading} />
          <label>从 <input aria-label="开始日期" type="date" value={from} onChange={(event) => update({ from: event.target.value })} /></label>
          <label>至 <input aria-label="结束日期" type="date" value={to} onChange={(event) => update({ to: event.target.value })} /></label>
          <Button onClick={() => setParams({})}>重置</Button>
        </div>
      </div>
      {runs.error && <Alert type="error" title={apiErrorMessage(runs.error, "历史加载失败")} />}
      <Table<TestRunListResponse> className="history-table" rowKey="id" loading={runs.isLoading} dataSource={validRange ? runs.data?.items : []} scroll={{ x: 940 }}
        onRow={(run) => ({ onClick: () => navigate(reportUrl(run.id)), className: "clickable-row" })}
        pagination={{ current: page, pageSize, total: runs.data?.total ?? 0, showSizeChanger: true, pageSizeOptions: [8, 20, 50], showTotal: (total) => "共 " + total + " 条", onChange: (next, size) => update({ page: String(size === pageSize ? next : 1), size: String(size) }) }}
        columns={[
          { title: "Run ID", width: 125, render: (_, run) => <Link className="mono" to={reportUrl(run.id)} title={run.id} onClick={(event) => event.stopPropagation()}>{run.id.slice(0, 8)}</Link> },
          { title: "用例", width: 240, render: (_, run) => <div><small className="mono muted">{run.test_case_id.slice(0, 8)}</small><strong className="table-case-name">{run.test_case_name}</strong></div> },
          { title: "开始时间", width: 155, render: (_, run) => formatTime(run.started_at) },
          { title: "时长", width: 90, render: (_, run) => <ElapsedTime startedAt={run.started_at} finishedAt={run.finished_at} active={run.status === "running"} /> },
          { title: "状态", width: 105, render: (_, run) => <StatusBadge status={run.verdict ?? run.status} /> },
          { title: "摘要", render: (_, run) => <span className="run-summary">{runSummary(run)}</span> },
          { title: "", width: 40, render: () => <span className="muted">→</span> }
        ]} />
    </div>
    <Modal className="run-detail-modal" title="历史记录 / 执行记录" open={!!runId} width={1040} footer={null} destroyOnHidden onCancel={() => navigate("/runs?" + params)}>
      {runId && <ReportPage key={runId} />}
    </Modal>
  </>;
}
