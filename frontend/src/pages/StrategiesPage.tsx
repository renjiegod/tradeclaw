import { ReloadOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Descriptions, Drawer, Empty, Input, Modal, Space, Spin, Table, Tabs, Tag, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  compileStrategyDefinition,
  deleteStrategyDefinition,
  deleteStrategyDefinitions,
  getStrategyDefinition,
  listStrategyDefinitions,
  updateStrategyDefinition,
} from "../api";
import { JsonCodeBlock } from "../components/JsonCodeBlock";
import { PageIntro } from "../components/PageIntro";
import { StrategyFileTree } from "../components/StrategyFileTree";
import { usePageRefreshToken } from "../pageRefreshContext";
import type {
  StrategyDefinitionCompileResult,
  StrategyDefinitionDetail,
  StrategyDefinitionRow,
} from "../types";
import { formatDateTimeUtc8 } from "../utils/datetime";

const CARD_CLASSNAME = "!border !border-shell-line !bg-card-bg shadow-shell-card";
const SECTION_CARD_CLASSNAME =
  "!overflow-hidden !border !border-shell-line !bg-card-bg shadow-shell-card";

function JsonSection({
  title,
  value,
}: {
  title: string;
  value: Record<string, unknown> | string[] | null | undefined;
}) {
  return (
    <Card size="small" title={title} className={CARD_CLASSNAME}>
      <JsonCodeBlock value={value ?? {}} />
    </Card>
  );
}

function StrategySection({
  eyebrow,
  title,
  description,
  count,
  listTitle,
  list,
}: {
  eyebrow: string;
  title: string;
  description: string;
  count: number;
  listTitle: string;
  list: React.ReactNode;
}) {
  return (
    <Card className={SECTION_CARD_CLASSNAME} bodyStyle={{ padding: 0 }}>
      <div className="border-b border-shell-line bg-[linear-gradient(135deg,rgba(15,23,42,0.03),rgba(148,163,184,0.08))] px-5 py-5">
        <Space direction="vertical" size={4} className="w-full">
          <Typography.Text className="text-[11px] font-semibold uppercase tracking-[0.24em] text-shell-muted">
            {eyebrow}
          </Typography.Text>
          <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
            <div className="space-y-1">
              <Typography.Title level={4} className="!mb-0 !font-display !text-shell-ink">
                {title}
              </Typography.Title>
              <Typography.Paragraph className="!mb-0 text-sm text-shell-muted">{description}</Typography.Paragraph>
            </div>
            <div className="inline-flex min-w-[88px] flex-col rounded-2xl border border-shell-line bg-white/75 px-4 py-2 text-right backdrop-blur">
              <Typography.Text className="text-[11px] uppercase tracking-[0.18em] text-shell-muted">
                Count
              </Typography.Text>
              <Typography.Text className="text-2xl font-semibold text-shell-ink">{count}</Typography.Text>
            </div>
          </div>
        </Space>
      </div>
      <div className="p-4">
        <Typography.Title level={5} className="!mb-3 !text-shell-ink">
          {listTitle}
        </Typography.Title>
        {list}
      </div>
    </Card>
  );
}

