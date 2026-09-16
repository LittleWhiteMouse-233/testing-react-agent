import { PlusOutlined } from "@ant-design/icons";
import { useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Empty, Form, Input, Modal, Pagination, Spin, message } from "antd";
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { $api, apiErrorMessage } from "../api/client";
import type { TestCaseCreateRequest } from "../api/contracts";
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
  const [form] = Form.useForm<TestCaseCreateRequest>();
  const cases = $api.useQuery("get", "/api/test-cases", { params: { query: { search, limit: 12, offset: (page - 1) * 12 } } });
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
          cases.data?.items.length ? cases.data.items.map((testCase) => <Link key={testCase.id}
            className={"case-card " + (caseId === testCase.id ? "selected" : "")} to={"/cases/" + testCase.id + "/plan?" + params}>
            <div className="case-card-meta"><span className="mono" title={testCase.id}>{testCase.id.slice(0, 8)}</span>
              <span className={"pill " + (testCase.latest_plan_version ? "blue" : "")}>{testCase.latest_plan_version ? "已规划 · v" + testCase.latest_plan_version : "未规划"}</span></div>
            <h3>{testCase.content.name}</h3><p>{testCase.content.source_text}</p>
          </Link>) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={search ? "没有匹配的用例" : "从第一个用例开始"} />}
        </div>
        <Pagination simple current={page} pageSize={12} total={cases.data?.total ?? 0} onChange={(next) => setParams({ ...(search ? { search } : {}), page: String(next) })} />
      </aside>
      {caseId ? <PlanPage key={caseId} /> : <div className="planning-welcome"><span className="welcome-mark">T</span><h2>从测试意图开始</h2><p>选择左侧用例，或创建一个新用例。<br />查看并确认计划后，即可开始执行。</p><Button type="primary" onClick={() => setOpen(true)}>新建用例</Button></div>}
    </div>
    <Modal title="新建测试用例" open={open} onCancel={() => setOpen(false)} onOk={() => form.submit()} confirmLoading={create.isPending} destroyOnHidden>
      <Form form={form} layout="vertical" onFinish={(value) => create.mutate({ body: value })}>
        <Form.Item name="name" label="名称" rules={[{ required: true, whitespace: true, max: 200 }]}><Input maxLength={200} /></Form.Item>
        <Form.Item name="source_text" label="自然语言用例" rules={[{ required: true, whitespace: true }]}><Input.TextArea rows={8} placeholder="描述前置条件、操作目标和预期结果" /></Form.Item>
      </Form>
    </Modal>
  </>;
}
