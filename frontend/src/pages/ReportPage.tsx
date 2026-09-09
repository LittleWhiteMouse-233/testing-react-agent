import { DownloadOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Image,
  Row,
  Space,
  Spin,
  Statistic,
  Tag,
  Typography,
  message
} from "antd";
import { useParams } from "react-router-dom";
import { $api, apiErrorMessage, artifactUrl } from "../api/client";
import type { Artifact } from "../api/contracts";

const terminalStatuses = ["passed", "failed", "blocked", "skipped", "cancelled"] as const;

export default function ReportPage() {
  const { runId = "" } = useParams();
  const report = $api.useQuery("get", "/api/runs/{test_run_id}/report", {
    params: { path: { test_run_id: runId } }
  });
  const exportReport = $api.useMutation(
    "post",
    "/api/runs/{test_run_id}/exports",
    {
      onSuccess: (artifact) => {
        window.location.assign(artifactUrl(artifact.id));
        message.success("导出文件已创建");
      },
      onError: (error) => message.error(apiErrorMessage(error, "报告导出失败"))
    }
  );
  if (report.isLoading) return <Spin />;
  if (!report.data) return <Alert type="error" message={apiErrorMessage(report.error, "报告不存在")} />;

  const data = report.data;
  const counts = Object.fromEntries(
    terminalStatuses.map((status) => [
      status,
      data.detail.task_runs.filter((task) => task.status === status).length
    ])
  ) as Record<(typeof terminalStatuses)[number], number>;
  const artifacts = new Map(data.artifacts.map((artifact) => [artifact.id, artifact]));

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card>
        <div className="toolbar">
          <div>
            <Typography.Text type="secondary">确定性运行报告</Typography.Text>
            <Typography.Title level={2} className={`result-${data.detail.run.verdict}`} style={{ margin: 0 }}>{data.detail.run.verdict}</Typography.Title>
          </div>
          <Space>
            <Button icon={<DownloadOutlined />} loading={exportReport.isPending} onClick={() => exportReport.mutate({ params: { path: { test_run_id: runId } }, body: { format: "json" } })}>JSON</Button>
            <Button type="primary" icon={<DownloadOutlined />} loading={exportReport.isPending} onClick={() => exportReport.mutate({ params: { path: { test_run_id: runId } }, body: { format: "html" } })}>HTML</Button>
          </Space>
        </div>
        <Descriptions column={{ xs: 1, md: 2 }}>
          <Descriptions.Item label="计划">{data.detail.test_plan.content.title}</Descriptions.Item>
          <Descriptions.Item label="计划版本">{data.detail.test_plan.version_number}</Descriptions.Item>
          <Descriptions.Item label="规划模型">{data.detail.test_plan.planning_context.planning_model.profile_id}</Descriptions.Item>
          <Descriptions.Item label="执行模型">{data.detail.snapshot.execution_model.profile_id}</Descriptions.Item>
        </Descriptions>
        <Row gutter={16}>
          {terminalStatuses.map((status) => <Col key={status} xs={12} md={4}><Statistic title={status} value={counts[status]} /></Col>)}
        </Row>
      </Card>
      {data.detail.task_runs.map((taskRun, index) => {
        const task = data.detail.test_plan.content.tasks.find((item) => item.test_task_id === taskRun.test_task_id);
        const evidence = (taskRun.result?.evidence_artifact_ids ?? [])
          .map((id) => artifacts.get(id))
          .filter((artifact): artifact is Artifact => artifact !== undefined);
        return (
          <Card key={taskRun.id} title={`${index + 1}. ${task?.definition.title ?? taskRun.test_task_id}`} extra={<Tag>{taskRun.status}</Tag>}>
            <Typography.Paragraph><strong>目标：</strong>{task?.definition.goal}</Typography.Paragraph>
            <Typography.Paragraph><strong>判定标准：</strong>{task?.definition.success_criteria.join("；")}</Typography.Paragraph>
            <Typography.Paragraph><strong>结论：</strong>{taskRun.result?.summary ?? "无"}（{taskRun.cycle_count} cycles）</Typography.Paragraph>
            {taskRun.result && <Typography.Paragraph><strong>原因：</strong>{taskRun.result.reason_code}</Typography.Paragraph>}
            <Image.PreviewGroup>
              <Space wrap>{evidence.map((artifact) => <Image key={artifact.id} width={240} src={artifactUrl(artifact.id)} />)}</Space>
            </Image.PreviewGroup>
          </Card>
        );
      })}
    </Space>
  );
}
