import { FileTextOutlined, StopOutlined } from "@ant-design/icons";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Col, Descriptions, List, Progress, Row, Space, Spin, Tag, Typography, message } from "antd";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, apiUrl } from "../api/client";
import type { Run, StepEvent } from "../types";

const eventTypes = ["run_started", "task_started", "observation_captured", "decision_made", "action_started", "action_finished", "task_finished", "tasks_skipped", "run_finished", "run_cancelled", "error"];

export default function RunPage() {
  const { runId = "" } = useParams();
  const [events, setEvents] = useState<StepEvent[]>([]);
  const run = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api<Run>(`/runs/${runId}`),
    refetchInterval: (query) => ["pending", "running"].includes(query.state.data?.status ?? "") ? 1000 : false
  });
  const cancel = useMutation({
    mutationFn: () => api(`/runs/${runId}/cancel`, { method: "POST" }),
    onSuccess: () => message.info("取消请求已提交，将在动作边界生效"),
    onError: (error: Error) => message.error(error.message)
  });

  useEffect(() => {
    const storageKey = `run:${runId}:sequence`;
    let cursor = Number(localStorage.getItem(storageKey) ?? "0");
    let source: EventSource | null = null;
    let retryTimer: number | undefined;
    let disposed = false;
    const receive = (raw: Event) => {
      const item = JSON.parse((raw as MessageEvent).data) as StepEvent;
      setEvents((current) => [...current.filter((entry) => entry.sequence !== item.sequence), item].sort((a, b) => a.sequence - b.sequence));
      cursor = Math.max(cursor, item.sequence);
      localStorage.setItem(storageKey, String(item.sequence));
      if (["run_finished", "run_cancelled"].includes(item.type)) run.refetch();
    };
    const connect = () => {
      if (disposed) return;
      source = new EventSource(apiUrl(`/runs/${runId}/stream?after=${cursor}`));
      eventTypes.forEach((type) => source!.addEventListener(type, receive));
      source.onerror = () => {
        source?.close();
        retryTimer = window.setTimeout(connect, 1000);
      };
    };
    connect();
    return () => {
      disposed = true;
      source?.close();
      if (retryTimer) window.clearTimeout(retryTimer);
    };
  }, [runId]);

  const latestScreenshot = useMemo(() => [...events].reverse().find((item) => item.type === "observation_captured")?.payload.artifact_id, [events]);
  const activeTask = run.data?.task_runs?.find((item) => item.status === "running");
  const definition = run.data?.snapshot?.plan?.tasks?.[activeTask?.task_index ?? -1];
  const percent = activeTask && definition ? Math.min(100, Math.round(activeTask.cycle_count / definition.max_cycles * 100)) : 0;
  if (run.isLoading) return <Spin />;
  if (run.error || !run.data) return <Alert type="error" message={(run.error as Error)?.message ?? "运行不存在"} />;

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card>
        <div className="toolbar">
          <div>
            <Typography.Title level={3} style={{ margin: 0 }}>运行 {runId.slice(0, 8)}</Typography.Title>
            <Space><Tag>{run.data.status}</Tag>{run.data.overall_result && <Tag color={run.data.overall_result === "PASS" ? "green" : "orange"}>{run.data.overall_result}</Tag>}</Space>
          </div>
          <Space>
            {run.data.status === "running" && <Button danger icon={<StopOutlined />} onClick={() => cancel.mutate()} loading={cancel.isPending}>取消</Button>}
            {run.data.overall_result && <Link to={`/runs/${runId}/report`}><Button type="primary" icon={<FileTextOutlined />}>查看报告</Button></Link>}
          </Space>
        </div>
        <Descriptions column={{ xs: 1, sm: 2, md: 3 }}>
          <Descriptions.Item label="设备">{run.data.device_id}</Descriptions.Item>
          <Descriptions.Item label="计划版本">{run.data.snapshot?.plan_revision?.revision}</Descriptions.Item>
          <Descriptions.Item label="模型">{run.data.snapshot?.model?.model}</Descriptions.Item>
        </Descriptions>
        {activeTask && <><Typography.Text strong>当前任务：{definition?.title}</Typography.Text><Progress percent={percent} format={() => `${activeTask.cycle_count}/${definition?.max_cycles}`} /></>}
      </Card>
      <Row gutter={[20, 20]}>
        <Col xs={24} lg={13}>
          <Card title="最新电视截图">
            {latestScreenshot ? <img className="screenshot" src={apiUrl(`/artifacts/${latestScreenshot}`)} alt="电视截图" /> : <Alert message="等待首次观察…" type="info" />}
          </Card>
        </Col>
        <Col xs={24} lg={11}>
          <Card title={`事件时间线（${events.length}）`}>
            <List
              className="event-list"
              dataSource={[...events].reverse()}
              locale={{ emptyText: "等待事件…" }}
              renderItem={(item) => (
                <List.Item><div className="event-item"><Space><Tag>{item.sequence}</Tag><strong>{item.type}</strong></Space><Typography.Paragraph type="secondary" ellipsis={{ rows: 3, expandable: true }}>{JSON.stringify(item.payload, null, 2)}</Typography.Paragraph></div></List.Item>
              )}
            />
          </Card>
        </Col>
      </Row>
    </Space>
  );
}
