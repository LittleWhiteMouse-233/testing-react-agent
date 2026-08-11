import { FileTextOutlined, StopOutlined } from "@ant-design/icons";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  List,
  Progress,
  Row,
  Space,
  Spin,
  Tag,
  Typography,
  message
} from "antd";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, apiUrl } from "../api/client";
import { parseStoredRunEvent } from "../api/runEvents";
import type {
  RunEventPage,
  StoredRunEvent,
  TestRunDetail
} from "../api/contracts";

function eventDetails(stored: StoredRunEvent): string {
  return JSON.stringify(stored.event, null, 2);
}

export default function RunPage() {
  const { runId = "" } = useParams();
  const [events, setEvents] = useState<StoredRunEvent[]>([]);
  const detail = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api<TestRunDetail>(`/runs/${runId}`),
    refetchInterval: (query) =>
      query.state.data?.run.status !== "finished" ? 1000 : false
  });
  const cancel = useMutation({
    mutationFn: () => api<void>(`/runs/${runId}/cancel`, { method: "POST" }),
    onSuccess: () => message.info("取消请求已提交，将在安全边界生效"),
    onError: (error: Error) => message.error(error.message)
  });

  useEffect(() => {
    setEvents([]);
    let cursor = 0;
    let terminal = false;
    let source: EventSource | null = null;
    let retryTimer: number | undefined;
    let disposed = false;
    const receive = (raw: MessageEvent<string>) => {
      const item = parseStoredRunEvent(raw.data);
      setEvents((current) =>
        [...current.filter((entry) => entry.sequence !== item.sequence), item].sort(
          (left, right) => left.sequence - right.sequence
        )
      );
      cursor = Math.max(cursor, item.sequence);
      if (item.event.type === "run.finished" || item.event.type === "run.cancelled") {
        terminal = true;
        source?.close();
        void detail.refetch();
      }
    };
    const connect = () => {
      if (disposed || terminal) return;
      source = new EventSource(apiUrl(`/runs/${runId}/stream?after=${cursor}`));
      source.onmessage = receive;
      source.onerror = () => {
        source?.close();
        if (disposed || terminal) return;
        retryTimer = window.setTimeout(connect, 1000);
      };
    };
    const restore = async () => {
      try {
        const history = await api<RunEventPage>(`/runs/${runId}/events?after=0`);
        if (disposed) return;
        const restored = [...history.items].sort(
          (left, right) => left.sequence - right.sequence
        );
        setEvents(restored);
        cursor = restored.at(-1)?.sequence ?? 0;
        terminal = restored.some(
          (item) => item.event.type === "run.finished" || item.event.type === "run.cancelled"
        );
      } catch {
        // The persisted SSE endpoint can still replay from sequence zero.
      } finally {
        connect();
      }
    };
    void restore();
    return () => {
      disposed = true;
      source?.close();
      if (retryTimer) window.clearTimeout(retryTimer);
    };
  }, [runId]);

  const latestScreenshot = useMemo(() => {
    for (const stored of [...events].reverse()) {
      if (stored.event.type !== "message.appended") continue;
      const image = stored.event.message.content.find(
        (block) => block.type === "image_artifact"
      );
      if (image?.type === "image_artifact") return image.artifact_id;
    }
    return undefined;
  }, [events]);

  if (detail.isLoading) return <Spin />;
  if (detail.error || !detail.data) {
    return <Alert type="error" message={(detail.error as Error)?.message ?? "运行不存在"} />;
  }
  const data = detail.data;
  const activeTaskRun = data.task_runs.find((task) => task.status === "running");
  const activeTask = data.test_plan.content.tasks.find(
    (task) => task.test_task_id === activeTaskRun?.test_task_id
  );
  const percent = activeTaskRun && activeTask
    ? Math.min(100, Math.round((activeTaskRun.cycle_count / activeTask.definition.max_cycles) * 100))
    : 0;

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Card>
        <div className="toolbar">
          <div>
            <Typography.Title level={3} style={{ margin: 0 }}>运行 {runId.slice(0, 8)}</Typography.Title>
            <Space>
              <Tag>{data.run.status}</Tag>
              {data.run.verdict && <Tag color={data.run.verdict === "PASS" ? "green" : data.run.verdict === "FAIL" ? "red" : "orange"}>{data.run.verdict}</Tag>}
            </Space>
          </div>
          <Space>
            {data.run.status !== "finished" && <Button danger icon={<StopOutlined />} onClick={() => cancel.mutate()} loading={cancel.isPending}>取消</Button>}
            {data.run.verdict && <Link to={`/runs/${runId}/report`}><Button type="primary" icon={<FileTextOutlined />}>查看报告</Button></Link>}
          </Space>
        </div>
        <Descriptions column={{ xs: 1, sm: 2, md: 3 }}>
          <Descriptions.Item label="设备">{data.run.device_id}</Descriptions.Item>
          <Descriptions.Item label="计划版本">{data.test_plan.version_number}</Descriptions.Item>
          <Descriptions.Item label="执行协议">{data.snapshot.execution_protocol_version}</Descriptions.Item>
          <Descriptions.Item label="执行模型">{data.snapshot.act_model.profile_id}</Descriptions.Item>
          <Descriptions.Item label="判定模型">{data.snapshot.judge_model.profile_id}</Descriptions.Item>
        </Descriptions>
        {activeTaskRun && activeTask && (
          <>
            <Typography.Text strong>当前任务：{activeTask.definition.title}</Typography.Text>
            <Progress percent={percent} format={() => `${activeTaskRun.cycle_count}/${activeTask.definition.max_cycles}`} />
          </>
        )}
      </Card>
      <Row gutter={[20, 20]}>
        <Col xs={24} lg={13}>
          <Card title="最新电视截图">
            {latestScreenshot ? (
              <img className="screenshot" src={apiUrl(`/artifacts/${latestScreenshot}`)} alt="电视截图" />
            ) : (
              <Alert message="等待首次截图…" type="info" />
            )}
          </Card>
        </Col>
        <Col xs={24} lg={11}>
          <Card title={`事实时间线（${events.length}）`}>
            <List
              className="event-list"
              dataSource={[...events].reverse()}
              locale={{ emptyText: "等待事件…" }}
              renderItem={(stored) => (
                <List.Item>
                  <div className="event-item">
                    <Space><Tag>{stored.sequence}</Tag><strong>{stored.event.type}</strong></Space>
                    <Typography.Paragraph type="secondary" ellipsis={{ rows: 3, expandable: true }}>{eventDetails(stored)}</Typography.Paragraph>
                  </div>
                </List.Item>
              )}
            />
          </Card>
        </Col>
      </Row>
    </Space>
  );
}
