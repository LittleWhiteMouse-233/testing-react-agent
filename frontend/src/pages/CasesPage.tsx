import { InboxOutlined, PlusOutlined } from "@ant-design/icons";
import { useMutationState, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Empty, Form, Input, Modal, Pagination, Spin, Tooltip, message } from "antd";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { $api, apiErrorMessage } from "../api/client";
import type { TestCase, TestCaseCreateRequest } from "../api/contracts";
import PlanPage from "./PlanPage";
import { parsePage } from "../pageParams";

export default function CasesPage() {
  const { caseId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const search = params.get("search") ?? "";
  const page = parsePage(params.get("page"), 12);
  const [open, setOpen] = useState(false);
  const [archiveTarget, setArchiveTarget] = useState<TestCase | null>(null);
  const [form] = Form.useForm<TestCaseCreateRequest>();
  const cases = $api.useQuery("get", "/api/test-cases", { params: { query: { search, limit: 12, offset: (page - 1) * 12 } } });
  // Case-consuming mutations opt into this shared UI guard through their meta.
  const occupiedCases = useMutationState({ filters: { status: "pending" }, select: (mutation) => mutation.options.meta?.testCaseId });
  const activeRuns = $api.useQuery("get", "/api/runs", { params: { query: { status: ["pending", "running"], limit: 1 } } }, { refetchInterval: 3000 });
  const cannotArchive = (id: string) => occupiedCases.includes(id) || !!activeRuns.data?.items.some((run) => run.test_case_id === id);
  useEffect(() => {
    if (!cases.data) return;
    const lastPage = Math.max(1, Math.ceil(cases.data.total / 12));
    if (page > lastPage) setParams({ ...(search ? { search } : {}), page: String(lastPage) }, { replace: true });
  }, [cases.data, page, search, setParams]);
  const archive = $api.useMutation("post", "/api/test-cases/{test_case_id}/archive", {
    onSuccess: async (_, variables) => {
      const archivedId = variables.params.path.test_case_id;
      setArchiveTarget(null);
      if (caseId === archivedId) navigate("/?" + params, { replace: true });
      for (const path of ["/api/test-cases/{test_case_id}", "/api/test-cases/{test_case_id}/plans"] as const) {
        const queryKey = $api.queryOptions("get", path, { params: { path: { test_case_id: archivedId } } }).queryKey;
        await queryClient.cancelQueries({ queryKey });
        queryClient.removeQueries({ queryKey });
      }
      for (const path of ["/api/test-cases", "/api/runs", "/api/runs/test-cases", "/api/runs/{test_run_id}"]) {
        void queryClient.invalidateQueries({ queryKey: ["get", path] });
      }
      message.success("用例已归档，历史运行与证据保留");
    },
    onError: (error) => message.error(apiErrorMessage(error, "归档失败"))
  });
  const create = $api.useMutation("post", "/api/test-cases", {
    onSuccess: (testCase) => {
      setOpen(false); form.resetFields();
      void queryClient.invalidateQueries({ queryKey: ["get", "/api/test-cases"] });
      navigate("/cases/" + testCase.id + "/plan");
    }, onError: (error) => message.error(apiErrorMessage(error, "创建失败"))
  });
  return <>
    <header className="page-heading"><div><h1>用例规划</h1><p>将自然语言用例转为可确认的语义任务 · {cases.data?.total ?? "—"} 条用例</p></div>
      <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>新建用例</Button>
    </header>
    <div className="planning-layout">
      <aside className="case-library"><div className="section-heading"><div><small>用例库</small><h2>测试用例</h2></div><span>{cases.data?.total ?? 0} 条</span></div>
        <Input.Search key={search} aria-label="搜索用例" placeholder="搜索用例编号或关键字" defaultValue={search}
          onSearch={(value) => setParams(value ? { search: value } : {})} allowClear />
        <div className="case-list">{cases.isLoading ? <Spin /> : cases.error ? <Alert type="error" title={apiErrorMessage(cases.error, "用例加载失败")} action={<Button onClick={() => void cases.refetch()}>重试</Button>} /> :
          cases.data?.items.length ? cases.data.items.map((testCase) => <article key={testCase.id}
            className={"case-card " + (caseId === testCase.id ? "selected" : "")}>
            <Link className="case-card-link" to={"/cases/" + testCase.id + "/plan?" + params}>
            <div className="case-card-meta"><span className="mono" title={testCase.id}>{testCase.id.slice(0, 8)}</span>
              <span className={"pill " + (testCase.latest_plan_version ? "blue" : "")}>{testCase.latest_plan_version ? "已规划 · v" + testCase.latest_plan_version : "未规划"}</span></div>
            <h3>{testCase.content.name}</h3><p>{testCase.content.source_text}</p>
            </Link><Tooltip title="归档用例"><Button className="case-archive-button" type="text" icon={<InboxOutlined />} aria-label={"归档用例：" + testCase.content.name} disabled={archive.isPending || cannotArchive(testCase.id)} onClick={() => setArchiveTarget(testCase)} /></Tooltip>
          </article>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={search ? "没有匹配的用例" : "从第一个用例开始"} />}
        </div>
        <Pagination simple current={page} pageSize={12} total={cases.data?.total ?? 0} onChange={(next) => setParams({ ...(search ? { search } : {}), page: String(next) })} />
      </aside>
      {caseId ? <PlanPage key={caseId} archiving={archiveTarget?.id === caseId} /> : <div className="planning-welcome"><span className="welcome-mark">T</span><h2>从测试意图开始</h2><p>选择左侧用例，或创建一个新用例。<br />查看并确认计划后，即可开始执行。</p><Button type="primary" onClick={() => setOpen(true)}>新建用例</Button></div>}
    </div>
    <Modal title="归档用例" open={!!archiveTarget} okText="归档用例" cancelText="取消" confirmLoading={archive.isPending}
      okButtonProps={{ className: "case-archive-confirm", disabled: !!archiveTarget && cannotArchive(archiveTarget.id) }} cancelButtonProps={{ disabled: archive.isPending }} closable={!archive.isPending} mask={{ closable: !archive.isPending }}
      onCancel={() => { if (!archive.isPending) setArchiveTarget(null); }} onOk={() => { if (archiveTarget) archive.mutate({ params: { path: { test_case_id: archiveTarget.id } } }); }}>
      <p>归档“{archiveTarget?.content.name}”？</p><p>归档后从规划页移除，历史运行与证据保留。</p>
    </Modal>
    <Modal title="新建测试用例" open={open} onCancel={() => setOpen(false)} onOk={() => form.submit()} confirmLoading={create.isPending} destroyOnHidden>
      <Form form={form} layout="vertical" onFinish={(value) => create.mutate({ body: value })}>
        <Form.Item name="name" label="名称" rules={[{ required: true, whitespace: true, max: 200 }]}><Input maxLength={200} /></Form.Item>
        <Form.Item name="source_text" label="自然语言用例" rules={[{ required: true, whitespace: true }]}><Input.TextArea rows={8} placeholder="描述前置条件、操作目标和预期结果" /></Form.Item>
      </Form>
    </Modal>
  </>;
}
