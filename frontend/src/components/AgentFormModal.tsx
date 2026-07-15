import React from "react";
import { LockOutlined } from "@ant-design/icons";
import type {
  Agent,
  AgentContextCompaction,
  AgentToolConfig,
  AgentPromptTemplate,
  CreateAgentPayload,
} from "../types";
import {
  createAssistantAgent,
  updateAssistantAgent,
  listAssistantAgentPromptTemplates,
  listAssistantAgentTools,
  listAssistantAgentSkills,
  listModelRoutes,
} from "../api";
import {
  Alert,
  Button,
  Collapse,
  Form,
  Input,
  InputNumber,
  message,
  Modal,
  Radio,
  Row,
  Col,
  Select,
  Space,
  Switch,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { MarkdownEditor } from "./MarkdownEditor";
import { MultiSelectWithBulkActions } from "./MultiSelectWithBulkActions";

type Props = {
  agent?: Agent;
  onSaved: (agent: Agent) => void;
  onClose: () => void;
};

type LoadMode = "base" | "deferred";

/** Which advanced-collapse panel each optional / defaulted field lives in, so
 * a validation failure inside a collapsed panel can auto-expand it (see
 * onFinishFailed) instead of failing invisibly. Nested Form.Item names (e.g.
 * ["context_compaction", "mode"]) match via their first path segment because
 * onFinishFailed flattens every name path into a string set. */
const ADVANCED_PANEL_FIELDS: Record<string, string[]> = {
  tools: ["tool_names", "tool_load_mode", "skill_names"],
  runtime: ["max_turns"],
  ctx: ["context_compaction"],
};

export const AgentFormModal: React.FC<Props> = ({ agent, onSaved, onClose }) => {
  const [form] = Form.useForm();
  const [saving, setSaving] = React.useState(false);
  // Advanced sections stay collapsed by default; validation failures inside a
  // collapsed panel expand it (see onFinishFailed) so errors are never hidden.
  const [openPanels, setOpenPanels] = React.useState<string[]>([]);
  const [availableModelRoutes, setAvailableModelRoutes] = React.useState<Array<{ route_name: string; target_model: string }>>([]);
  const [availablePromptTemplates, setAvailablePromptTemplates] = React.useState<AgentPromptTemplate[]>([]);
  const [availableTools, setAvailableTools] = React.useState<Array<{ name: string; description: string }>>([]);
  const [availableSkills, setAvailableSkills] = React.useState<Array<{ name: string; description: string }>>([]);
  const [loadingOptions, setLoadingOptions] = React.useState(true);

  const loadOptions = React.useCallback(async () => {
    setLoadingOptions(true);
    try {
      const [routesResult, promptTemplatesResult, toolsResult, skillsResult] = await Promise.all([
        listModelRoutes(),
        listAssistantAgentPromptTemplates(),
        listAssistantAgentTools(),
        listAssistantAgentSkills(),
      ]);
      setAvailableModelRoutes(routesResult.items ?? []);
      setAvailablePromptTemplates(promptTemplatesResult.items ?? []);
      setAvailableTools(toolsResult.tools ?? []);
      setAvailableSkills(skillsResult.items ?? []);
    } catch (err) {
      // Option loading is non-fatal (the form stays usable with empty option
      // lists), but the user must see WHY selects are empty instead of a silent
      // console.error that looks like "no data".
      const content = err instanceof Error ? err.message : String(err);
      message.warning(`部分选项加载失败，下拉框可能为空：${content}`);
    } finally {
      setLoadingOptions(false);
    }
  }, []);

  React.useEffect(() => {
    void loadOptions();
  }, [loadOptions]);

  const defaultContextCompaction: AgentContextCompaction = React.useMemo(() => ({
    enabled: true,
    mode: "auto",
    trigger_strategy: "token_estimate",
    auto_threshold_tokens: 24000,
    warning_threshold_tokens: 20000,
    preserve_recent_messages: 12,
    preserve_recent_tool_pairs: 4,
    micro_compaction_enabled: true,
    tool_result_max_chars: 4000,
    full_compaction_enabled: true,
    summary_model_route_name: "",
    allow_slash_compact: true,
  }), []);

  // Tool load modes keyed by tool NAME (not array index). The old array-index
  // form name (["tool_configs", safeIndex, "load_mode"]) silently desynced when
  // tool order changed — safeIndex fell back to 0 and wrote the wrong row.
  // Keying by name round-trips deterministically regardless of ordering.
  const initialToolLoadModes = React.useMemo<Record<string, LoadMode>>(() => {
    const map: Record<string, LoadMode> = {};
    for (const config of agent?.tool_configs ?? []) {
      if (config?.name) {
        map[config.name] = config.load_mode === "deferred" ? "deferred" : "base";
      }
    }
    return map;
  }, [agent]);

  const selectedToolNames = Form.useWatch("tool_names", form) as string[] | undefined;
  const selectedPromptTemplateId = Form.useWatch("system_prompt_template_id", form) as string | undefined;
  const selectedPromptTemplate = React.useMemo(
    () => availablePromptTemplates.find((template) => template.template_id === selectedPromptTemplateId),
    [availablePromptTemplates, selectedPromptTemplateId],
  );

  // The builtin main agent is code-fixed: its name, system prompt, tools and
  // skills are controlled in code. Only the three runtime knobs (model route,
  // context compaction, max turns) are editable, so we render a restricted form
  // and submit only those fields — sending any locked field triggers a 403.
  const isBuiltin = agent?.is_builtin === true;

  const handleSubmit = async (values: Record<string, unknown>) => {
    setSaving(true);
    try {
      const contextCompaction = {
        ...defaultContextCompaction,
        ...((values.context_compaction as Partial<AgentContextCompaction> | undefined) ?? {}),
      };
      if (isBuiltin && agent) {
        // Restricted payload: only the runtime knobs. Omitting name /
        // system_prompt / tools / skills / template avoids the backend's
        // ``agent_builtin_immutable`` 403.
        const builtinPayload: Partial<CreateAgentPayload> = {
          model_route_name: (values.model_route_name as string) || "",
          context_compaction: contextCompaction,
          max_turns: values.max_turns as number,
        };
        const savedBuiltin = await updateAssistantAgent(agent.id, builtinPayload);
        onSaved(savedBuiltin);
        return;
      }
      const selectedNames = ((values.tool_names as string[]) || []).filter(Boolean);
      const loadModes = (values.tool_load_mode as Record<string, string> | undefined) ?? {};
      const toolConfigs: AgentToolConfig[] = selectedNames.map((name) => ({
        name,
        load_mode: loadModes[name] === "deferred" ? "deferred" : "base",
      }));
      const templateId = (values.system_prompt_template_id as string | undefined) || null;
      // Template mode is the canonical "link to .j2" form — store an empty
      // raw prompt so the runtime resolver always reaches the live template
      // and there's no shadow snapshot to drift from the file on disk.
      const systemPromptToSend = templateId ? "" : ((values.system_prompt as string) || "");
      const payload: CreateAgentPayload = {
        name: values.name as string,
        status: values.status as "active" | "inactive",
        system_prompt: systemPromptToSend,
        system_prompt_template_id: templateId,
        model_route_name: (values.model_route_name as string) || "",
        tool_configs: toolConfigs,
        tool_names: selectedNames,
        skill_names: (values.skill_names as string[]) || [],
        max_turns: values.max_turns as number,
        context_compaction: contextCompaction,
      };
      const saved = agent
        ? await updateAssistantAgent(agent.id, payload)
        : await createAssistantAgent(payload);
      onSaved(saved);
    } catch (err) {
      const content = err instanceof Error ? err.message : String(err);
      message.error(`保存失败：${content}`);
    } finally {
      setSaving(false);
    }
  };

  const modelRouteOptions = availableModelRoutes.map((route) => ({
    label: route.target_model ? `${route.route_name} → ${route.target_model}` : route.route_name,
    value: route.route_name,
  }));

  return (
    <Modal
      title={agent ? "编辑 Agent" : "新建 Agent"}
      open
      onCancel={onClose}
      destroyOnHidden
      width={840}
      styles={{ body: { maxHeight: "72vh", overflowY: "auto", paddingRight: 8 } }}
      footer={[
        <Button key="cancel" autoInsertSpace={false} onClick={onClose}>
          取消
        </Button>,
        <Button key="submit" type="primary" autoInsertSpace={false} loading={saving} onClick={() => form.submit()}>
          {saving ? "保存中…" : agent ? "保存" : "创建"}
        </Button>,
      ]}
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          name: agent?.name || "",
          status: agent?.status || "active",
          system_prompt: agent?.system_prompt || "",
          system_prompt_template_id: agent?.system_prompt_template_id || undefined,
          model_route_name: agent?.model_route_name || "",
          tool_names: agent?.tool_names || [],
          tool_load_mode: initialToolLoadModes,
          skill_names: agent?.skill_names || [],
          max_turns: agent?.max_turns || 6,
          context_compaction: {
            ...defaultContextCompaction,
            ...(agent?.context_compaction || {}),
          },
        }}
        scrollToFirstError
        onFinishFailed={({ errorFields }) => {
          const failed = new Set(errorFields.flatMap((field) => field.name.map(String)));
          const panelsToOpen = Object.entries(ADVANCED_PANEL_FIELDS)
            .filter(([, fields]) => fields.some((name) => failed.has(name)))
            .map(([key]) => key);
          if (panelsToOpen.length > 0) {
            setOpenPanels((prev) => Array.from(new Set([...prev, ...panelsToOpen])));
          }
        }}
        onFinish={handleSubmit}
      >
        {isBuiltin && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message="固定主智能体"
            description="仅可编辑运行配置（使用的模型 / 上下文压缩 / 最大轮数）；名称、提示词、Skills、Tools 由代码控制。"
          />
        )}
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item
              name="name"
              label={
                isBuiltin ? (
                  <Space size={4}>
                    <span>名称</span>
                    <Tooltip title="代码控制，不可编辑">
                      <LockOutlined style={{ color: "var(--ant-color-text-tertiary)" }} />
                    </Tooltip>
                  </Space>
                ) : (
                  "名称"
                )
              }
              rules={[{ required: true, message: "请输入名称" }]}
            >
              <Input disabled={isBuiltin} />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="status" label="状态">
              <Select>
                <Select.Option value="active">启用</Select.Option>
                <Select.Option value="inactive">停用</Select.Option>
              </Select>
            </Form.Item>
          </Col>
        </Row>
        {isBuiltin && (
          <Form.Item label="系统提示词">
            <Typography.Text type="secondary">
              系统提示词：main_agent.j2（代码控制，不可编辑）
            </Typography.Text>
          </Form.Item>
        )}
        {!isBuiltin && (
          <>
            <div style={{ marginBottom: 8 }} data-testid="prompt-mode-indicator">
              {selectedPromptTemplate ? (
                <Tag color="blue" aria-label="prompt-mode-linked">
                  关联 .j2 模板
                </Tag>
              ) : (
                <Tag color="default" aria-label="prompt-mode-custom">
                  自定义提示词
                </Tag>
              )}
            </div>
            <Form.Item
              name="system_prompt_template_id"
              label="提示词模板"
              extra={
                selectedPromptTemplate
                  ? "已关联磁盘上的 .j2 文件——模板改动会自动生效到新会话。"
                  : "选择模板以关联其 .j2 文件，或留空以在下方撰写自定义提示词。"
              }
            >
              <Select
                placeholder="选择要关联的模板"
                allowClear
                showSearch
                optionFilterProp="label"
                loading={loadingOptions}
                notFoundContent={loadingOptions ? "加载中…" : "暂无可用模板"}
                options={availablePromptTemplates.map((template) => ({
                  label: template.name,
                  value: template.template_id,
                  title: template.description,
                }))}
              />
            </Form.Item>
            {selectedPromptTemplate ? (
              <Form.Item
                label="模板内容"
                extra="只读——如需修改请直接编辑底层 .j2 文件。"
              >
                <div
                  data-testid="prompt-template-readonly-preview"
                  className="rounded-card border border-shell-line bg-shell-bg"
                  style={{
                    minHeight: 120,
                    maxHeight: 320,
                    overflowY: "auto",
                    padding: "10px 14px",
                  }}
                >
                  <Typography.Text type="secondary" style={{ display: "block", marginBottom: 8 }}>
                    {selectedPromptTemplate.description}
                  </Typography.Text>
                  <div className="markdown-body" style={{ fontSize: 13, lineHeight: 1.6 }}>
                    <Markdown remarkPlugins={[remarkGfm]}>
                      {selectedPromptTemplate.system_prompt}
                    </Markdown>
                  </div>
                </div>
              </Form.Item>
            ) : (
              <Form.Item
                name="system_prompt"
                label="系统提示词"
                extra="未关联模板时直接使用此处内容。"
                rules={[
                  {
                    validator: async (_, value) => {
                      if (selectedPromptTemplateId || String(value || "").trim()) {
                        return;
                      }
                      throw new Error("未选择模板时必须填写系统提示词");
                    },
                  },
                ]}
              >
                <MarkdownEditor minHeight={120} />
              </Form.Item>
            )}
          </>
        )}
        <Form.Item name="model_route_name" label="使用的模型">
          <Select
            placeholder="留空使用系统默认"
            allowClear
            showSearch
            optionFilterProp="label"
            loading={loadingOptions}
            notFoundContent={loadingOptions ? "加载中…" : "暂无可用路由"}
            options={modelRouteOptions}
          />
        </Form.Item>
        {isBuiltin && (
          <Form.Item label="工具与 Skills">
            <Typography.Text type="secondary">
              工具与 Skills：自动加载全部 in-process 工具与全部启用 skills（代码控制，不可编辑）。
            </Typography.Text>
          </Form.Item>
        )}
        {/* 高级设置：以下面板内全部字段均有默认值或可留空，默认折叠；
            校验失败时 onFinishFailed 会自动展开对应面板。 */}
        <Collapse
          ghost
          activeKey={openPanels}
          onChange={(keys) => setOpenPanels(Array.isArray(keys) ? keys.map(String) : [String(keys)])}
          items={[
            ...(!isBuiltin && !loadingOptions
              ? [
                  {
                    key: "tools",
                    label: "工具与 Skills（可选，留空则不加载额外工具）",
                    forceRender: true,
                    children: (
                      <>
                        <Form.Item name="tool_names" label="工具">
                          <MultiSelectWithBulkActions
                            placeholder="选择工具"
                            loading={loadingOptions}
                            options={availableTools.map((tool) => ({ label: tool.name, value: tool.name }))}
                          />
                        </Form.Item>
                        {(selectedToolNames || []).length > 0 && (
                          <Form.Item label="工具加载模式">
                            <Space direction="vertical" style={{ width: "100%" }}>
                              {(selectedToolNames || []).map((toolName, idx) => (
                                <Space
                                  key={toolName}
                                  align="center"
                                  style={{ display: "flex", justifyContent: "space-between", width: "100%" }}
                                >
                                  <span>{toolName}</span>
                                  <Form.Item
                                    label={idx === 0 ? "加载模式" : undefined}
                                    name={["tool_load_mode", toolName]}
                                    style={{ marginBottom: 0 }}
                                    initialValue="base"
                                  >
                                    <Radio.Group
                                      aria-label={`工具加载模式 ${toolName}`}
                                      optionType="button"
                                      buttonStyle="solid"
                                      options={[
                                        { label: "Base", value: "base" },
                                        { label: "Deferred", value: "deferred" },
                                      ]}
                                    />
                                  </Form.Item>
                                </Space>
                              ))}
                            </Space>
                          </Form.Item>
                        )}
                        <Form.Item name="skill_names" label="Skills">
                          <MultiSelectWithBulkActions
                            placeholder="选择 Skills"
                            loading={loadingOptions}
                            options={availableSkills.map((skill) => ({ label: skill.name, value: skill.name }))}
                          />
                        </Form.Item>
                      </>
                    ),
                  },
                ]
              : []),
            {
              key: "runtime",
              label: "运行限制（已填默认值，通常无需修改）",
              forceRender: true,
              children: (
                <Row gutter={16}>
                  <Col span={12}>
                    <Form.Item name="max_turns" label="最大轮数">
                      <InputNumber min={1} style={{ width: "100%" }} />
                    </Form.Item>
                  </Col>
                </Row>
              ),
            },
            {
              key: "ctx",
              label: "上下文压缩（已填默认值，通常无需修改）",
              forceRender: true,
              children: (
                <>
                  <Row gutter={16}>
                    <Col span={12}>
                      <Form.Item
                        name={["context_compaction", "enabled"]}
                        label="启用上下文压缩"
                        valuePropName="checked"
                      >
                        <Switch />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item name={["context_compaction", "mode"]} label="压缩模式">
                        <Select
                          options={[
                            { label: "自动", value: "auto" },
                            { label: "手动", value: "manual" },
                          ]}
                        />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item
                        name={["context_compaction", "auto_threshold_tokens"]}
                        label="自动压缩阈值（tokens）"
                      >
                        <InputNumber min={1} step={1000} style={{ width: "100%" }} />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item
                        name={["context_compaction", "warning_threshold_tokens"]}
                        label="预警阈值（tokens）"
                      >
                        <InputNumber min={1} step={1000} style={{ width: "100%" }} />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item
                        name={["context_compaction", "preserve_recent_messages"]}
                        label="保留最近消息数"
                      >
                        <InputNumber min={0} max={100} style={{ width: "100%" }} />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item
                        name={["context_compaction", "preserve_recent_tool_pairs"]}
                        label="保留最近工具对数"
                      >
                        <InputNumber min={0} max={50} style={{ width: "100%" }} />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item
                        name={["context_compaction", "tool_result_max_chars"]}
                        label="工具结果最大字符数"
                      >
                        <InputNumber min={32} step={100} style={{ width: "100%" }} />
                      </Form.Item>
                    </Col>
                    <Col span={12}>
                      <Form.Item
                        name={["context_compaction", "summary_model_route_name"]}
                        label="摘要用的模型"
                      >
                        <Select
                          placeholder="留空则复用主路由"
                          allowClear
                          showSearch
                          optionFilterProp="label"
                          loading={loadingOptions}
                          notFoundContent={loadingOptions ? "加载中…" : "暂无可用路由"}
                          options={modelRouteOptions}
                        />
                      </Form.Item>
                    </Col>
                  </Row>
                  <Space size={32} wrap style={{ marginBottom: 16 }}>
                    <Form.Item
                      name={["context_compaction", "micro_compaction_enabled"]}
                      label="微压缩"
                      valuePropName="checked"
                      style={{ marginBottom: 0 }}
                    >
                      <Switch />
                    </Form.Item>
                    <Form.Item
                      name={["context_compaction", "full_compaction_enabled"]}
                      label="全量压缩"
                      valuePropName="checked"
                      style={{ marginBottom: 0 }}
                    >
                      <Switch />
                    </Form.Item>
                    <Form.Item
                      name={["context_compaction", "allow_slash_compact"]}
                      label="允许 /compact"
                      valuePropName="checked"
                      style={{ marginBottom: 0 }}
                    >
                      <Switch />
                    </Form.Item>
                  </Space>
                </>
              ),
            },
          ]}
        />
      </Form>
    </Modal>
  );
};
