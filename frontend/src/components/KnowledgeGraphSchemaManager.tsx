import { DatabaseOutlined } from "@ant-design/icons";
import {
  Button,
  Empty,
  Input,
  List,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Tag,
  Typography,
  message,
} from "antd";
import { useCallback, useMemo, useState } from "react";

import {
  deprecateKnowledgeGraphSchemaItem,
  getKnowledgeGraphSchema,
  upsertKnowledgeGraphSchemaItem,
} from "../api";
import type { KnowledgeGraphSchema } from "../types";

type SchemaKind = "entity_type" | "relation_type" | "property";

type CustomItem = {
  kind: SchemaKind;
  key: string;
  label: string;
  status: "active" | "deprecated";
  version: number;
  definition: Record<string, unknown>;
};

type Props = {
  onChanged: () => Promise<void>;
};

const KIND_LABELS: Record<SchemaKind, string> = {
  entity_type: "实体类型",
  relation_type: "关系类型",
  property: "属性定义",
};

export function KnowledgeGraphSchemaManager({ onChanged }: Props) {
  const [open, setOpen] = useState(false);
  const [schema, setSchema] = useState<KnowledgeGraphSchema | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [kind, setKind] = useState<SchemaKind>("entity_type");
  const [key, setKey] = useState("");
  const [label, setLabel] = useState("");
  const [expectedVersion, setExpectedVersion] = useState(0);
  const [parentKey, setParentKey] = useState<string | null>(null);
  const [sourceType, setSourceType] = useState<string | null>(null);
  const [targetType, setTargetType] = useState<string | null>(null);
  const [symmetric, setSymmetric] = useState(false);
  const [transitive, setTransitive] = useState(false);
  const [ownerKind, setOwnerKind] = useState<"entity_type" | "relation_type">(
    "entity_type",
  );
  const [ownerKey, setOwnerKey] = useState<string | null>(null);
  const [valueType, setValueType] = useState("string");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setSchema(await getKnowledgeGraphSchema());
    } catch (error: unknown) {
      const detail = error instanceof Error ? error.message : String(error);
      message.error(`加载 Schema 失败：${detail}`);
    } finally {
      setLoading(false);
    }
  }, []);

  const openManager = useCallback(() => {
    setOpen(true);
    void load();
  }, [load]);

  const entityOptions = useMemo(
    () =>
      (schema?.entity_types ?? [])
        .filter((item) => item.status !== "deprecated")
        .map((item) => ({ value: item.key, label: `${item.label}（${item.key}）` })),
    [schema],
  );
  const relationOptions = useMemo(
    () =>
      (schema?.relation_types ?? [])
        .filter((item) => item.status !== "deprecated")
        .map((item) => ({ value: item.key, label: `${item.label}（${item.key}）` })),
    [schema],
  );
  const customItems = useMemo<CustomItem[]>(() => {
    if (!schema) return [];
    const entities = schema.entity_types
      .filter((item) => item.namespace === "custom")
      .map((item) => ({
        kind: "entity_type" as const,
        key: item.key,
        label: item.label,
        status: item.status ?? "active",
        version: item.version ?? 1,
        definition: {
          label: item.label,
          parent_key: item.parent_key,
        },
      }));
    const relations = schema.relation_types
      .filter((item) => item.namespace === "custom")
      .map((item) => ({
        kind: "relation_type" as const,
        key: item.key,
        label: item.label,
        status: item.status ?? "active",
        version: item.version ?? 1,
        definition: {
          label: item.label,
          source_type: item.source_type,
          target_type: item.target_type,
          symmetric: item.symmetric,
          transitive: item.transitive,
          inverse_key: item.inverse_key,
        },
      }));
    const properties = schema.property_definitions
      .filter((item) => item.namespace === "custom")
      .map((item) => ({
        kind: "property" as const,
        key: item.key,
        label: item.label,
        status: item.status ?? "active",
        version: item.version ?? 1,
        definition: {
          label: item.label,
          owner_kind: item.owner_kind,
          owner_key: item.owner_key,
          value_type: item.value_type,
          required: item.required,
          multiple: item.multiple,
          constraints: item.constraints,
        },
      }));
    return [...entities, ...relations, ...properties];
  }, [schema]);

  const resetForm = useCallback((nextKind: SchemaKind = "entity_type") => {
    setKind(nextKind);
    setKey("");
    setLabel("");
    setExpectedVersion(0);
    setParentKey(null);
    setSourceType(null);
    setTargetType(null);
    setSymmetric(false);
    setTransitive(false);
    setOwnerKind("entity_type");
    setOwnerKey(null);
    setValueType("string");
  }, []);

  const editItem = useCallback((item: CustomItem) => {
    setKind(item.kind);
    setKey(item.key);
    setLabel(item.label);
    setExpectedVersion(item.version);
    setParentKey((item.definition.parent_key as string | null) ?? null);
    setSourceType((item.definition.source_type as string | null) ?? null);
    setTargetType((item.definition.target_type as string | null) ?? null);
    setSymmetric(Boolean(item.definition.symmetric));
    setTransitive(Boolean(item.definition.transitive));
    setOwnerKind(
      (item.definition.owner_kind as "entity_type" | "relation_type") ??
        "entity_type",
    );
    setOwnerKey((item.definition.owner_key as string | null) ?? null);
    setValueType((item.definition.value_type as string) ?? "string");
  }, []);

  const definition = useMemo<Record<string, unknown>>(() => {
    if (kind === "entity_type") {
      return { label: label.trim(), parent_key: parentKey };
    }
    if (kind === "relation_type") {
      return {
        label: label.trim(),
        source_type: sourceType,
        target_type: targetType,
        symmetric,
        transitive,
        inverse_key: null,
      };
    }
    return {
      label: label.trim(),
      owner_kind: ownerKind,
      owner_key: ownerKey,
      value_type: valueType,
      required: false,
      multiple: false,
      constraints: null,
    };
  }, [
    kind,
    label,
    ownerKey,
    ownerKind,
    parentKey,
    sourceType,
    symmetric,
    targetType,
    transitive,
    valueType,
  ]);

  const valid =
    key.startsWith("custom.") &&
    Boolean(label.trim()) &&
    (kind !== "relation_type" || Boolean(sourceType && targetType)) &&
    (kind !== "property" || Boolean(ownerKey));

  const submit = useCallback(async () => {
    if (!schema || !valid) return;
    setSubmitting(true);
    try {
      await upsertKnowledgeGraphSchemaItem(
        kind,
        key.trim(),
        definition,
        schema.revision ?? 0,
        expectedVersion,
      );
      message.success(expectedVersion ? "自定义 Schema 已更新" : "自定义 Schema 已创建");
      resetForm(kind);
      await Promise.all([load(), onChanged()]);
    } catch (error: unknown) {
      const detail = error instanceof Error ? error.message : String(error);
      message.error(`Schema 保存失败：${detail}`);
    } finally {
      setSubmitting(false);
    }
  }, [
    definition,
    expectedVersion,
    key,
    kind,
    load,
    onChanged,
    resetForm,
    schema,
    valid,
  ]);

  const deprecate = useCallback(
    async (item: CustomItem) => {
      if (!schema) return;
      setSubmitting(true);
      try {
        await deprecateKnowledgeGraphSchemaItem(
          item.kind,
          item.key,
          schema.revision ?? 0,
          item.version,
        );
        message.success("Schema 项已弃用");
        await Promise.all([load(), onChanged()]);
      } catch (error: unknown) {
        const detail = error instanceof Error ? error.message : String(error);
        message.error(`Schema 弃用失败：${detail}`);
      } finally {
        setSubmitting(false);
      }
    },
    [load, onChanged, schema],
  );

  return (
    <>
      <Button
        size="small"
        icon={<DatabaseOutlined />}
        onClick={openManager}
        data-testid="kg-schema-manager"
      >
        Schema
      </Button>
      <Modal
        title="知识图谱 Schema"
        open={open}
        onCancel={() => setOpen(false)}
        footer={null}
        width={900}
        loading={loading}
        destroyOnHidden
      >
        <Typography.Paragraph type="secondary">
          系统类型受保护；自定义 key 必须以 <code>custom.</code> 开头。删除采用弃用语义。
        </Typography.Paragraph>
        <div className="grid gap-5 md:grid-cols-2">
          <div className="flex flex-col gap-3" data-testid="kg-schema-form">
            <Select
              value={kind}
              options={Object.entries(KIND_LABELS).map(([value, text]) => ({
                value,
                label: text,
              }))}
              disabled={expectedVersion > 0}
              onChange={(value) => resetForm(value as SchemaKind)}
            />
            <Input
              value={key}
              disabled={expectedVersion > 0}
              onChange={(event) => setKey(event.target.value)}
              placeholder="custom.indicator"
              data-testid="kg-schema-key"
            />
            <Input
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              placeholder="显示名称"
              data-testid="kg-schema-label"
            />
            {kind === "entity_type" ? (
              <Select
                allowClear
                value={parentKey}
                options={entityOptions}
                onChange={(value) => setParentKey(value ?? null)}
                placeholder="父类型（可选）"
              />
            ) : null}
            {kind === "relation_type" ? (
              <>
                <Select
                  value={sourceType}
                  options={entityOptions}
                  onChange={setSourceType}
                  placeholder="起点实体类型"
                  data-testid="kg-schema-source-type"
                />
                <Select
                  value={targetType}
                  options={entityOptions}
                  onChange={setTargetType}
                  placeholder="终点实体类型"
                  data-testid="kg-schema-target-type"
                />
                <Space>
                  <span>对称</span>
                  <Switch checked={symmetric} onChange={setSymmetric} />
                  <span>传递</span>
                  <Switch checked={transitive} onChange={setTransitive} />
                </Space>
              </>
            ) : null}
            {kind === "property" ? (
              <>
                <Select
                  value={ownerKind}
                  options={[
                    { value: "entity_type", label: "实体属性" },
                    { value: "relation_type", label: "关系属性" },
                  ]}
                  onChange={(value) => {
                    setOwnerKind(value);
                    setOwnerKey(null);
                  }}
                />
                <Select
                  value={ownerKey}
                  options={ownerKind === "entity_type" ? entityOptions : relationOptions}
                  onChange={setOwnerKey}
                  placeholder="属性所属类型"
                />
                <Select
                  value={valueType}
                  options={[
                    "string",
                    "integer",
                    "number",
                    "boolean",
                    "date",
                    "datetime",
                    "enum",
                    "uri",
                    "json",
                    "entity_ref",
                  ].map((value) => ({ value, label: value }))}
                  onChange={setValueType}
                />
              </>
            ) : null}
            <Space>
              <Button
                type="primary"
                disabled={!valid}
                loading={submitting}
                onClick={() => void submit()}
                data-testid="kg-schema-submit"
              >
                {expectedVersion ? "保存修改" : "创建"}
              </Button>
              {expectedVersion ? (
                <Button onClick={() => resetForm(kind)}>取消编辑</Button>
              ) : null}
            </Space>
          </div>
          <div>
            {customItems.length === 0 ? (
              <Empty description="暂无自定义 Schema" />
            ) : (
              <List
                dataSource={customItems}
                renderItem={(item) => (
                  <List.Item
                    key={`${item.kind}:${item.key}`}
                    actions={[
                      <Button key="edit" size="small" onClick={() => editItem(item)}>
                        编辑
                      </Button>,
                      <Popconfirm
                        key="deprecate"
                        title="确认弃用？"
                        onConfirm={() => void deprecate(item)}
                      >
                        <Button
                          size="small"
                          danger
                          disabled={item.status === "deprecated"}
                        >
                          弃用
                        </Button>
                      </Popconfirm>,
                    ]}
                  >
                    <Space direction="vertical" size={1}>
                      <Space>
                        <Typography.Text strong>{item.label}</Typography.Text>
                        <Tag>{KIND_LABELS[item.kind]}</Tag>
                        <Tag color={item.status === "active" ? "green" : "default"}>
                          {item.status}
                        </Tag>
                      </Space>
                      <Typography.Text type="secondary" className="!text-xs">
                        {item.key} · v{item.version}
                      </Typography.Text>
                    </Space>
                  </List.Item>
                )}
              />
            )}
          </div>
        </div>
      </Modal>
    </>
  );
}

export default KnowledgeGraphSchemaManager;
