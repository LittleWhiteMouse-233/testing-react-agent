import { useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Checkbox, Modal, Spin } from "antd";
import { useCallback, useEffect, useState } from "react";
import { $api, apiErrorMessage } from "../api/client";
import type { CapabilityColumnSelection, CapabilityPreview } from "../api/contracts";
import { capabilityColumnContent, capabilityColumnTitle } from "../capabilities";

export function useCapabilitySelection() {
  const queryClient = useQueryClient();
  const catalog = $api.useQuery("get", "/api/capabilities", {}, {
    retry: false, refetchOnWindowFocus: false, refetchOnMount: "always"
  });
  const [preview, setPreview] = useState<CapabilityPreview | null>(null);
  const [selectedColumns, setSelectedColumns] = useState<CapabilityColumnSelection[]>([]);
  const [stale, setStale] = useState(false);
  useEffect(() => {
    if (!preview && catalog.isFetchedAfterMount && catalog.isSuccess && catalog.data) {
      setPreview(catalog.data);
      setSelectedColumns(catalog.data.columns);
    }
  }, [catalog.data, catalog.isFetchedAfterMount, catalog.isSuccess, preview]);
  const refresh = $api.useMutation("post", "/api/capabilities/refresh", {
    onSuccess: (value) => {
      setSelectedColumns((previous) => preview === null ? value.columns :
        value.columns.filter((column) => previous.some((selected) => selected.id === column.id)));
      setPreview(value);
      setStale(false);
      queryClient.setQueryData($api.queryOptions("get", "/api/capabilities", {}).queryKey, value);
    }
  });
  const error = refresh.error ?? catalog.error;
  const markStale = useCallback(() => setStale(true), []);
  return {
    preview, selectedColumns, setSelectedColumns, stale, markStale,
    ready: preview !== null && !refresh.isPending && !error && !stale,
    refreshing: refresh.isPending, error,
    refresh: () => refresh.mutate({})
  };
}

export function PlanningCapabilitySelector({ selection, disabled }: {
  selection: ReturnType<typeof useCapabilitySelection>; disabled: boolean;
}) {
  return <div>
    <div className="planning-capabilities">
      <CapabilitySelector selection={selection} disabled={disabled} planningColumnKind="mcp" />
      <CapabilitySelector selection={selection} disabled={disabled} planningColumnKind="local" />
    </div>
    {selection.error && <Alert type="error" title={"规划参考目录发现失败：" + apiErrorMessage(selection.error, "请重试")}
      action={<Button disabled={disabled || selection.refreshing} onClick={selection.refresh}>重试规划目录发现</Button>} />}
    {selection.stale && <Alert type="warning" title="规划参考目录已变化，请刷新并检查选择" />}
  </div>;
}

export default function CapabilitySelector({ selection, disabled, planningColumnKind }: {
  selection: ReturnType<typeof useCapabilitySelection>; disabled: boolean;
  planningColumnKind?: "mcp" | "local";
}) {
  const [open, setOpen] = useState(false);
  const { preview, selectedColumns } = selection;
  const busy = disabled || selection.refreshing;
  const planning = planningColumnKind !== undefined;
  const title = planning ? planningColumnKind === "mcp" ? "MCP" : "本地资源" : "资源与 MCP";
  const buttonLabel = planning ? "选择规划" + (planningColumnKind === "mcp" ? " MCP" : "本地资源") : "选择资源与 MCP";
  const columns = preview?.columns.filter((column) => !planning || column.id.startsWith("mcp:") === (planningColumnKind === "mcp")) ?? [];
  const selectColumns = (checked: boolean) => selection.setSelectedColumns((previous) => [
    ...previous.filter((selected) => !columns.some((column) => column.id === selected.id)),
    ...(checked ? columns : [])
  ]);
  const content = <>
    {selection.error && <Alert type="error" title={apiErrorMessage(selection.error, "目录发现失败")} action={<Button disabled={busy} onClick={selection.refresh}>重试发现</Button>} />}
    {selection.stale && <Alert type="warning" title="所选目录已变化，请刷新并检查本次选择" />}
    {!preview && !selection.error && <Spin />}
    {preview && <>
      <div className="capability-actions"><Button disabled={busy} onClick={() => selectColumns(true)}>全选</Button>
        <Button disabled={busy} onClick={() => selectColumns(false)}>全部取消</Button>
        <Button loading={selection.refreshing} disabled={disabled} onClick={selection.refresh}>刷新目录</Button></div>
      {columns.map((column) => {
        const entries = capabilityColumnContent(preview, column.id);
        return <section className="capability-column" key={column.id}>
          <Checkbox disabled={busy} checked={selectedColumns.some((selected) => selected.id === column.id)}
            onChange={(event) => selection.setSelectedColumns((previous) => event.target.checked ? [...previous, column] : previous.filter((selected) => selected.id !== column.id))}>
            {capabilityColumnTitle(preview, column.id)}
          </Checkbox>
          <p className="muted">{entries.tools.length} 个工具 · {entries.resources.length} 个资源 · {entries.templates.length} 个地址模板</p>
          <div className="resource-tools">{entries.tools.map((tool) => <details key={tool.name}><summary>{tool.name}</summary>
            <p>{tool.description}</p><pre>{JSON.stringify(tool.input_schema, null, 2)}</pre></details>)}
          {entries.resources.map((resource) => <details key={resource.uri}><summary>{resource.name}</summary>
            <code>{resource.uri}</code>{resource.description && <p>{resource.description}</p>}{resource.mime_type && <p>{resource.mime_type}</p>}</details>)}
          {entries.templates.map((template) => <details key={template.uri_template}><summary>{template.name} · 地址模板</summary>
            <code>{template.uri_template}</code>{template.description && <p>{template.description}</p>}</details>)}</div>
        </section>;
      })}
    </>}
    <p className="muted">{planning ? "仅影响本轮规划。MCP 工具说明用于参考，规划仅能读取所选资源；任务以目标和成功标准描述。" : "仅影响本次运行。内置结束、资源读取和图片重读工具始终可用。"}</p>
  </>;
  return <section className="capability-card">
    <div className="section-heading"><div><small>{planning ? "本轮规划" : "本次运行"}</small><h3>{title}</h3></div>
      <Button aria-label={buttonLabel} disabled={disabled} onClick={() => setOpen(true)}>{planning ? "选择栏目" : buttonLabel}</Button></div>
    {preview && <p>已选 {columns.filter((column) => selectedColumns.some((selected) => selected.id === column.id)).length} / {columns.length} 个栏目</p>}
    {!planning && !open && selection.error && <Alert type="error" title={apiErrorMessage(selection.error, "目录发现失败")} action={<Button disabled={busy} onClick={selection.refresh}>重试发现</Button>} />}
    {!planning && !open && selection.stale && <Alert type="warning" title="所选目录已变化，请刷新并检查本次选择" />}
    {!preview && !selection.error && <Spin />}
    <Modal title={planning ? "本轮规划的 " + title : "本次运行的资源与 MCP"} open={open} width={860} onCancel={() => setOpen(false)}
      footer={<Button onClick={() => setOpen(false)}>完成</Button>}>{content}</Modal>
  </section>;
}
