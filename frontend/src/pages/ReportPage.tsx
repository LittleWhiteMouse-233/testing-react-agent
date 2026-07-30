import { DownloadOutlined } from "@ant-design/icons";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Col, Descriptions, Image, Row, Space, Spin, Statistic, Tag, Typography, message } from "antd";
import { useParams } from "react-router-dom";
import { api, apiUrl } from "../api/client";
import type { Artifact, Report } from "../types";

const taskResultKeys = ["passed", "failed", "blocked", "skipped"] as const;

export default function ReportPage() {
  const { runId = "" } = useParams();
  const report = useQuery({ queryKey: ["report", runId], queryFn: () => api<Report>(`/runs/${runId}/report`) });
  const exportReport = useMutation({
    mutationFn: (format: "json" | "html") => api<Artifact>(`/runs/${runId}/exports`, { method: "POST", body: JSON.stringify({ format }) }),
    onSuccess: (artifact) => {
      window.location.assign(apiUrl(`/artifacts/${artifact.id}`));
      message.success("导出文件已创建");
    },
    onError: (error: Error) => message.error(error.message)
  });
  if (report.isLoading) return <Spin />;
  if (!report.data) return <Alert type="error" message={(report.error as Error)?.message ?? "报告不存在"} />;
  const data = report.data;
  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card>
        <div className="toolbar">
          <div>
            <Typography.Text type="secondary">确定性运行报告</Typography.Text>
            <Typography.Title level={2} className={`result-${data.run.overall_result}`} style={{ margin: 0 }}>{data.run.overall_result}</Typography.Title>
          </div>
          <Space>
            <Button icon={<DownloadOutlined />} loading={exportReport.isPending} onClick={() => exportReport.mutate("json")}>JSON</Button>
            <Button type="primary" icon={<DownloadOutlined />} loading={exportReport.isPending} onClick={() => exportReport.mutate("html")}>HTML</Button>
          </Space>
        </div>
        <Descriptions column={{ xs: 1, md: 2 }}>
          <Descriptions.Item label="用例">{data.snapshot.test_case?.name}</Descriptions.Item>
          <Descriptions.Item label="设备">{data.run.device_id}</Descriptions.Item>
          <Descriptions.Item label="计划修订">{data.snapshot.plan_revision?.revision}</Descriptions.Item>
          <Descriptions.Item label="已确认 assumptions">{data.snapshot.confirmed_assumptions?.join("；") || "无"}</Descriptions.Item>
        </Descriptions>
        <Row gutter={16}>
          {taskResultKeys.map((key) => <Col key={key} xs={12} md={6}><Statistic title={key} value={data.summary[key]} /></Col>)}
        </Row>
      </Card>
      {data.tasks.map((task) => (
        <Card key={task.id} title={`${task.task_index + 1}. ${task.definition?.title}`} extra={<Tag>{task.status}</Tag>}>
          <Typography.Paragraph><strong>目标：</strong>{task.definition?.goal}</Typography.Paragraph>
          <Typography.Paragraph><strong>判定标准：</strong>{task.definition?.success_criteria?.join("；")}</Typography.Paragraph>
          <Typography.Paragraph><strong>结论：</strong>{task.outcome?.summary ?? "无"}（{task.cycle_count} cycles）</Typography.Paragraph>
          {task.outcome && <Typography.Paragraph><strong>原因：</strong>{task.outcome.reason_code}</Typography.Paragraph>}
          <Image.PreviewGroup>
            <Space wrap>{task.evidence.map((item) => <Image key={item.id} width={240} src={apiUrl(`/artifacts/${item.id}`)} />)}</Space>
          </Image.PreviewGroup>
        </Card>
      ))}
    </Space>
  );
}
