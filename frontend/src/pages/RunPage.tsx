import { FileTextOutlined, StopOutlined } from "@ant-design/icons";
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
import {
  $api,
  apiErrorMessage,
  artifactUrl
} from "../api/client";
import {
  mergeStoredRunEvent,
  startRunEventStream,
  type RunEventSource
} from "../api/runEvents";
import type { StoredRunEvent } from "../api/contracts";

function eventDetails(stored: StoredRunEvent): string {
  return JSON.stringify(stored.event, null, 2);
}

export default function RunPage() {
  const { runId = "" } = useParams();
  const [events, setEvents] = useState<StoredRunEvent[]>([]);
  const [streamContractError, setStreamContractError] = useState<string>();
  const detail = $api.useQuery("get", "/api/runs/{test_run_id}", {
    params: { path: { test_run_id: runId } }
  }, {
    refetchInterval: (query) =>
      query.state.data?.run.status !== "finished" ? 1000 : false
  });
  const cancel = $api.useMutation("post", "/api/runs/{test_run_id}/cancel", {
    onSuccess: () => message.info("取消请求已提交，将在安全边界生效"),
    onError: (error) => message.error(apiErrorMessage(error, "取消请求失败"))
  });

  useEffect(() => {
    setEvents([]);
    setStreamContractError(undefined);
    let source: RunEventSource | null = null;
    let disposed = false;
    const restoreAndConnect = async () => {
      const started = await startRunEventStream(runId, {
        onEvent: (stored) => {
          setEvents((current) => mergeStoredRunEvent(current, stored));
        },
        onTerminal: () => {
          void detail.refetch();
        },
        onContractError: () => {
          setStreamContractError("事件流违反 OpenAPI 契约，实时更新已停止");
        }
      });
      if (disposed) {
        started.source?.close();
        return;
      }
      setEvents(started.events);
      source = started.source;
    };
    void restoreAndConnect();
    return () => {
      disposed = true;
      source?.close();
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
    return <Alert type="error" message={apiErrorMessage(detail.error, "运行不存在")} />;
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
      {streamContractError && <Alert type="error" showIcon message={streamContractError} />}
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
            {data.run.status !== "finished" && <Button danger icon={<StopOutlined />} onClick={() => cancel.mutate({ params: { path: { test_run_id: runId } } })} loading={cancel.isPending}>取消</Button>}
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
              <img className="screenshot" src={artifactUrl(latestScreenshot)} alt="电视截图" />
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
