import { useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Checkbox, Modal, Spin } from "antd";
import { useCallback, useEffect, useState } from "react";
import { $api, apiErrorMessage } from "../api/client";
import type { CapabilityPreview } from "../api/contracts";
import { capabilityColumnContent, capabilityColumnTitle } from "../capabilities";

export function useCapabilityCatalog() {
  const queryClient = useQueryClient();
  const catalog = $api.useQuery("get", "/api/capabilities", {}, {
    retry: false, refetchOnWindowFocus: false, refetchOnMount: "always"
  });
  const refresh = $api.useMutation("post", "/api/capabilities/refresh", {
    onSuccess: async (value) => {
      const queryKey = $api.queryOptions("get", "/api/capabilities", {}).queryKey;
      await queryClient.cancelQueries({ queryKey });
      queryClient.setQueryData(queryKey, value);
    }
  });
  const { refetch } = catalog;
  const { reset } = refresh;
  const reload = useCallback(() => { reset(); void refetch(); }, [reset, refetch]);
  const preview = catalog.isFetchedAfterMount && catalog.isSuccess ? catalog.data : null;
  const error = refresh.error ?? catalog.error;
  const updating = catalog.isFetching || refresh.isPending;
  const retryDiscovery = refresh.isError || !catalog.data;
  return {
    preview, ready: preview !== null && !updating && !error,
    updating, error, reload, retryDiscovery,
    retry: retryDiscovery ? () => refresh.mutate({}) : reload,
    refresh: () => refresh.mutate({})
  };
}

export function useCapabilitySelection(preview: CapabilityPreview | null) {
  const [selectedColumnIds, setSelectedColumnIds] = useState<string[] | null>(null);
  useEffect(() => {
    if (preview) setSelectedColumnIds((previous) => previous === null ? preview.columns.map((column) => column.id) :
      previous.filter((columnId) => preview.columns.some((column) => column.id === columnId)));
  }, [preview]);
  return {
    selectedColumns: preview?.columns.filter((column) => selectedColumnIds?.includes(column.id)) ?? [],
    setSelectedColumnIds, ready: selectedColumnIds !== null
  };
}

export function PlanningCapabilitySelector({ catalog, selection, disabled }: {
  catalog: ReturnType<typeof useCapabilityCatalog>; selection: ReturnType<typeof useCapabilitySelection>; disabled: boolean;
}) {
  return <div>
    <div className="planning-capabilities">
      <CapabilitySelector catalog={catalog} selection={selection} disabled={disabled} planningColumnKind="mcp" />
      <CapabilitySelector catalog={catalog} selection={selection} disabled={disabled} planningColumnKind="local" />
    </div>
    {catalog.error && <Alert type="error" title={"规划参考目录不可用：" + apiErrorMessage(catalog.error, "请重试")}
      action={<Button disabled={disabled || catalog.updating} onClick={catalog.retry}>{catalog.retryDiscovery ? "重试规划目录发现" : "重试读取规划目录"}</Button>} />}
  </div>;
}

export default function CapabilitySelector({ catalog, selection, disabled, planningColumnKind }: {
  catalog: ReturnType<typeof useCapabilityCatalog>; selection: ReturnType<typeof useCapabilitySelection>; disabled: boolean;
  planningColumnKind?: "mcp" | "local";
}) {
  const [open, setOpen] = useState(false);
  const { preview } = catalog;
  const { selectedColumns } = selection;
  const busy = disabled || catalog.updating;
  const planning = planningColumnKind !== undefined;
  const title = planning ? planningColumnKind === "mcp" ? "MCP" : "本地资源" : "资源与 MCP";
  const buttonLabel = planning ? "选择规划" + (planningColumnKind === "mcp" ? " MCP" : "本地资源") : "选择资源与 MCP";
  const columns = preview?.columns.filter((column) => !planning || column.id.startsWith("mcp:") === (planningColumnKind === "mcp")) ?? [];
  const selectColumns = (checked: boolean) => selection.setSelectedColumnIds((previous) => [
    ...(previous ?? []).filter((columnId) => !columns.some((column) => column.id === columnId)),
    ...(checked ? columns.map((column) => column.id) : [])
  ]);
  const errorAlert = catalog.error && <Alert type="error" title={apiErrorMessage(catalog.error, "目录读取失败")}
    action={<Button disabled={busy} onClick={catalog.retry}>{catalog.retryDiscovery ? "重试发现" : "重试读取目录"}</Button>} />;
  const content = <>
    {errorAlert}
    {!preview && !catalog.error && <Spin />}
    <div className="capability-actions"><Button disabled={busy || !preview} onClick={() => selectColumns(true)}>全选</Button>
      <Button disabled={busy || !preview} onClick={() => selectColumns(false)}>全部取消</Button>
      <Button loading={catalog.updating} disabled={busy} onClick={catalog.refresh}>刷新目录</Button></div>
    {preview && <>
      {columns.map((column) => {
        const entries = capabilityColumnContent(preview, column.id);
        return <section className="capability-column" key={column.id}>
          <Checkbox disabled={busy} checked={selectedColumns.some((selected) => selected.id === column.id)}
            onChange={(event) => selection.setSelectedColumnIds((previous) => event.target.checked ? [...(previous ?? []), column.id] : (previous ?? []).filter((columnId) => columnId !== column.id))}>
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
    <p className="muted">{planning ? "本处勾选用于本轮规划。MCP 工具说明用于参考，规划仅能读取所选资源；任务以目标和成功标准描述。" : "本处勾选用于本次运行。内置结束、资源读取和图片重读工具始终可用。"}</p>
  </>;
  return <section className="capability-card">
    <div className="section-heading"><div><small>{planning ? "本轮规划" : "本次运行"}</small><h3>{title}</h3></div>
      <Button aria-label={buttonLabel} disabled={disabled} onClick={() => setOpen(true)}>{planning ? "选择栏目" : buttonLabel}</Button></div>
    {preview && <p>已选 {columns.filter((column) => selectedColumns.some((selected) => selected.id === column.id)).length} / {columns.length} 个栏目</p>}
    {!planning && !open && errorAlert}
    {!preview && !catalog.error && <Spin />}
    <Modal title={planning ? "本轮规划的 " + title : "本次运行的资源与 MCP"} open={open} width={860} onCancel={() => setOpen(false)}
      footer={<Button onClick={() => setOpen(false)}>完成</Button>}>{content}</Modal>
  </section>;
}
