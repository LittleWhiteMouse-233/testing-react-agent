import type { ErrorObject } from "ajv";
import type { ApiValidationError, SettingsResponse, SettingsUpdateRequest } from "./api/contracts";

export type SettingsDraft = SettingsUpdateRequest["settings"];
export type SettingsPath = (string | number)[];

// The JSON Schema subset emitted by the backend's current configuration models.
export interface SettingsSchema {
  $ref?: string;
  $defs?: Record<string, SettingsSchema>;
  type?: string;
  title?: string;
  description?: string;
  properties?: Record<string, SettingsSchema>;
  propertyNames?: SettingsSchema;
  additionalProperties?: boolean | SettingsSchema;
  items?: SettingsSchema;
  anyOf?: SettingsSchema[];
  enum?: (string | number)[];
  default?: unknown;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
  format?: string;
  "x-requires-restart"?: boolean;
  "x-options-path"?: string[];
  "x-option-value"?: string;
}

export function toSettingsDraft(response: SettingsResponse): SettingsDraft {
  return { ...response.settings, llm: { ...response.settings.llm,
    profiles: response.settings.llm.profiles.map(({ api_key_configured, ...profile }) => profile)
  } };
}

export function resolveSettingsSchema(schema: SettingsSchema, root: SettingsSchema): SettingsSchema {
  const referenced = schema.$ref ? root.$defs?.[schema.$ref.split("/").at(-1)!] : undefined;
  const resolved = { ...referenced, ...schema };
  const nonNull = resolved.anyOf?.find((choice) => choice.type !== "null");
  return nonNull ? { ...resolveSettingsSchema(nonNull, root), ...resolved } : resolved;
}

export function settingsSchemaProperties(schema: SettingsSchema, root: SettingsSchema): Record<string, SettingsSchema> {
  if (schema.properties) return schema.properties;
  const keys = schema.propertyNames && resolveSettingsSchema(schema.propertyNames, root).enum;
  if (keys && typeof schema.additionalProperties === "object") {
    return Object.fromEntries(keys.map((key) => [String(key), { ...schema.additionalProperties as SettingsSchema, title: String(key) }]));
  }
  return {};
}

export function settingsValueAtPath(value: unknown, path: SettingsPath): unknown {
  return path.reduce<unknown>((parent, name) => {
    if (Array.isArray(parent) && typeof name === "number") return parent[name];
    return parent && typeof parent === "object" ? (parent as Record<string, unknown>)[name] : undefined;
  }, value);
}

export function settingsFieldErrors(errors: ApiValidationError[], path: SettingsPath): string[] {
  return errors.filter((error) => error.loc.length === path.length && error.loc.every((name, index) => name === path[index])).map((error) => error.msg);
}

export function settingsSchemaErrors(errors: ErrorObject[] | null | undefined): ApiValidationError[] {
  return (errors ?? []).filter((error) => error.keyword !== "anyOf").map((error) => {
    const loc: SettingsPath = error.instancePath.split("/").slice(1).map((part) => {
      const name = part.replace(/~1/g, "/").replace(/~0/g, "~");
      return /^\d+$/.test(name) ? Number(name) : name;
    });
    if (error.keyword === "required") loc.push(String(error.params.missingProperty));
    const messages: Record<string, string> = {
      minimum: `必须大于等于 ${error.params.limit}`,
      maximum: `必须小于等于 ${error.params.limit}`,
      exclusiveMinimum: `必须大于 ${error.params.limit}`,
      minLength: `至少填写 ${error.params.limit} 个字符`,
      minItems: `至少保留 ${error.params.limit} 项`,
      required: "请填写此项",
      type: "数值类型不正确",
      enum: "请选择有效选项"
    };
    return { loc, msg: messages[error.keyword] ?? error.message ?? "配置不合法" };
  });
}
