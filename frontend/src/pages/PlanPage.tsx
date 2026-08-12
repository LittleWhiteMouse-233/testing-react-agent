import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  DeleteOutlined,
  PlusOutlined,
  RocketOutlined,
  SaveOutlined
} from "@ant-design/icons";
import { useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  Popconfirm,
  Radio,
  Row,
  Select,
  Space,
  Spin,
  Typography,
  message
} from "antd";
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { $api, apiErrorMessage } from "../api/client";
import type {
  TestPlan,
  TestPlanDraft,
  TestTaskDefinition
} from "../api/contracts";
import { emptyTask, isPlanDraftValid, toPlanDraft } from "../planDraft";

export default function PlanPage() {
  const { caseId = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<TestPlanDraft | null>(null);
  const [plan, setPlan] = useState<TestPlan | null>(null);
  const [deviceId, setDeviceId] = useState<string>();
  const [confirmed, setConfirmed] = useState(false);
  const [dirty, setDirty] = useState(false);

  const testCase = $api.useQuery("get", "/api/test-cases/{test_case_id}", {
    params: { path: { test_case_id: caseId } }
  });
  const plans = $api.useQuery("get", "/api/test-cases/{test_case_id}/plans", {
    params: { path: { test_case_id: caseId } }
  });
  const devices = $api.useQuery("get", "/api/devices");

  useEffect(() => {
    const latest = plans.data?.items[0];
    if (latest && !plan) {
      setPlan(latest);
      setDraft(toPlanDraft(latest));
    }
  }, [plans.data, plan]);
  useEffect(() => {
    const first = devices.data?.items.find((item) => item.health.available);
    if (first && !deviceId) setDeviceId(first.device_id);
  }, [devices.data, deviceId]);

  const acceptPlan = (value: TestPlan) => {
    setPlan(value);
    setDraft(toPlanDraft(value));
    setConfirmed(false);
    setDirty(false);
    void queryClient.invalidateQueries({
      queryKey: $api.queryOptions(
        "get",
        "/api/test-cases/{test_case_id}/plans",
        { params: { path: { test_case_id: caseId } } }
      ).queryKey
    });
  };
  const generate = $api.useMutation(
    "post",
    "/api/test-cases/{test_case_id}/plans",
    {
      onSuccess: (value) => {
        acceptPlan(value);
        message.success("计划已生成");
      },
      onError: (error) => message.error(apiErrorMessage(error, "计划生成失败"))
    }
  );
  const revise = $api.useMutation(
    "post",
    "/api/test-plans/{test_plan_id}/revisions",
    {
      onSuccess: (value) => {
        acceptPlan(value);
        message.success(`已保存为计划版本 ${value.version_number}`);
      },
      onError: (error) => message.error(apiErrorMessage(error, "计划保存失败"))
    }
  );
  const start = $api.useMutation("post", "/api/runs", {
    onSuccess: (run) => navigate(`/runs/${run.id}`),
    onError: (error) => {
      const errorMessage = apiErrorMessage(error, "运行启动失败");
      if (["active_run_exists", "test_plan_not_startable"].includes(error.code)) {
        message.warning(errorMessage);
      } else {
        message.error(errorMessage);
      }
    }
  });

  const generatePlan = () => {
    if (!deviceId) return;
    generate.mutate({
      params: { path: { test_case_id: caseId } },
      body: { device_id: deviceId }
    });
  };
  const revisePlan = () => {
    if (!draft || !plan) return;
    revise.mutate({
      params: { path: { test_plan_id: plan.id } },
      body: { content: draft }
    });
  };
  const startRun = () => {
    if (!plan || !deviceId) return;
    start.mutate({
      body: {
        test_plan_id: plan.id,
        device_id: deviceId,
        assumptions_confirmed: true
      }
    });
  };

  const valid = useMemo(() => isPlanDraftValid(draft), [draft]);
  const latestVersion = Math.max(0, ...(plans.data?.items.map((item) => item.version_number) ?? []));
  const isLatest = !!plan && plan.version_number >= latestVersion;

  const updateTask = (index: number, patch: Partial<TestTaskDefinition>) => {
    if (!draft) return;
    const tasks = [...draft.tasks];
    const task = tasks[index];
    if (!task) return;
    tasks[index] = { ...task, ...patch };
    setDraft({ ...draft, tasks });
    setDirty(true);
    setConfirmed(false);
  };
  const moveTask = (index: number, delta: number) => {
    if (!draft || index + delta < 0 || index + delta >= draft.tasks.length) return;
    const tasks = [...draft.tasks];
    const task = tasks[index];
    const destinationTask = tasks[index + delta];
    if (!task || !destinationTask) return;
    tasks[index] = destinationTask;
    tasks[index + delta] = task;
    setDraft({ ...draft, tasks });
    setDirty(true);
    setConfirmed(false);
  };

  if (testCase.isLoading || plans.isLoading || devices.isLoading) return <Spin />;
  if (!draft || !plan) {
    return (
      <Card>
        <Empty description="尚未生成计划">
          <Select
            style={{ width: 300, marginRight: 12 }}
            value={deviceId}
            onChange={setDeviceId}
            options={devices.data?.items.map((device) => ({
              value: device.device_id,
              label: `${device.device_id} · ${device.provider}`,
              disabled: !device.health.available
            }))}
            placeholder="选择设备"
          />
          <Button type="primary" disabled={!deviceId} loading={generate.isPending} onClick={generatePlan}>生成计划</Button>
        </Empty>
      </Card>
    );
  }

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card>
        <div className="toolbar">
          <div>
            <Typography.Title level={3} style={{ margin: 0 }}>{testCase.data?.content.name}</Typography.Title>
            <Typography.Text type="secondary">版本 {plan.version_number} · {plan.origin}</Typography.Text>
          </div>
          <Space wrap>
            <Button disabled={!deviceId} loading={generate.isPending} onClick={generatePlan}>重新规划</Button>
            <Button icon={<SaveOutlined />} disabled={!valid || !dirty || !isLatest} loading={revise.isPending} onClick={revisePlan}>保存新版本</Button>
          </Space>
        </div>
        <Typography.Paragraph>{testCase.data?.content.source_text}</Typography.Paragraph>
      </Card>

      <Card title="计划信息">
        <Form.Item label="标题" required>
          <Input value={draft.title} onChange={(event) => { setDraft({ ...draft, title: event.target.value }); setDirty(true); setConfirmed(false); }} />
        </Form.Item>
        <Typography.Title level={5}>Assumptions</Typography.Title>
        <List dataSource={draft.assumptions} locale={{ emptyText: "无" }} renderItem={(item) => <List.Item>{item}</List.Item>} />
        <Typography.Title level={5}>Setup steps</Typography.Title>
        <List
          dataSource={draft.setup_steps}
          locale={{ emptyText: "无" }}
          renderItem={(item) => (
            <List.Item actions={[<Button key="promote" onClick={() => { setDraft({ ...draft, tasks: [...draft.tasks, emptyTask(item)] }); setDirty(true); setConfirmed(false); }}>转为任务</Button>]}>{item}</List.Item>
          )}
        />
      </Card>

      <Card title="有序任务">
        {draft.tasks.map((task, index) => (
          <Card
            key={index}
            size="small"
            className="task-card"
            title={`${index + 1}. ${task.title || "未命名任务"}`}
            extra={(
              <Space>
                <Button size="small" icon={<ArrowUpOutlined />} disabled={index === 0} onClick={() => moveTask(index, -1)} />
                <Button size="small" icon={<ArrowDownOutlined />} disabled={index === draft.tasks.length - 1} onClick={() => moveTask(index, 1)} />
                <Popconfirm title="删除此任务？" onConfirm={() => { setDraft({ ...draft, tasks: draft.tasks.filter((_, taskIndex) => taskIndex !== index) }); setDirty(true); setConfirmed(false); }}>
                  <Button size="small" danger icon={<DeleteOutlined />} />
                </Popconfirm>
              </Space>
            )}
          >
            <div className="task-grid">
              <Form.Item label="类型" required>
                <Radio.Group value={task.type} onChange={(event) => updateTask(index, { type: event.target.value as TestTaskDefinition["type"] })}>
                  <Radio.Button value="act">Act</Radio.Button>
                  <Radio.Button value="judge">Judge</Radio.Button>
                </Radio.Group>
              </Form.Item>
              <Form.Item label="最大 cycles" required>
                <InputNumber min={1} max={100} value={task.max_cycles} onChange={(value) => updateTask(index, { max_cycles: value ?? 10 })} />
              </Form.Item>
            </div>
            <Form.Item label="标题" required><Input value={task.title} onChange={(event) => updateTask(index, { title: event.target.value })} /></Form.Item>
            <Form.Item label="目标" required><Input.TextArea value={task.goal} onChange={(event) => updateTask(index, { goal: event.target.value })} /></Form.Item>
            <Form.Item label="成功标准（每行一条）" required>
              <Input.TextArea rows={3} value={task.success_criteria.join("\n")} onChange={(event) => updateTask(index, { success_criteria: event.target.value.split("\n") })} />
            </Form.Item>
          </Card>
        ))}
        <Button block icon={<PlusOutlined />} onClick={() => { setDraft({ ...draft, tasks: [...draft.tasks, emptyTask()] }); setDirty(true); setConfirmed(false); }}>新增任务</Button>
      </Card>

      <Card>
        {!valid && <Alert type="warning" showIcon message="请补全计划标题及所有任务的目标、成功标准与 cycle 上限" style={{ marginBottom: 16 }} />}
        {dirty && valid && <Alert type="info" showIcon message="保存为新计划版本后才能启动" style={{ marginBottom: 16 }} />}
        {!isLatest && <Alert type="warning" showIcon message="历史计划仅供回溯，不能启动" style={{ marginBottom: 16 }} />}
        <Checkbox checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)}>
          我已确认计划 assumptions、任务目标和成功标准
        </Checkbox>
        <Row gutter={16} align="middle" style={{ marginTop: 16 }}>
          <Col flex="auto">
            <Select
              style={{ width: "100%" }}
              value={deviceId}
              onChange={setDeviceId}
              options={devices.data?.items.map((device) => ({
                value: device.device_id,
                label: `${device.device_id} · ${device.provider}`,
                disabled: !device.health.available
              }))}
              placeholder="选择设备"
            />
          </Col>
          <Col>
            <Button type="primary" size="large" icon={<RocketOutlined />} disabled={!valid || dirty || !confirmed || !deviceId || !isLatest} loading={start.isPending} onClick={startRun}>
              启动运行
            </Button>
          </Col>
        </Row>
      </Card>
    </Space>
  );
}
