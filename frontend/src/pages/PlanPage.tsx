import { ArrowDownOutlined, ArrowUpOutlined, DeleteOutlined, EditOutlined, PlusOutlined, RocketOutlined, SaveOutlined } from "@ant-design/icons";
import { useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Checkbox, Empty, Form, Input, InputNumber, Modal, Radio, Select, Spin, message } from "antd";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { $api, apiErrorMessage } from "../api/client";
import type { TestCaseContent, TestPlan, TestPlanDraft, TestTaskDefinition } from "../api/contracts";
import { emptyTask, isPlanDraftValid, toPlanDraft } from "../planDraft";
import { formatTime, runSummary, StatusBadge } from "../runPresentation";

export default function PlanPage({ archiving = false }: { archiving?: boolean }) {
  const { caseId = "" } = useParams();
  return <TestCasePlan key={caseId} caseId={caseId} archiving={archiving} />;
}

function TestCasePlan({ caseId, archiving }: { caseId: string; archiving: boolean }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [plan, setPlan] = useState<TestPlan | null>(null);
  const [draft, setDraft] = useState<TestPlanDraft | null>(null);
  const [dirty, setDirty] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [userInput, setUserInput] = useState("");
  const [editing, setEditing] = useState(false);
  const [caseDraft, setCaseDraft] = useState<TestCaseContent | null>(null);
  const [caseUpdated, setCaseUpdated] = useState(false);
  const testCase = $api.useQuery("get", "/api/test-cases/{test_case_id}", { params: { path: { test_case_id: caseId } } });
  const plans = $api.useQuery("get", "/api/test-cases/{test_case_id}/plans", { params: { path: { test_case_id: caseId } } });
  const runs = $api.useQuery("get", "/api/runs", { params: { query: { test_case_id: caseId, limit: 5 } } });
  const updateCase = $api.useMutation("patch", "/api/test-cases/{test_case_id}", {
    meta: { testCaseId: caseId },
    onSuccess: (value) => {
      queryClient.setQueryData($api.queryOptions("get", "/api/test-cases/{test_case_id}", { params: { path: { test_case_id: caseId } } }).queryKey, value);
      for (const path of ["/api/test-cases", "/api/runs", "/api/runs/test-cases"]) {
        void queryClient.invalidateQueries({ queryKey: ["get", path] });
      }
      setCaseDraft(null); setConfirmed(false); setCaseUpdated(true);
      message.success("用例已更新，请检查计划或重新规划");
    },
    onError: (error) => {
      message.error(apiErrorMessage(error, "保存用例失败"));
      if (error.code === "test_case_not_found") void testCase.refetch();
    }
  });
  const selectPlan = (value: TestPlan) => {
    setPlan(value); setDraft(toPlanDraft(value)); setConfirmed(false); setDirty(false); setEditing(false);
  };
  useEffect(() => { if (!plan && plans.data?.items[0]) selectPlan(plans.data.items[0]); }, [plans.data, plan]);
  const acceptPlan = (value: TestPlan) => {
    selectPlan(value);
    setCaseUpdated(false);
    void queryClient.invalidateQueries({ queryKey: ["get", "/api/test-cases/{test_case_id}/plans"] });
    void queryClient.invalidateQueries({ queryKey: ["get", "/api/test-cases"] });
  };
  const generate = $api.useMutation("post", "/api/test-cases/{test_case_id}/plans", {
    meta: { testCaseId: caseId },
    onSuccess: (value) => { acceptPlan(value); setUserInput(""); message.success("计划已生成"); },
    onError: async (error) => {
      message.error(apiErrorMessage(error, "计划生成失败"));
      if (error.code === "planning_input_not_found") void testCase.refetch();
      if (error.code === "test_plan_not_latest") {
        const latest = (await plans.refetch()).data?.items[0];
        if (latest) selectPlan(latest);
      }
    }
  });
  const revise = $api.useMutation("post", "/api/test-plans/{test_plan_id}/revisions", {
    meta: { testCaseId: caseId },
    onSuccess: (value) => { acceptPlan(value); message.success("已保存为计划版本 " + value.version_number); },
    onError: (error) => {
      message.error(apiErrorMessage(error, "保存失败"));
      if (error.code === "test_plan_not_found") void testCase.refetch();
      if (error.code === "test_plan_not_latest") void plans.refetch();
    }
  });
  const start = $api.useMutation("post", "/api/runs", {
    meta: { testCaseId: caseId },
    onSuccess: (run) => {
      void queryClient.invalidateQueries({ queryKey: ["get", "/api/runs"] });
      navigate("/runs/" + run.id);
    },
    onError: (error) => {
      message.error(apiErrorMessage(error, "启动失败"));
      if (error.code === "run_input_not_found") void testCase.refetch();
      if (error.code === "test_plan_not_startable") { setConfirmed(false); void plans.refetch(); }
    }
  });
  const latest = plans.data?.items[0];
  const isLatest = !!plan && plan.id === latest?.id;
  const busy = archiving || generate.isPending || revise.isPending || start.isPending || caseDraft !== null || updateCase.isPending;
  const canEdit = !busy && isLatest;
  const valid = isPlanDraftValid(draft);
  const canGenerate = !busy && !dirty && (!plan || (isLatest && !!userInput.trim()));
  const generatePlan = () => { if (canGenerate) generate.mutate({ params: { path: { test_case_id: caseId } }, body: { user_input: userInput.trim() || null } }); };
  const updateDraft = (value: TestPlanDraft) => { setDraft(value); setDirty(true); setConfirmed(false); };
  const updateTask = (index: number, patch: Partial<TestTaskDefinition>) => {
    if (draft) updateDraft({ ...draft, tasks: draft.tasks.map((task, position) => position === index ? { ...task, ...patch } : task) });
  };
  const moveTask = (index: number, direction: number) => {
    if (!draft) return;
    const tasks = [...draft.tasks];
    const current = tasks[index]; const other = tasks[index + direction];
    if (!current || !other) return;
    tasks[index] = other; tasks[index + direction] = current; updateDraft({ ...draft, tasks });
  };
  if (testCase.isLoading || plans.isLoading) return <div className="planning-welcome"><Spin /></div>;
  if (testCase.error || plans.error || !testCase.data) return <div className="planning-welcome"><Alert type="error" title={apiErrorMessage(testCase.error ?? plans.error, "用例加载失败")} action={<Button onClick={() => { void testCase.refetch(); void plans.refetch(); }}>重试</Button>} /><Link to="/">返回用例列表</Link></div>;

  return <>
    <section className="plan-context">
      <div className="context-heading"><div className="eyebrow">用例库 / <span className="mono" title={caseId}>{caseId.slice(0, 8)}</span></div><h2>{testCase.data.content.name}</h2>
      </div>
      <div className="context-scroll"><section className="case-description"><div className="section-label">用例描述 <span>{testCase.data.content.source_text.length} 字</span>
        {!caseDraft && <Button size="small" icon={<EditOutlined />} disabled={busy || dirty || editing} onClick={() => setCaseDraft({ ...testCase.data.content })}>编辑用例</Button>}</div>
        {caseDraft ? <Form layout="vertical" disabled={updateCase.isPending || archiving} onFinish={() => { if (!archiving && caseDraft.name.trim() && caseDraft.name.length <= 200 && caseDraft.source_text.trim()) updateCase.mutate({ params: { path: { test_case_id: caseId } }, body: caseDraft }); }}>
          <Form.Item label="用例名称" required validateStatus={!caseDraft.name.trim() ? "error" : undefined} help={!caseDraft.name.trim() ? "名称不能为空" : undefined}>
            <Input aria-label="用例名称" maxLength={200} value={caseDraft.name} onChange={(event) => setCaseDraft({ ...caseDraft, name: event.target.value })} />
          </Form.Item>
          <Form.Item label="用例描述" required validateStatus={!caseDraft.source_text.trim() ? "error" : undefined} help={!caseDraft.source_text.trim() ? "描述不能为空" : undefined}>
            <Input.TextArea aria-label="用例描述" rows={7} value={caseDraft.source_text} onChange={(event) => setCaseDraft({ ...caseDraft, source_text: event.target.value })} />
          </Form.Item>
          <div className="editor-footer"><Button onClick={() => setCaseDraft(null)}>取消</Button><Button type="primary" htmlType="submit" loading={updateCase.isPending} disabled={!caseDraft.name.trim() || caseDraft.name.length > 200 || !caseDraft.source_text.trim()}>保存用例</Button></div>
        </Form> : <p>{testCase.data.content.source_text}</p>}
        {caseUpdated && <Alert type="info" title="用例已更新，请检查计划或重新规划" />}
        {plan && <>
          <h3>准备步骤</h3>{plan.content.setup_steps.length ? <ol className="setup-list">{plan.content.setup_steps.map((step, index) => <li key={index}>{step}</li>)}</ol> : <p className="muted">无准备步骤</p>}
          <h3>前置假设</h3>{plan.content.assumptions.length ? <ul>{plan.content.assumptions.map((assumption, index) => <li key={index}>{assumption}</li>)}</ul> : <p className="muted">无前置假设</p>}
        </>}
      </section>
      {plan && <details className="planning-audit"><summary>规划来源与额外输入</summary><dl><dt>生成此计划时的额外输入</dt><dd>{plan.planning_context.user_input ?? "无"}</dd>
        <dt>规划模型</dt><dd>{plan.planning_context.planning_model.model}</dd><dt>父计划</dt><dd className="mono">{plan.derived_from_plan_id ?? "首次规划"}</dd></dl></details>}
      </div>
      <div className="planning-prompt"><div className="section-label">告诉规划 Agent 如何调整 <small>Ctrl / ⌘ + Enter 发送</small></div>
        <Form.Item label={"额外输入（" + (plan ? "必填" : "可选") + "）"} required={!!plan}>
          <Input.TextArea aria-label="规划额外输入" value={userInput} disabled={busy || (!!plan && !isLatest)} rows={4} placeholder={plan ? "说明希望如何修改最新计划" : "补充本次规划的要求"}
            onChange={(event) => setUserInput(event.target.value)} onKeyDown={(event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); generatePlan(); } }} />
        </Form.Item>
        {dirty && <Alert type="info" title="存在未保存的修改，请先保存新版本再重新规划" action={<Button disabled={busy} onClick={() => { if (plan) selectPlan(plan); }}>放弃修改</Button>} />}
        {!!plan && !isLatest && <Alert type="info" title="历史计划仅供回溯，不能修订或启动" />}
        <div className="prompt-actions"><span className="muted">{generate.isPending ? "规划中…" : "以目标和成功标准描述任务"}</span><Button type="primary" loading={generate.isPending} disabled={!canGenerate} onClick={generatePlan}>{plan ? "重新规划" : "生成计划"}</Button></div>
      </div>
    </section>
    <aside className="plan-results"><div className="section-heading"><div><small>规划结果</small><h2>任务序列</h2></div>{plan && <Button icon={<EditOutlined />} disabled={!canEdit} onClick={() => setEditing(true)}>编辑计划</Button>}</div>
      {plan && <div className="version-strip"><span className={"pill " + (isLatest ? "blue" : "")}>版本 {plan.version_number} · {isLatest ? "最新" : "历史只读"}</span>
        <Select aria-label="计划版本" value={plan.id} disabled={busy || dirty} onChange={(id) => { const selected = plans.data?.items.find((entry) => entry.id === id); if (selected) selectPlan(selected); }}
          options={plans.data?.items.map((entry) => ({ value: entry.id, label: "v" + entry.version_number + (entry.id === latest?.id ? " · 最新" : " · 只读") }))} />
      </div>}
      {plan && draft ? <>
        <div className="plan-task-list"><h3>{draft.title}</h3>{draft.tasks.map((task, index) => <article className="plan-task" key={index}><span className={"task-number " + task.type}>{index + 1}</span><div>
          <div className="task-title"><h3>{task.title}</h3><span className={"pill " + (task.type === "judge" ? "purple" : "blue")}>{task.type === "judge" ? "Judge" : "Act"}</span></div>
          <p>{task.goal}</p><ul>{task.success_criteria.map((criterion, position) => <li key={position}>{criterion}</li>)}</ul><small>最多 {task.max_cycles} 轮</small>
        </div></article>)}</div>
        <div className="plan-start"><Checkbox disabled={busy || dirty || !isLatest} checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)}>我已确认计划 assumptions、任务目标和成功标准</Checkbox>
          <Button block size="large" type="primary" icon={<RocketOutlined />} loading={start.isPending} disabled={busy || !valid || dirty || !confirmed || !isLatest}
            onClick={() => start.mutate({ body: { test_plan_id: plan.id, assumptions_confirmed: true } })}>启动运行</Button>
          {dirty && <p className="muted">保存为新计划版本后才能启动</p>}
        </div>
      </> : <Empty className="plan-empty" image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未生成计划" />}
      <div className="recent-runs"><div className="section-heading"><div><small>最近 5 次 · 按时间倒序</small><h3>历史运行 · 本用例</h3></div><Link to={"/runs?case=" + caseId}>查看全部 →</Link></div>
        {runs.error ? <Alert type="error" title="历史运行加载失败" /> : runs.data?.items.length ? runs.data.items.map((run) => <Link className="recent-run" to={"/runs/" + run.id + "/report"} key={run.id}>
          <div><span className="mono">{run.id.slice(0, 8)}</span><time>{formatTime(run.created_at)}</time><StatusBadge status={run.verdict ?? run.status} /></div><p>{runSummary(run)}</p>
        </Link>) : <p className="muted">暂无历史运行</p>}
      </div>
    </aside>
    <Modal title="编辑语义计划" open={editing} width={760} onCancel={() => setEditing(false)} footer={null} mask={{ closable: false }}>
      {draft && <Form layout="vertical" disabled={!canEdit}>
        <Form.Item label="计划标题"><Input value={draft.title} maxLength={200} onChange={(event) => updateDraft({ ...draft, title: event.target.value })} /></Form.Item>
        <Form.Item label="前置假设（每行一条）"><Input.TextArea value={draft.assumptions.join("\n")} onChange={(event) => updateDraft({ ...draft, assumptions: event.target.value ? event.target.value.split("\n") : [] })} /></Form.Item>
        <Form.Item label="准备步骤（每行一条）"><Input.TextArea value={draft.setup_steps.join("\n")} onChange={(event) => updateDraft({ ...draft, setup_steps: event.target.value ? event.target.value.split("\n") : [] })} /></Form.Item>
        {draft.tasks.map((task, index) => <section className="task-editor" key={index}><div className="task-editor-heading"><strong>任务 {index + 1}</strong><div>
          <Button aria-label={"上移任务 " + (index + 1)} icon={<ArrowUpOutlined />} disabled={!canEdit || index === 0} onClick={() => moveTask(index, -1)} />
          <Button aria-label={"下移任务 " + (index + 1)} icon={<ArrowDownOutlined />} disabled={!canEdit || index === draft.tasks.length - 1} onClick={() => moveTask(index, 1)} />
          <Button aria-label={"删除任务 " + (index + 1)} danger icon={<DeleteOutlined />} onClick={() => updateDraft({ ...draft, tasks: draft.tasks.filter((_, position) => position !== index) })} /></div></div>
          <div className="task-editor-options"><Form.Item label="类型"><Radio.Group value={task.type} onChange={(event) => updateTask(index, { type: event.target.value })}><Radio.Button value="act">Act</Radio.Button><Radio.Button value="judge">Judge</Radio.Button></Radio.Group></Form.Item>
            <Form.Item label="最大循环数"><InputNumber min={1} max={100} value={task.max_cycles} onChange={(value) => updateTask(index, { max_cycles: value ?? 1 })} /></Form.Item></div>
          <Form.Item label="任务标题"><Input value={task.title} maxLength={200} onChange={(event) => updateTask(index, { title: event.target.value })} /></Form.Item>
          <Form.Item label="目标"><Input.TextArea value={task.goal} onChange={(event) => updateTask(index, { goal: event.target.value })} /></Form.Item>
          <Form.Item label="成功标准（每行一条）"><Input.TextArea value={task.success_criteria.join("\n")} onChange={(event) => updateTask(index, { success_criteria: event.target.value.split("\n") })} /></Form.Item>
        </section>)}
        <Button block icon={<PlusOutlined />} onClick={() => updateDraft({ ...draft, tasks: [...draft.tasks, emptyTask()] })}>新增任务</Button>
        {draft.setup_steps.map((step, index) => <Button key={index} type="link" onClick={() => updateDraft({ ...draft, tasks: [...draft.tasks, emptyTask(step)] })}>将准备步骤 {index + 1} 转为任务</Button>)}
        {!valid && <Alert type="warning" title="请补全计划标题、任务目标、成功标准与循环上限" />}
        <div className="editor-footer"><Button disabled={busy} onClick={() => { if (plan) selectPlan(plan); }}>放弃修改</Button><Button type="primary" icon={<SaveOutlined />} disabled={!canEdit || !dirty || !valid} loading={revise.isPending}
          onClick={() => { if (plan) revise.mutate({ params: { path: { test_plan_id: plan.id } }, body: { content: draft } }); }}>保存新版本</Button></div>
      </Form>}
    </Modal>
  </>;
}
