import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Card,
  Form,
  Input,
  Modal,
  Space,
  Table,
  Tag,
  Typography,
  message
} from "antd";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { $api, apiErrorMessage } from "../api/client";
import type { TestCase, TestCaseCreateRequest } from "../api/contracts";

export default function CasesPage({ runsOnly = false }: { runsOnly?: boolean }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm<TestCaseCreateRequest>();
  const cases = $api.useQuery("get", "/api/test-cases");
  const runs = $api.useQuery("get", "/api/runs", {}, {
    refetchInterval: (query) =>
      query.state.data?.items.some((run) => run.status !== "finished") ? 1500 : false
  });
  const create = $api.useMutation("post", "/api/test-cases", {
    onSuccess: (testCase) => {
      setOpen(false);
      form.resetFields();
      void queryClient.invalidateQueries({
        queryKey: $api.queryOptions("get", "/api/test-cases").queryKey
      });
      navigate(`/cases/${testCase.id}/plan`);
    },
    onError: (error) => message.error(apiErrorMessage(error, "请求失败"))
  });

  if (runsOnly) {
    return (
      <Card>
        <div className="toolbar">
          <Typography.Title level={4} style={{ margin: 0 }}>历史运行</Typography.Title>
          <Button icon={<ReloadOutlined />} onClick={() => void runs.refetch()}>刷新</Button>
        </div>
        <Table
          rowKey="id"
          loading={runs.isLoading}
          dataSource={runs.data?.items}
          pagination={{ pageSize: 20 }}
          columns={[
            {
              title: "运行 ID",
              dataIndex: "id",
              render: (id: string) => <Link to={`/runs/${id}`}>{id.slice(0, 8)}</Link>
            },
            { title: "设备", dataIndex: "device_id" },
            { title: "状态", dataIndex: "status", render: (value: string) => <Tag>{value}</Tag> },
            {
              title: "结果",
              dataIndex: "verdict",
              render: (value: string | null) => value ? <Tag color={value === "PASS" ? "green" : value === "FAIL" ? "red" : "orange"}>{value}</Tag> : "—"
            },
            {
              title: "创建时间",
              dataIndex: "created_at",
              render: (value: string) => new Date(value).toLocaleString()
            }
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
            <Typography.Text type="secondary">从自然语言用例生成可确认的语义任务计划。</Typography.Text>
          </div>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>新建用例</Button>
        </div>
        <Table
          rowKey="id"
          loading={cases.isLoading}
          dataSource={cases.data?.items}
          pagination={{ pageSize: 20 }}
          columns={[
            { title: "名称", render: (_: unknown, item: TestCase) => item.content.name },
            { title: "用例正文", ellipsis: true, render: (_: unknown, item: TestCase) => item.content.source_text },
            { title: "创建时间", dataIndex: "created_at", width: 190, render: (value: string) => new Date(value).toLocaleString() },
            { title: "操作", width: 150, render: (_: unknown, item: TestCase) => <Space><Link to={`/cases/${item.id}/plan`}>计划与运行</Link></Space> }
          ]}
        />
      </Card>
      <Modal
        title="新建测试用例"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={create.isPending}
      >
        <Form
          form={form}
          layout="vertical"
          onFinish={(value) => create.mutate({ body: value })}
        >
          <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="source_text" label="自然语言用例" rules={[{ required: true }]}>
            <Input.TextArea rows={7} placeholder="包含前置条件、目标和预期结果" />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
