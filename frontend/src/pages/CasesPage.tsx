import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Card, Form, Input, Modal, Space, Table, Tag, Typography, message } from "antd";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Run, TestCase } from "../types";

export default function CasesPage({ runsOnly = false }: { runsOnly?: boolean }) {
  const navigate = useNavigate();
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  const cases = useQuery({
    queryKey: ["cases"],
    queryFn: () => api<{ items: TestCase[]; total: number }>("/test-cases")
  });
  const runs = useQuery({
    queryKey: ["runs"],
    queryFn: () => api<{ items: Run[]; total: number }>("/runs"),
    refetchInterval: (query) =>
      query.state.data?.items.some((item) => ["pending", "running"].includes(item.status)) ? 1500 : false
  });
  const create = useMutation({
    mutationFn: (value: { name: string; source_text: string }) =>
      api<TestCase>("/test-cases", { method: "POST", body: JSON.stringify(value) }),
    onSuccess: (item) => {
      setOpen(false);
      form.resetFields();
      client.invalidateQueries({ queryKey: ["cases"] });
      navigate(`/cases/${item.id}/plan`);
    },
    onError: (error: Error) => message.error(error.message)
  });

  if (runsOnly) {
    return (
      <Card>
        <div className="toolbar">
          <Typography.Title level={4} style={{ margin: 0 }}>历史运行</Typography.Title>
          <Button icon={<ReloadOutlined />} onClick={() => runs.refetch()}>刷新</Button>
        </div>
        <Table
          rowKey="id"
          loading={runs.isLoading}
          dataSource={runs.data?.items}
          pagination={{ pageSize: 20 }}
          columns={[
            { title: "运行 ID", dataIndex: "id", render: (id) => <Link to={`/runs/${id}`}>{id.slice(0, 8)}</Link> },
            { title: "设备", dataIndex: "device_id" },
            { title: "状态", dataIndex: "status", render: (value) => <Tag>{value}</Tag> },
            { title: "结果", dataIndex: "overall_result", render: (value) => value ? <Tag color={value === "PASS" ? "green" : value === "FAIL" ? "red" : "orange"}>{value}</Tag> : "—" },
            { title: "创建时间", dataIndex: "created_at", render: (value) => new Date(value).toLocaleString() }
          ]}
        />
      </Card>
    );
  }

  return (
    <>
      <Card>
        <div className="toolbar">
          <div>
            <Typography.Title level={4} style={{ margin: 0 }}>测试用例</Typography.Title>
            <Typography.Text type="secondary">从自然语言用例生成可确认的执行计划。</Typography.Text>
          </div>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>新建用例</Button>
        </div>
        <Table
          rowKey="id"
          loading={cases.isLoading}
          dataSource={cases.data?.items}
          pagination={{ pageSize: 20 }}
          columns={[
            { title: "名称", dataIndex: "name" },
            { title: "用例正文", dataIndex: "source_text", ellipsis: true },
            { title: "创建时间", dataIndex: "created_at", width: 190, render: (value) => new Date(value).toLocaleString() },
            {
              title: "操作",
              width: 150,
              render: (_, item) => (
                <Space><Link to={`/cases/${item.id}/plan`}>计划与运行</Link></Space>
              )
            }
          ]}
        />
      </Card>
      <Modal title="新建测试用例" open={open} onCancel={() => setOpen(false)} onOk={() => form.submit()} confirmLoading={create.isPending}>
        <Form form={form} layout="vertical" onFinish={(value) => create.mutate(value)}>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="source_text" label="自然语言用例" rules={[{ required: true }]}>
            <Input.TextArea rows={7} placeholder="包含前置条件、操作步骤和预期结果" />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}

