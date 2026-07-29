import { ArrowDownOutlined, ArrowUpOutlined, DeleteOutlined, PlusOutlined, RocketOutlined, SaveOutlined } from "@ant-design/icons";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Checkbox, Col, Divider, Empty, Form, Input, InputNumber, List, Popconfirm, Radio, Row, Select, Space, Spin, Typography, message } from "antd";
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { PlanOutput, PlanRevision, Task, TestCase } from "../types";

const newTask = (index: number, setup = ""): Task => ({
  task_id: `task-${Date.now()}-${index}`,
  type: "act",
  title: setup || "新任务",
  goal: setup,
  success_criteria: [""],
  max_cycles: 10
});

export default function PlanPage() {
  const { caseId = "" } = useParams();
  const navigate = useNavigate();
  const [plan, setPlan] = useState<PlanOutput | null>(null);
  const [revision, setRevision] = useState<PlanRevision | null>(null);
  const [confirmed, setConfirmed] = useState<string[]>([]);
  const [deviceId, setDeviceId] = useState<string>();
  const [dirty, setDirty] = useState(false);

  const testCase = useQuery({ queryKey: ["case", caseId], queryFn: () => api<TestCase>(`/test-cases/${caseId}`) });
  const revisions = useQuery({
    queryKey: ["plans", caseId],
    queryFn: () => api<{ items: PlanRevision[] }>(`/test-cases/${caseId}/plans`)
  });
  const devices = useQuery({
    queryKey: ["devices"],
    queryFn: () => api<{ items: Array<{ id: string; type: string; health: { available: boolean } }> }>("/devices")
  });
  useEffect(() => {
    const latest = revisions.data?.items[0];
    if (latest && !revision) {
      setRevision(latest);
      setPlan(structuredClone(latest.plan));
      setDirty(false);
    }
  }, [revisions.data, revision]);
  useEffect(() => {
    const first = devices.data?.items.find((item) => item.health.available);
    if (first && !deviceId) setDeviceId(first.id);
  }, [devices.data, deviceId]);

  const generate = useMutation({
    mutationFn: () => api<PlanRevision>(`/test-cases/${caseId}/plans`, { method: "POST", body: "{}" }),
    onSuccess: (item) => {
      setRevision(item);
      setPlan(structuredClone(item.plan));
      setConfirmed([]);
      setDirty(false);
      message.success("计划已生成");
    },
    onError: (error: Error) => message.error(error.message)
  });
  const save = useMutation({
    mutationFn: () => api<PlanRevision>(`/plan-revisions/${revision!.id}/revisions`, {
      method: "POST", body: JSON.stringify({ plan })
    }),
    onSuccess: (item) => {
      setRevision(item);
      setPlan(structuredClone(item.plan));
      setDirty(false);
      message.success(`已保存为修订版 ${item.revision}`);
    },
    onError: (error: Error) => message.error(error.message)
  });
  const start = useMutation({
    mutationFn: () => api<{ id: string }>("/runs", {
      method: "POST",
      body: JSON.stringify({
        plan_revision_id: revision!.id,
        device_id: deviceId,
        confirmed_assumptions: confirmed
      })
    }),
    onSuccess: (run) => navigate(`/runs/${run.id}`),
    onError: (error: Error) => {
      if (error instanceof ApiError && error.status === 409) message.warning("已有运行正在执行，请先查看或等待完成");
      else message.error(error.message);
    }
  });

  const valid = useMemo(() => !!plan && plan.tasks.length > 0 && plan.tasks.every(
    (task) => task.title.trim() && task.goal.trim() && task.success_criteria.length > 0 &&
      task.success_criteria.every((item) => item.trim()) && task.max_cycles >= 1
  ), [plan]);
  const assumptionsReady = plan ? confirmed.length === plan.assumptions.length : false;

  const updateTask = (index: number, patch: Partial<Task>) => {
    if (!plan) return;
    const tasks = [...plan.tasks];
    tasks[index] = { ...tasks[index], ...patch };
    setPlan({ ...plan, tasks });
    setDirty(true);
  };
  const moveTask = (index: number, delta: number) => {
    if (!plan || index + delta < 0 || index + delta >= plan.tasks.length) return;
    const tasks = [...plan.tasks];
    [tasks[index], tasks[index + delta]] = [tasks[index + delta], tasks[index]];
    setPlan({ ...plan, tasks });
    setDirty(true);
  };

  if (testCase.isLoading || revisions.isLoading) return <Spin />;
  if (!plan) return (
    <Card>
      <Empty description="尚未生成计划">
        <Button type="primary" loading={generate.isPending} onClick={() => generate.mutate()}>使用模型生成计划</Button>
      </Empty>
    </Card>
  );

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card>
        <div className="toolbar">
          <div>
            <Typography.Title level={3} style={{ margin: 0 }}>{testCase.data?.name}</Typography.Title>
            <Typography.Text type="secondary">修订版 {revision?.revision} · {revision?.source}</Typography.Text>
          </div>
          <Space wrap>
            <Button onClick={() => generate.mutate()} loading={generate.isPending}>重新规划</Button>
            <Button icon={<SaveOutlined />} disabled={!valid || !dirty} loading={save.isPending} onClick={() => save.mutate()}>保存新修订</Button>
          </Space>
        </div>
        <Typography.Paragraph>{testCase.data?.source_text}</Typography.Paragraph>
      </Card>

      <Card title="前置条件">
        <Typography.Title level={5}>Assumptions（必须逐项确认）</Typography.Title>
        {plan.assumptions.length ? (
          <Checkbox.Group value={confirmed} onChange={(values) => setConfirmed(values as string[])}>
            <Space direction="vertical">{plan.assumptions.map((item) => <Checkbox key={item} value={item}>{item}</Checkbox>)}</Space>
          </Checkbox.Group>
        ) : <Alert type="success" showIcon message="没有需要人工确认的 assumptions" />}
        <Divider />
        <Typography.Title level={5}>Setup steps（仅提示，可转为 Act Task）</Typography.Title>
        <List
          dataSource={plan.setup_steps}
          locale={{ emptyText: "无" }}
          renderItem={(item) => (
            <List.Item actions={[<Button key="promote" onClick={() => { setPlan({ ...plan, tasks: [...plan.tasks, newTask(plan.tasks.length, item)] }); setDirty(true); }}>转为任务</Button>]}>
              {item}
            </List.Item>
          )}
        />
      </Card>

      <Card title="有序任务">
        {plan.tasks.map((task, index) => (
          <Card
            key={task.task_id}
            size="small"
            className="task-card"
            title={`${index + 1}. ${task.title || "未命名任务"}`}
            extra={
              <Space>
                <Button size="small" icon={<ArrowUpOutlined />} disabled={index === 0} onClick={() => moveTask(index, -1)} />
                <Button size="small" icon={<ArrowDownOutlined />} disabled={index === plan.tasks.length - 1} onClick={() => moveTask(index, 1)} />
                <Popconfirm title="删除此任务？" onConfirm={() => { setPlan({ ...plan, tasks: plan.tasks.filter((_, i) => i !== index) }); setDirty(true); }}>
                  <Button size="small" danger icon={<DeleteOutlined />} />
                </Popconfirm>
              </Space>
            }
          >
            <div className="task-grid">
              <Form.Item label="类型" required><Radio.Group value={task.type} onChange={(e) => updateTask(index, { type: e.target.value })}><Radio.Button value="act">Act</Radio.Button><Radio.Button value="judge">Judge</Radio.Button></Radio.Group></Form.Item>
              <Form.Item label="最大 cycles" required><InputNumber min={1} max={100} value={task.max_cycles} onChange={(value) => updateTask(index, { max_cycles: value ?? 10 })} /></Form.Item>
            </div>
            <Form.Item label="标题" required><Input value={task.title} onChange={(e) => updateTask(index, { title: e.target.value })} /></Form.Item>
            <Form.Item label="目标" required><Input.TextArea value={task.goal} onChange={(e) => updateTask(index, { goal: e.target.value })} /></Form.Item>
            <Form.Item label="成功标准（每行一条）" required>
              <Input.TextArea rows={3} value={task.success_criteria.join("\n")} onChange={(e) => updateTask(index, { success_criteria: e.target.value.split("\n") })} />
            </Form.Item>
          </Card>
        ))}
        <Button block icon={<PlusOutlined />} onClick={() => { setPlan({ ...plan, tasks: [...plan.tasks, newTask(plan.tasks.length)] }); setDirty(true); }}>新增任务</Button>
      </Card>

      <Card>
        {!valid && <Alert type="warning" showIcon message="请补全所有任务的标题、目标、成功标准和 cycle 上限" style={{ marginBottom: 16 }} />}
        {dirty && valid && <Alert type="info" showIcon message="计划有未保存修改；保存为新修订版后才能启动" style={{ marginBottom: 16 }} />}
        <Row gutter={16} align="middle">
          <Col flex="auto">
            <Select
              style={{ width: "100%" }}
              value={deviceId}
              onChange={setDeviceId}
              options={devices.data?.items.map((item) => ({ value: item.id, label: `${item.id} · ${item.type}`, disabled: !item.health.available }))}
              placeholder="选择设备"
            />
          </Col>
          <Col>
            <Button type="primary" size="large" icon={<RocketOutlined />} disabled={!valid || dirty || !assumptionsReady || !deviceId} loading={start.isPending} onClick={() => start.mutate()}>
              启动运行
            </Button>
          </Col>
        </Row>
      </Card>
    </Space>
  );
}
