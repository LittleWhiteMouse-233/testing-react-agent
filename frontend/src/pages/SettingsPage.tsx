import { PlusOutlined, ReloadOutlined, SaveOutlined, DeleteOutlined } from "@ant-design/icons";
import { Alert, Button, Checkbox, Form, Input, InputNumber, Select, Spin, Tag, message } from "antd";
import type { FormInstance } from "antd";
import { useQueryClient } from "@tanstack/react-query";
import Ajv2020 from "ajv/dist/2020.js";
import { useEffect, useMemo, useState } from "react";
import { $api, apiErrorMessage } from "../api/client";
import type { ApiValidationError, SettingsResponse } from "../api/contracts";
import { resolveSettingsSchema, settingsFieldErrors, settingsSchemaErrors, settingsSchemaProperties, settingsValueAtPath, toSettingsDraft } from "../settingsForm";
import type { SettingsDraft, SettingsPath, SettingsSchema } from "../settingsForm";

export default function SettingsPage() {
  const queryClient = useQueryClient();
  const query = $api.useQuery("get", "/api/settings", {}, { refetchOnMount: "always", refetchOnWindowFocus: false });
  const [form] = Form.useForm<SettingsDraft>();
  const [snapshot, setSnapshot] = useState<SettingsResponse | null>(null);
  const [errors, setErrors] = useState<ApiValidationError[]>([]);
  const [saveError, setSaveError] = useState("");

  function acceptSettings(response: SettingsResponse) {
    form.resetFields();
    form.setFieldsValue(toSettingsDraft(response));
    setSnapshot(response);
    setErrors([]);
    setSaveError("");
  }

  useEffect(() => {
    if (query.isFetchedAfterMount && query.isSuccess && !snapshot) acceptSettings(query.data);
  }, [query.data, query.isFetchedAfterMount, query.isSuccess, snapshot]);

  const validate = useMemo(() => snapshot ? new Ajv2020({ allErrors: true, strict: false, validateFormats: false }).compile<SettingsDraft>(snapshot.settings_schema) : null, [snapshot?.settings_schema]);
  const save = $api.useMutation("put", "/api/settings", {
    onSuccess: (response) => {
      queryClient.setQueryData($api.queryOptions("get", "/api/settings", {}).queryKey, response);
      acceptSettings(response);
      message.success("设置已保存");
    },
    onError: (error) => {
      const uncertain = typeof error?.code !== "string" || typeof error?.message !== "string";
      const explanation = uncertain ? "保存结果尚未确认，请重新加载设置检查是否生效" : apiErrorMessage(error, "设置保存失败");
      setSaveError(explanation);
      setErrors(error?.errors ?? []);
      if (uncertain) message.warning(explanation); else message.error(explanation);
    }
  });

  function submitSettings() {
    if (!snapshot || !validate) return;
    const draft = form.getFieldsValue(true);
    setSaveError("");
    if (!validate(draft)) {
      setErrors(settingsSchemaErrors(validate.errors));
      return;
    }
    setErrors([]);
    save.mutate({ body: { version: snapshot.version, settings: draft }, signal: AbortSignal.timeout(30_000) });
  }

  return <div className="page settings-page">
    <div className="page-header"><div><h1>设置</h1><p>管理应用配置、模型、规划与执行参数</p></div>
      <div className="settings-actions">
        <Button aria-label="重新加载" icon={<ReloadOutlined />} disabled={save.isPending} loading={query.isFetching} onClick={async () => {
          const response = await query.refetch();
          if (response.isSuccess) acceptSettings(response.data);
        }}>重新加载</Button>
        <Button aria-label="保存设置" type="primary" icon={<SaveOutlined />} loading={save.isPending} disabled={!snapshot} onClick={submitSettings}>保存设置</Button>
      </div>
    </div>
    {snapshot?.restart_required && <Alert type="info" showIcon title="启动参数已经发生修改，请重启应用后生效" />}
    {saveError && <Alert type="error" showIcon title={saveError} />}
    {query.error && <Alert type="error" showIcon title={apiErrorMessage(query.error, "设置加载失败")} />}
    {!snapshot && <Spin />}
    <Form form={form} hidden={!snapshot} layout="vertical" disabled={save.isPending} onValuesChange={() => setErrors([])} onFinish={submitSettings}>
      <Form.ErrorList errors={settingsFieldErrors(errors, [])} />
      {snapshot && <SettingsFields form={form} snapshot={snapshot} errors={errors} />}
    </Form>
  </div>;
}