export function StrategiesPage() {
  const pageRefreshToken = usePageRefreshToken();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [definitions, setDefinitions] = useState<StrategyDefinitionRow[]>([]);
  const [selectedDefinitionId, setSelectedDefinitionId] = useState<string | null>(null);
  const [selectedDefinitionIds, setSelectedDefinitionIds] = useState<string[]>([]);
  const [activeTab, setActiveTab] = useState("definitions");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [definitionDetail, setDefinitionDetail] = useState<StrategyDefinitionDetail | null>(null);
  const [definitionDetailLoading, setDefinitionDetailLoading] = useState(false);
  const [compileLoading, setCompileLoading] = useState(false);
  const [deleteLoading, setDeleteLoading] = useState(false);
  const [compileResult, setCompileResult] = useState<StrategyDefinitionCompileResult | null>(null);

  // ---- Rename definition state ----
  // The rename modal targets a single definition row; ``renameValue`` holds the
  // editable display name. On success we ``load()`` so the table and drawer
  // title reflect the new name.
  const [renameTarget, setRenameTarget] = useState<StrategyDefinitionRow | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [renameLoading, setRenameLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const definitionsRes = await listStrategyDefinitions();
      setDefinitions(definitionsRes.items ?? []);
      setSelectedDefinitionIds((current) =>
        current.filter((definitionId) => (definitionsRes.items ?? []).some((item) => item.definition_id === definitionId)),
      );
    } catch (e: unknown) {
      setDefinitions([]);
      setSelectedDefinitionIds([]);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, pageRefreshToken]);

  useEffect(() => {
    setSelectedDefinitionId((current) => {
      if (definitions.length === 0) return null;
      if (current && definitions.some((item) => item.definition_id === current)) return current;
      return null;
    });
  }, [definitions]);

  useEffect(() => {
    if (!selectedDefinitionId) {
      setDefinitionDetail(null);
      setCompileResult(null);
      return;
    }
    let active = true;
    setDefinitionDetailLoading(true);
    void getStrategyDefinition(selectedDefinitionId)
      .then((detail) => {
        if (active) {
          setDefinitionDetail(detail);
          setCompileResult(null);
        }
      })
      .catch((e: unknown) => {
        if (active) {
          setDefinitionDetail(null);
          setError(e instanceof Error ? e.message : String(e));
        }
      })
      .finally(() => {
        if (active) {
          setDefinitionDetailLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, [selectedDefinitionId]);

  const clearDeletedDefinitionSelection = useCallback((deletedDefinitionIds: string[]) => {
    setSelectedDefinitionIds((current) => current.filter((definitionId) => !deletedDefinitionIds.includes(definitionId)));
    setSelectedDefinitionId((current) => (current && deletedDefinitionIds.includes(current) ? null : current));
    setDefinitionDetail((current) =>
      current && deletedDefinitionIds.includes(current.definition_id) ? null : current,
    );
    setCompileResult((current) =>
      current && deletedDefinitionIds.includes(current.definition_id) ? null : current,
    );
    if (selectedDefinitionId && deletedDefinitionIds.includes(selectedDefinitionId)) {
      setDrawerOpen(false);
    }
  }, [selectedDefinitionId]);

  const openRenameDefinition = useCallback((row: StrategyDefinitionRow) => {
    setRenameTarget(row);
    setRenameValue(row.name);
  }, []);

  const closeRenameDefinition = useCallback(() => {
    if (renameLoading) return;
    setRenameTarget(null);
    setRenameValue("");
  }, [renameLoading]);

  const handleRenameDefinition = useCallback(async () => {
    if (!renameTarget) return;
    const nextName = renameValue.trim();
    if (!nextName) {
      message.error("名称不能为空");
      return;
    }
    if (nextName === renameTarget.name) {
      setRenameTarget(null);
      setRenameValue("");
      return;
    }
    setRenameLoading(true);
    try {
      const detail = await updateStrategyDefinition(renameTarget.definition_id, { name: nextName });
      setDefinitionDetail((current) =>
        current && current.definition_id === detail.definition_id ? detail : current,
      );
      setRenameTarget(null);
      setRenameValue("");
      await load();
      message.success("已更新策略定义名称");
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setRenameLoading(false);
    }
  }, [load, renameTarget, renameValue]);

  const handleDeleteDefinition = useCallback((row: StrategyDefinitionRow) => {
    Modal.confirm({
      title: "删除策略定义",
      content: `确定删除「${row.name}」吗？`,
      okText: "删除",
      okButtonProps: { danger: true, loading: deleteLoading },
      cancelText: "取消",
      onOk: async () => {
        setDeleteLoading(true);
        try {
          await deleteStrategyDefinition(row.definition_id);
          clearDeletedDefinitionSelection([row.definition_id]);
          await load();
          message.success("已删除策略定义");
        } catch (e: unknown) {
          message.error(e instanceof Error ? e.message : String(e));
        } finally {
          setDeleteLoading(false);
        }
      },
    });
  }, [clearDeletedDefinitionSelection, deleteLoading, load]);

  const handleBulkDeleteDefinitions = useCallback(() => {
    if (selectedDefinitionIds.length === 0) return;
    Modal.confirm({
      title: "批量删除策略定义",
      content: `确定删除已选中的 ${selectedDefinitionIds.length} 个策略定义吗？`,
      okText: "删除",
      okButtonProps: { danger: true, loading: deleteLoading },
      cancelText: "取消",
      onOk: async () => {
        setDeleteLoading(true);
        try {
          await deleteStrategyDefinitions(selectedDefinitionIds);
          clearDeletedDefinitionSelection(selectedDefinitionIds);
          await load();
          message.success(`已删除 ${selectedDefinitionIds.length} 个策略定义`);
        } catch (e: unknown) {
          message.error(e instanceof Error ? e.message : String(e));
        } finally {
          setDeleteLoading(false);
        }
      },
    });
  }, [clearDeletedDefinitionSelection, deleteLoading, load, selectedDefinitionIds]);

  const handleCompileDefinition = useCallback(async () => {
    if (!selectedDefinitionId) return;
    setCompileLoading(true);
    try {
      const result = await compileStrategyDefinition(selectedDefinitionId);
      setCompileResult(result);
    } catch (e: unknown) {
      setCompileResult(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCompileLoading(false);
    }
  }, [selectedDefinitionId]);

  const selectedDefinition = useMemo(
    () => definitions.find((item) => item.definition_id === selectedDefinitionId) ?? null,
    [definitions, selectedDefinitionId],
  );

  const definitionColumns: ColumnsType<StrategyDefinitionRow> = useMemo(
    () => [
      {
        title: "名称",
        dataIndex: "name",
        key: "name",
        render: (value: string, row) => (
          <Button
            type="link"
            className="!h-auto !px-0 !py-0 text-left whitespace-normal break-words"
            onClick={() => {
              setSelectedDefinitionId(row.definition_id);
              setDrawerOpen(true);
            }}
          >
            {value}
          </Button>
        ),
      },
      {
        title: "Definition ID",
        dataIndex: "definition_id",
        key: "definition_id",
        render: (value: string) => (
          <Typography.Text className="font-mono text-xs" copyable={{ text: value }}>
            {value}
          </Typography.Text>
        ),
      },
      {
        title: "类名",
        dataIndex: "class_name",
        key: "class_name",
        render: (value: string) => <Typography.Text className="font-mono text-xs">{value}</Typography.Text>,
      },
      {
        title: "状态",
        dataIndex: "status",
        key: "status",
        width: 100,
        render: (value: string) => <Tag color={value === "active" ? "success" : "default"}>{value}</Tag>,
      },
      {
        title: "操作",
        key: "actions",
        width: 150,
        render: (_: unknown, row) => (
          <Space size={4}>
            <Button
              size="small"
              onClick={(event) => {
                event.stopPropagation();
                openRenameDefinition(row);
              }}
            >
              重命名
            </Button>
            <Button
              size="small"
              danger
              onClick={(event) => {
                event.stopPropagation();
                handleDeleteDefinition(row);
              }}
            >
              删除
            </Button>
          </Space>
        ),
      },
    ],
    [handleDeleteDefinition, openRenameDefinition],
  );

  const tabItems = useMemo(
    () => [
      {
        key: "definitions",
        label: `策略定义 (${definitions.length})`,
        children: (
          <StrategySection
            eyebrow="Definitions"
            title="策略定义"
            description="定义层负责策略代码契约、参数 schema 和能力声明，适合作为策略资产的入口。"
            count={definitions.length}
            listTitle="定义列表"
            list={
              <Space direction="vertical" size={12} className="w-full">
                <div className="flex justify-end">
                  <Button
                    danger
                    disabled={selectedDefinitionIds.length === 0}
                    loading={deleteLoading}
                    onClick={() => void handleBulkDeleteDefinitions()}
                  >
                    删除选中
                  </Button>
                </div>
                <Table<StrategyDefinitionRow>
                  rowKey="definition_id"
                  size="small"
                  loading={loading}
                  columns={definitionColumns}
                  dataSource={definitions}
                  rowSelection={{
                    selectedRowKeys: selectedDefinitionIds,
                    onChange: (keys) => setSelectedDefinitionIds(keys.map((key) => String(key))),
                  }}
                  pagination={false}
                  locale={{ emptyText: <Empty description="暂无策略定义" /> }}
                />
              </Space>
            }
          />
        ),
      },
    ],
    [
      definitionColumns,
      definitions,
      deleteLoading,
      handleBulkDeleteDefinitions,
      loading,
      selectedDefinitionIds,
    ],
  );

  return (
    <Space direction="vertical" size={16} className="w-full">
      <PageIntro
        title="策略库"
        description="浏览、选择和查看策略定义详情。"
        extra={
          <Button className="rounded-xl" icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
            刷新
          </Button>
        }
      />

      {error ? (
        <Typography.Paragraph type="danger" className="rounded-2xl border border-red-200 bg-red-50 px-4 py-3">
          加载失败：{error}
        </Typography.Paragraph>
      ) : null}

      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={tabItems}
        destroyInactiveTabPane={false}
        className="[&_.ant-tabs-nav]:mb-4 [&_.ant-tabs-tab]:rounded-t-2xl [&_.ant-tabs-tab]:px-4 [&_.ant-tabs-tab-active]:bg-card-bg"
      />

      <Drawer
        title={definitionDetail?.name ?? selectedDefinition?.name ?? "策略定义详情"}
        placement="right"
        width="min(88vw, 860px)"
        onClose={() => setDrawerOpen(false)}
        open={drawerOpen && selectedDefinition !== null}
        maskClosable
        destroyOnClose={false}
      >
        {definitionDetailLoading ? (
            <div className="flex min-h-[240px] items-center justify-center">
              <Spin />
            </div>
          ) : definitionDetail ? (
            <Space direction="vertical" size={12} className="w-full">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <Typography.Text className="text-sm text-shell-muted">
                  使用后端编译器验证当前持久化源码是否能真实编译。
                </Typography.Text>
                <Space>
                  <Button
                    onClick={() =>
                      openRenameDefinition({
                        definition_id: definitionDetail.definition_id,
                        name: definitionDetail.name,
                      } as StrategyDefinitionRow)
                    }
                  >
                    重命名
                  </Button>
                  <Button onClick={() => void handleCompileDefinition()} loading={compileLoading}>
                    编译验证
                  </Button>
                </Space>
              </div>
              <Descriptions size="small" column={1} bordered>
                <Descriptions.Item label="Definition ID">{definitionDetail.definition_id}</Descriptions.Item>
                <Descriptions.Item label="名称">{definitionDetail.name}</Descriptions.Item>
                <Descriptions.Item label="类名">
                  <Typography.Text className="font-mono text-xs">{definitionDetail.class_name}</Typography.Text>
                </Descriptions.Item>
                <Descriptions.Item label="API 版本">{definitionDetail.api_version}</Descriptions.Item>
                <Descriptions.Item label="状态">{definitionDetail.status}</Descriptions.Item>
                <Descriptions.Item label="代码 Hash">
                  <Typography.Text className="font-mono text-xs">{definitionDetail.code_hash}</Typography.Text>
                </Descriptions.Item>
                <Descriptions.Item label="生成模型">
                  {definitionDetail.generation_model || "—"}
                </Descriptions.Item>
                <Descriptions.Item label="更新时间">
                  {definitionDetail.updated_at
                    ? formatDateTimeUtc8(definitionDetail.updated_at, definitionDetail.updated_at)
                    : "—"}
                </Descriptions.Item>
              </Descriptions>
              <JsonSection title="输入契约" value={definitionDetail.input_contract} />
              <JsonSection title="参数 Schema" value={definitionDetail.parameter_schema} />
              <JsonSection title="默认参数" value={definitionDetail.default_parameters} />
              <JsonSection title="能力声明" value={definitionDetail.capabilities} />
              <JsonSection title="来源信息" value={definitionDetail.provenance} />
              <JsonSection title="生成元数据" value={definitionDetail.generation_metadata} />
              {compileResult ? (
                <Card size="small" title="编译结果" className={CARD_CLASSNAME}>
                  <Space direction="vertical" size={12} className="w-full">
                    <Alert
                      type={compileResult.success ? "success" : "error"}
                      showIcon
                      message={compileResult.success ? "编译成功" : "编译失败"}
                      description={
                        compileResult.success
                          ? `已成功编译为 ${compileResult.qualified_name ?? definitionDetail.class_name}`
                          : "后端编译器未能通过当前源码校验，错误详情如下。"
                      }
                    />
                    <Descriptions size="small" column={1} bordered>
                      <Descriptions.Item label="代码 Hash">
                        <Typography.Text className="font-mono text-xs">{compileResult.code_hash}</Typography.Text>
                      </Descriptions.Item>
                      <Descriptions.Item label="限定类名">
                        <Typography.Text className="font-mono text-xs">
                          {compileResult.qualified_name ?? "—"}
                        </Typography.Text>
                      </Descriptions.Item>
                    </Descriptions>
                    {compileResult.descriptor ? (
                      <>
                        <JsonSection title="编译产物参数 Schema" value={compileResult.descriptor.parameter_schema} />
                        <JsonSection title="编译产物能力声明" value={compileResult.descriptor.capabilities} />
                      </>
                    ) : null}
                    {!compileResult.success ? (
                      <Card size="small" title="错误详情" className={CARD_CLASSNAME}>
                        <Typography.Paragraph className="!mb-0 whitespace-pre-wrap break-words font-mono text-xs text-red-600">
                          {compileResult.errors.join("\n")}
                        </Typography.Paragraph>
                      </Card>
                    ) : null}
                  </Space>
                </Card>
              ) : null}
              <Card size="small" title="生成提示词" className={CARD_CLASSNAME}>
                <Typography.Paragraph className="!mb-0 whitespace-pre-wrap break-words text-sm">
                  {definitionDetail.generation_prompt || "—"}
                </Typography.Paragraph>
              </Card>
              <Card size="small" title="源码文件" className={CARD_CLASSNAME}>
                <StrategyFileTree files={definitionDetail.files} />
              </Card>
            </Space>
          ) : null}
      </Drawer>

      <Modal
        title="重命名策略定义"
        open={renameTarget !== null}
        onOk={() => void handleRenameDefinition()}
        onCancel={closeRenameDefinition}
        okText="保存"
        cancelText="取消"
        okButtonProps={{ loading: renameLoading }}
        cancelButtonProps={{ disabled: renameLoading }}
        confirmLoading={renameLoading}
        maskClosable={!renameLoading}
        destroyOnClose
      >
        <Space direction="vertical" size={8} className="w-full">
          <Typography.Text className="text-sm text-shell-muted">
            Definition ID：
            <Typography.Text className="font-mono text-xs">{renameTarget?.definition_id}</Typography.Text>
          </Typography.Text>
          <Input
            value={renameValue}
            onChange={(event) => setRenameValue(event.target.value)}
            placeholder="输入新的策略定义名称"
            maxLength={255}
            disabled={renameLoading}
            onPressEnter={() => void handleRenameDefinition()}
            autoFocus
          />
        </Space>
      </Modal>
    </Space>
  );
}
