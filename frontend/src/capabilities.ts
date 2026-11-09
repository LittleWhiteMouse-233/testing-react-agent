import type { CapabilityPreview, CapabilitySnapshot } from "./api/contracts";

type CapabilityDirectory = CapabilityPreview | CapabilitySnapshot;

export function capabilityColumnTitle(catalog: CapabilityDirectory, columnId: string): string {
  if (columnId.startsWith("mcp:")) return "MCP · " + columnId.slice(4);
  if (columnId === "local:other") return "其他本地资源";
  return catalog.resource_catalog.resources.find((resource) => resource.uri === columnId)?.name ?? "本地 Skill";
}

// Display grouping uses only the backend's declared top-level columns. Access is checked by the backend.
export function capabilityColumnContent(catalog: CapabilityDirectory, columnId: string) {
  const localPackagePrefixes = catalog.columns.filter((column) => column.id.startsWith("file:"))
    .map((column) => column.id.slice(0, column.id.lastIndexOf("/") + 1));
  const packagePrefix = columnId.slice(0, columnId.lastIndexOf("/") + 1);
  return {
    tools: catalog.tool_catalog.tools.filter((tool) => tool.source_id === columnId),
    resources: catalog.resource_catalog.resources.filter((resource) => {
      if (resource.source_id !== "local") return resource.source_id === columnId;
      if (columnId.startsWith("file:")) return resource.uri.startsWith(packagePrefix);
      return columnId === "local:other" && !localPackagePrefixes.some((prefix) => resource.uri.startsWith(prefix));
    }),
    templates: catalog.resource_catalog.templates.filter((template) => template.source_id === columnId)
  };
}