function SettingsFields({ form, snapshot, errors }: { form: FormInstance<SettingsDraft>; snapshot: SettingsResponse; errors: ApiValidationError[] }) {
  const draft = Form.useWatch((values) => values, form) as SettingsDraft | undefined;
  const root = snapshot.settings_schema as SettingsSchema;

  function renderField(definition: SettingsSchema, path: SettingsPath, name: SettingsPath) {
    const schema = resolveSettingsSchema(definition, root);
    const title = schema.title ?? String(path.at(-1) ?? "");
    const activationTag = path.length === 1 && <Tag>{schema["x-requires-restart"] === false ? "新工作生效" : "重启生效"}</Tag>;
    const fieldErrors = settingsFieldErrors(errors, path);
    if (schema.type === "object") {
      const showTitle = path.length > 0 && typeof path.at(-1) === "string";
      return <section key={path.join(".")} className={showTitle ? "settings-section" : "settings-fields"}>
        {showTitle && <h2>{title} {activationTag}</h2>}
        {showTitle && schema.description && <p className="settings-description">{schema.description}</p>}
        <Form.ErrorList errors={fieldErrors} />
        {Object.entries(settingsSchemaProperties(schema, root)).map(([key, child]) => renderField(child, [...path, key], [...name, key]))}
      </section>;
    }
    if (schema.type === "array" && schema.items) {
      const itemSchema = resolveSettingsSchema(schema.items, root);
      return <section className="settings-list" key={path.join(".")}>
        <h3>{title} {activationTag}</h3><p className="settings-description">{schema.description}</p>
        <Form.ErrorList errors={fieldErrors} />
        <Form.List name={name}>{(fields, { add, remove }) => <>
          {fields.map((field) => <div key={field.key} className="settings-list-row">
            {renderField(schema.items!, [...path, field.name], [field.name])}
            <Button icon={<DeleteOutlined />} aria-label={`删除${title}`} onClick={() => remove(field.name)} />
          </div>)}
          <Button aria-label={`新增${title}`} icon={<PlusOutlined />} onClick={() => add(itemSchema.type === "object" ? {} : "")}>新增{title}</Button>
        </>}</Form.List>
      </section>;
    }

    const optionsPath = schema["x-options-path"];
    const optionValue = schema["x-option-value"];
    const optionSource = optionsPath ? settingsValueAtPath(draft, optionsPath) : undefined;
    const options = Array.isArray(optionSource) && optionValue ? optionSource.flatMap((profile: unknown) => {
      const id = settingsValueAtPath(profile, [optionValue]);
      return typeof id === "string" && id ? [{ label: id, value: id }] : [];
    }) : schema.enum?.map((value) => ({ label: String(value), value }));
    const nullable = schema.anyOf?.some((choice) => choice.type === "null");
    let control;
    if (options) control = <Select options={options} />;
    else if (schema.type === "boolean") control = <Checkbox />;
    else if (schema.type === "integer" || schema.type === "number") control = <InputNumber style={{ width: "100%" }} />;
    else if (schema.format === "password") {
      const profileId = settingsValueAtPath(draft, [...path.slice(0, -1), "id"]);
      const configured = snapshot.settings.llm.profiles.some((profile) => profile.id === profileId && profile.api_key_configured);
      control = <Input.Password autoComplete="new-password" placeholder={configured ? "已配置，留空保持不变" : "未配置"} />;
    } else control = <Input />;
    const startupNote = path.length === 1 && schema["x-requires-restart"] !== false ? "重启后生效。" : "";
    return <Form.Item key={path.join(".")} name={name} label={title} extra={`${schema.description ?? ""}${startupNote}`}
      valuePropName={schema.type === "boolean" ? "checked" : "value"}
      normalize={nullable && schema.format !== "password" ? (value: unknown) => value === "" ? null : value : undefined}
      help={fieldErrors.length ? fieldErrors.join("；") : undefined} validateStatus={fieldErrors.length ? "error" : undefined}>
      {control}
    </Form.Item>;
  }

  return renderField(root, [], []);
}
