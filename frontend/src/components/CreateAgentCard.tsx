import { Button, Card, Col, Collapse, DatePicker, Form, Input, InputNumber, message, Row, Select, Switch, Tooltip, Typography } from "antd";
import React, { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useState } from "react";
import type { Dayjs } from "dayjs";

import { createTask, listAccounts, listDataProviders, listStrategyDefinitions, startTaskRun, updateTask } from "../api";
import { PANEL_CARD_CLASSNAME } from "../styles/classNames";
import {
  approvalTimeoutSecondsFromSettings,
  dataCacheFormValuesFromSettings,
  DEFAULT_SIGNAL_FORM,
  defaultSettingsObject,
  initialSettingsFromTask,
  lotSizeFromSettings,
  maxPositionRatioFromSettings,
  maxSingleOrderAmountFromSettings,
  maxTaskPositionAmountFromSettings,
  maxTaskPositionRatioFromSettings,
  minNotionalForApprovalFromSettings,
  normalizeOptionalText,
  rebalanceHysteresisFromSettings,
  reviewEquityFractionFromSettings,
} from "./createAgentSettings";
import type {
  Account,
  CreateTaskPayload,
  DataCacheSource,
  StrategyDefinitionRow,
  TaskDataCacheSettings,
  TaskStatus,
} from "../types";
import { SettingsJsonEditorModal } from "./SettingsJsonEditorModal";
import { CatalogSymbolSelect } from "./CatalogSymbolSelect";

export type CreateAgentCardHandle = {
  openSettingsJsonModal: () => void;
  applyCreatePatch: (
    values: Partial<CreateAgentFormValues>,
    options?: { onlyWhenEmpty?: boolean },
  ) => void;
};

type Props = {
  onCreated: () => void;
  /** When true, omit the card title (e.g. modal already shows a title). */
  hideCardTitle?: boolean;
  /** 'create' mode (default) or 'edit' mode */
  mode?: "create" | "edit";
  /** Pre-fill data when mode='edit' */
  editTask?: TaskStatus;
  /** When `none`, hide the inline Settings JSON button; parent should use ref + `openSettingsJsonModal`. */
  settingsJsonButtonPlacement?: "inline" | "none";
  /** Optional prefill values for create mode (used by duplicate flow). */
  createInitialValues?: Partial<CreateAgentFormValues> | null;
  /**
   * Restrict the selectable 运行模式 in create mode. The Tasks page passes the
   * modes that match the active tab (trading tab → paper/live, backtest tab →
   * backtest) so you cannot, e.g., create a backtest task from the trading tab.
   * When omitted, all run modes are offered. A single allowed mode locks the
   * selector. In edit mode the selectable modes are always constrained to the
   * task's current family (trading vs backtest).
   */
  allowedModes?: Array<"paper" | "live" | "backtest">;
};

export type CreateAgentFormValues = Omit<CreateTaskPayload, "universe" | "settings"> & {
  universe_symbols: string[];
  /** ``settings.position_constraints.max_single_order_amount`` — per-order cap; null = unlimited. */
  max_single_order_amount: number | null;
  /** ``settings.position_constraints.review_equity_fraction`` — review T uses min(cap, equity × f) when cap set. */
  review_equity_fraction: number;
  max_position_ratio: number;
  /** ``settings.position_constraints.lot_size`` — board lot (shares) for the
   * explicit target_quantity / target_exposure rebalance paths. 1 = whole-share;
   * A股 grids use 100. */
  lot_size: number;
  /** ``settings.position_constraints.rebalance_hysteresis_lots`` — rebalance dead
   * band in lots; sub-band rebalances are skipped to avoid grid churn. 0 = off. */
  rebalance_hysteresis_lots: number;
  /** ``settings.position_constraints.max_task_position_amount`` — task-level total
   * marked-to-market position amount cap; null = unlimited. */
  max_task_position_amount: number | null;
  /** ``settings.position_constraints.max_task_position_ratio`` — task-level total
   * marked-to-market position ratio cap; null = unlimited. */
  max_task_position_ratio: number | null;
  min_notional_for_approval: number;
  approval_timeout_seconds: number;
  backtest_range?: [Dayjs, Dayjs];
  backtest_market_profile?: string;
  backtest_bar_interval?: string;
  strategy_definition_id: string;
  strategy_parameter_overrides: string | Record<string, unknown>;
  strategy_execution_profile: string;
  /** ``settings.account_id`` — bound trading account; empty = inherit global. */
  account_id?: string;
  /** ``settings.data_cache.*`` — optional cache / backfill / continuity policy.
   * Every field is undefined by default; only explicitly-set fields are written
   * to ``settings.data_cache`` (omitting the whole block keeps backend defaults). */
  data_cache_source_priority?: DataCacheSource[];
  data_cache_local_first?: boolean;
  data_cache_auto_backfill?: boolean;
  data_cache_on_unverifiable_gap?: "fail" | "degrade";
};

/** When API fails, keep console usable with built-in ids (must match ``list_data_provider_ids`` core). */
const FALLBACK_DATA_PROVIDER_IDS = [
  "auto",
  "mock",
  "qmt",
  "akshare",
  "tushare",
  "baostock",
] as const;

const DATA_PROVIDER_LABELS: Record<string, string> = {
  auto: "自动（按全局配置解析为 QMT 或 Mock）",
  mock: "Mock（本地模拟行情/账户）",
  qmt: "QMT（需配置 data.qmt）",
  akshare: "Akshare（公开行情）",
  baostock: "Baostock（公开行情 + 交易所日历）",
};

const RUN_MODE_OPTIONS = [
  { label: "模拟盘", value: "paper" },
  { label: "实盘", value: "live" },
  { label: "回测", value: "backtest" },
] as const;

/** ``settings.data_cache.source_priority`` candidates (mirror backend enum). The
 * Select preserves selection order, which becomes the priority order. */
const DATA_CACHE_SOURCE_OPTIONS: { label: string; value: DataCacheSource }[] = [
  { label: "QMT", value: "qmt" },
  { label: "Baostock", value: "baostock" },
  { label: "Akshare", value: "akshare" },
  { label: "Tushare", value: "tushare" },
  { label: "Mock", value: "mock" },
];

const DATA_CACHE_ON_UNVERIFIABLE_GAP_OPTIONS = [
  { label: "fail（无法证明是停牌则拒绝写入，更安全）", value: "fail" },
  { label: "degrade（无法证明时降级放行）", value: "degrade" },
];

/** Which advanced-collapse panel each optional / defaulted field lives in, so a
 * validation failure inside a collapsed panel can auto-expand it instead of
 * failing invisibly. */
const ADVANCED_PANEL_FIELDS: Record<string, string[]> = {
  runtime: ["account_id", "data_provider", "description"],
  strategy_advanced: ["strategy_execution_profile", "strategy_parameter_overrides"],
  risk: [
    "max_single_order_amount",
    "max_position_ratio",
    "review_equity_fraction",
    "max_task_position_amount",
    "max_task_position_ratio",
    "min_notional_for_approval",
    "lot_size",
    "rebalance_hysteresis_lots",
    "approval_timeout_seconds",
  ],
  data_cache: [
    "data_cache_source_priority",
    "data_cache_local_first",
    "data_cache_auto_backfill",
    "data_cache_on_unverifiable_gap",
  ],
};

/** Collapse the flat ``data_cache_*`` form fields into the nested
 * ``settings.data_cache`` block, writing only the fields the user explicitly
 * set. Returns ``undefined`` when nothing is set so we never submit an empty
 * object that would override the backend's "omitted = defaults" semantics. */
function buildDataCacheSettings(values: {
  data_cache_source_priority?: DataCacheSource[];
  data_cache_local_first?: boolean;
  data_cache_auto_backfill?: boolean;
  data_cache_on_unverifiable_gap?: "fail" | "degrade";
}): TaskDataCacheSettings | undefined {
  const block: TaskDataCacheSettings = {};
  if (Array.isArray(values.data_cache_source_priority) && values.data_cache_source_priority.length > 0) {
    block.source_priority = [...values.data_cache_source_priority];
  }
  if (typeof values.data_cache_local_first === "boolean") {
    block.local_first = values.data_cache_local_first;
  }
  if (typeof values.data_cache_auto_backfill === "boolean") {
    block.auto_backfill = values.data_cache_auto_backfill;
  }
  const continuity: NonNullable<TaskDataCacheSettings["continuity"]> = {};
  if (values.data_cache_on_unverifiable_gap === "fail" || values.data_cache_on_unverifiable_gap === "degrade") {
    continuity.on_unverifiable_gap = values.data_cache_on_unverifiable_gap;
  }
  if (Object.keys(continuity).length > 0) {
    block.continuity = continuity;
  }
  return Object.keys(block).length > 0 ? block : undefined;
}

export const CreateAgentCard = forwardRef<CreateAgentCardHandle, Props>(function CreateAgentCard(
  {
    onCreated,
    hideCardTitle = false,
    mode = "create",
    editTask,
    settingsJsonButtonPlacement = "inline",
    createInitialValues = null,
    allowedModes,
  },
  ref,
) {
  const [form] = Form.useForm<CreateAgentFormValues>();

  const applyCreatePatch = useCallback(
    (values: Partial<CreateAgentFormValues>, options?: { onlyWhenEmpty?: boolean }) => {
      if (mode !== "create") {
        return;
      }
      if (!options?.onlyWhenEmpty) {
        form.setFieldsValue(values);
        return;
      }
      const nextValues: Partial<CreateAgentFormValues> = {};
      for (const [key, incoming] of Object.entries(values)) {
        const current = form.getFieldValue(key);
        const currentEmpty =
          current == null ||
          current === "" ||
          (Array.isArray(current) && current.length === 0);
        if (currentEmpty) {
          (nextValues as Record<string, unknown>)[key] = incoming;
        }
      }
      if (Object.keys(nextValues).length > 0) {
        form.setFieldsValue(nextValues);
      }
    },
    [form, mode],
  );
  const [loading, setLoading] = useState(false);
  // Advanced sections stay collapsed by default; validation failures inside a
  // collapsed panel expand it (see onFinishFailed) so errors are never hidden.
  const [openPanels, setOpenPanels] = useState<string[]>([]);
  const [settingsObject, setSettingsObject] = useState<Record<string, unknown>>(defaultSettingsObject);
  const [settingsModalOpen, setSettingsModalOpen] = useState(false);
  const [settingsModalInitialText, setSettingsModalInitialText] = useState("");
  const [dataProviderIds, setDataProviderIds] = useState<string[]>([...FALLBACK_DATA_PROVIDER_IDS]);
  const [dataProvidersLoading, setDataProvidersLoading] = useState(true);
  const [strategyDefinitionOptions, setStrategyDefinitionOptions] = useState<{ label: string; value: string }[]>([]);
  const [strategyDefinitionsLoading, setStrategyDefinitionsLoading] = useState(false);
  const [accountOptions, setAccountOptions] = useState<{ label: string; value: string }[]>([]);
  const [accountsLoading, setAccountsLoading] = useState(false);

  useEffect(() => {
    let active = true;
    setAccountsLoading(true);
    void listAccounts()
      .then((res) => {
        if (!active) {
          return;
        }
        const opts = (res.items ?? []).map((account: Account) => ({
          value: account.id,
          label: `${account.name} (${account.mode}${account.is_default ? ", 默认" : ""})`,
        }));
        setAccountOptions(opts);
      })
      .catch(() => {
        if (active) {
          setAccountOptions([]);
        }
      })
      .finally(() => {
        if (active) {
          setAccountsLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    setStrategyDefinitionsLoading(true);
    void listStrategyDefinitions()
      .then((res) => {
        if (!active) {
          return;
        }
        const opts = (res.items ?? []).map((definition: StrategyDefinitionRow) => ({
          value: definition.definition_id,
          label: `${definition.name} (${definition.definition_id})`,
        }));
        setStrategyDefinitionOptions(opts);
      })
      .catch(() => {
        if (active) {
          setStrategyDefinitionOptions([]);
        }
      })
      .finally(() => {
        if (active) {
          setStrategyDefinitionsLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    setDataProvidersLoading(true);
    void listDataProviders()
      .then((res) => {
        if (!active) {
          return;
        }
        const ids = res.providers?.filter((x) => typeof x === "string" && x.trim());
        setDataProviderIds(ids?.length ? ids : [...FALLBACK_DATA_PROVIDER_IDS]);
      })
      .catch(() => {
        if (active) {
          setDataProviderIds([...FALLBACK_DATA_PROVIDER_IDS]);
        }
      })
      .finally(() => {
        if (active) {
          setDataProvidersLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    const isEdit = mode === "edit" && editTask != null;
    const editDefaults = isEdit
      ? {
          name: editTask.name,
          mode: editTask.mode,
          description: editTask.description ?? "",
          data_provider: editTask.data_provider?.trim() || undefined,
          universe_symbols: [...(editTask.universe || [])],
          max_single_order_amount: maxSingleOrderAmountFromSettings(
            editTask.settings as Record<string, unknown> | null,
          ),
          review_equity_fraction: reviewEquityFractionFromSettings(
            editTask.settings as Record<string, unknown> | null,
          ),
          max_position_ratio: maxPositionRatioFromSettings(
            editTask.settings as Record<string, unknown> | null,
          ),
          max_task_position_amount: maxTaskPositionAmountFromSettings(
            editTask.settings as Record<string, unknown> | null,
          ),
          max_task_position_ratio: maxTaskPositionRatioFromSettings(
            editTask.settings as Record<string, unknown> | null,
          ),
          lot_size: lotSizeFromSettings(editTask.settings as Record<string, unknown> | null),
          rebalance_hysteresis_lots: rebalanceHysteresisFromSettings(
            editTask.settings as Record<string, unknown> | null,
          ),
          min_notional_for_approval: minNotionalForApprovalFromSettings(
            editTask.settings as Record<string, unknown> | null,
          ),
          approval_timeout_seconds: approvalTimeoutSecondsFromSettings(
            editTask.settings as Record<string, unknown> | null,
          ),
          strategy_definition_id:
            ((editTask.settings as Record<string, unknown>)?.strategy as Record<string, unknown> | undefined)?.definition_id ??
            "",
          strategy_parameter_overrides: JSON.stringify(
            ((editTask.settings as Record<string, unknown>)?.strategy as Record<string, unknown> | undefined)
              ?.parameter_overrides ?? {},
            null,
            2,
          ),
          strategy_execution_profile:
            String(
              (((editTask.settings as Record<string, unknown>)?.strategy as Record<string, unknown> | undefined)
                ?.execution_profile as string | undefined) ?? "default",
            ),
          account_id:
            typeof (editTask.settings as Record<string, unknown>)?.account_id === "string"
              ? ((editTask.settings as Record<string, unknown>).account_id as string)
              : undefined,
          // Prefill the optional data_cache fields from the stored settings;
          // each stays undefined when the task omits it, so an untouched edit
          // re-submits exactly what was there (and nothing if it was absent).
          ...dataCacheFormValuesFromSettings(editTask.settings as Record<string, unknown> | null),
        }
      : null;

    if (isEdit) {
      setSettingsObject(initialSettingsFromTask(editTask));
      if (editDefaults) {
        form.setFieldsValue(editDefaults);
      }
      return;
    }

      setSettingsObject(defaultSettingsObject());
      form.setFieldsValue({
        mode: "paper",
        strategy_parameter_overrides: "{}",
        strategy_execution_profile: "default",
        ...DEFAULT_SIGNAL_FORM,
        ...createInitialValues,
    });
  }, [createInitialValues, form, mode, editTask]);

  const openSettingsJsonModal = useCallback(() => {
    const prevAgent =
      settingsObject.agent && typeof settingsObject.agent === "object"
        ? { ...(settingsObject.agent as Record<string, unknown>) }
        : {};
    const prevPc =
      prevAgent.position_constraints && typeof prevAgent.position_constraints === "object"
        ? { ...(prevAgent.position_constraints as Record<string, unknown>) }
        : {};
    const prevApproval =
      prevAgent.approval && typeof prevAgent.approval === "object"
        ? { ...(prevAgent.approval as Record<string, unknown>) }
        : {};
    const merged: Record<string, unknown> = {
      ...settingsObject,
      strategy: {
        definition_id: form.getFieldValue("strategy_definition_id"),
        parameter_overrides:
          typeof form.getFieldValue("strategy_parameter_overrides") === "string"
            ? JSON.parse(form.getFieldValue("strategy_parameter_overrides") || "{}")
            : (form.getFieldValue("strategy_parameter_overrides") ?? {}),
        execution_profile: form.getFieldValue("strategy_execution_profile") ?? "default",
      },
      agent: {
        ...prevAgent,
            position_constraints: {
          ...prevPc,
          max_single_order_amount:
            form.getFieldValue("max_single_order_amount") === undefined ||
            form.getFieldValue("max_single_order_amount") === ""
              ? null
              : form.getFieldValue("max_single_order_amount"),
          review_equity_fraction: form.getFieldValue("review_equity_fraction"),
          max_position_ratio: form.getFieldValue("max_position_ratio") ?? 0.3,
          max_task_position_amount:
            form.getFieldValue("max_task_position_amount") === undefined ||
            form.getFieldValue("max_task_position_amount") === "" ||
            form.getFieldValue("max_task_position_amount") === null
              ? null
              : form.getFieldValue("max_task_position_amount"),
          max_task_position_ratio:
            form.getFieldValue("max_task_position_ratio") === undefined ||
            form.getFieldValue("max_task_position_ratio") === "" ||
            form.getFieldValue("max_task_position_ratio") === null
              ? null
              : form.getFieldValue("max_task_position_ratio"),
          lot_size: form.getFieldValue("lot_size") ?? 1,
          rebalance_hysteresis_lots: form.getFieldValue("rebalance_hysteresis_lots") ?? 0,
        },
        approval: {
          ...prevApproval,
          min_notional_for_approval: form.getFieldValue("min_notional_for_approval") ?? 1000,
          timeout_seconds: form.getFieldValue("approval_timeout_seconds") ?? 300,
        },
      },
    };
    if (merged.agent && typeof merged.agent === "object" && !Array.isArray(merged.agent)) {
      delete (merged.agent as Record<string, unknown>).react_max_turns;
      delete (merged.agent as Record<string, unknown>).signal_tool_names;
      delete (merged.agent as Record<string, unknown>).enabled_skills;
    }
    // Reflect the optional data_cache form fields into the JSON view, or strip
    // the block when the user left every field unset.
    const mergedDataCache = buildDataCacheSettings({
      data_cache_source_priority: form.getFieldValue("data_cache_source_priority"),
      data_cache_local_first: form.getFieldValue("data_cache_local_first"),
      data_cache_auto_backfill: form.getFieldValue("data_cache_auto_backfill"),
      data_cache_on_unverifiable_gap: form.getFieldValue("data_cache_on_unverifiable_gap"),
    });
    if (mergedDataCache) {
      merged.data_cache = mergedDataCache;
    } else {
      delete merged.data_cache;
    }
    setSettingsModalInitialText(JSON.stringify(merged, null, 2));
    setSettingsModalOpen(true);
  }, [form, settingsObject]);

  const handleSettingsModalApply = useCallback(
    (parsed: Record<string, unknown>) => {
      setSettingsObject(parsed);
      form.setFieldsValue({
        max_single_order_amount: maxSingleOrderAmountFromSettings(parsed),
        review_equity_fraction: reviewEquityFractionFromSettings(parsed),
        max_position_ratio: maxPositionRatioFromSettings(parsed),
        max_task_position_amount: maxTaskPositionAmountFromSettings(parsed),
        max_task_position_ratio: maxTaskPositionRatioFromSettings(parsed),
        lot_size: lotSizeFromSettings(parsed),
        rebalance_hysteresis_lots: rebalanceHysteresisFromSettings(parsed),
        min_notional_for_approval: minNotionalForApprovalFromSettings(parsed),
        approval_timeout_seconds: approvalTimeoutSecondsFromSettings(parsed),
        strategy_definition_id:
          typeof (parsed.strategy as Record<string, unknown> | undefined)?.definition_id === "string"
            ? ((parsed.strategy as Record<string, unknown>).definition_id as string)
            : form.getFieldValue("strategy_definition_id"),
        strategy_parameter_overrides: JSON.stringify(
          ((parsed.strategy as Record<string, unknown> | undefined)?.parameter_overrides as Record<string, unknown> | undefined) ?? {},
          null,
          2,
        ),
        strategy_execution_profile:
          typeof (parsed.strategy as Record<string, unknown> | undefined)?.execution_profile === "string"
            ? ((parsed.strategy as Record<string, unknown>).execution_profile as string)
            : "default",
        // Keep the data_cache form fields in sync with a hand-edited settings
        // JSON. Fields absent from the JSON reset to undefined ("unset").
        data_cache_source_priority: undefined,
        data_cache_local_first: undefined,
        data_cache_auto_backfill: undefined,
        data_cache_on_unverifiable_gap: undefined,
        ...dataCacheFormValuesFromSettings(parsed),
      });
      setSettingsModalOpen(false);
    },
    [form],
  );

  useImperativeHandle(
    ref,
    () => ({ openSettingsJsonModal, applyCreatePatch }),
    [applyCreatePatch, openSettingsJsonModal],
  );

  const dataProviderSelectOptions = useMemo(() => {
    const seen = new Set<string>();
    const ordered: string[] = [];
    const push = (id: string) => {
      const k = id.trim();
      if (!k || seen.has(k)) {
        return;
      }
      seen.add(k);
      ordered.push(k);
    };
    for (const id of dataProviderIds) {
      push(id);
    }
    const current =
      mode === "edit" && editTask?.data_provider?.trim() ? editTask.data_provider.trim() : "";
    if (current) {
      push(current);
    }
    return ordered.map((id) => ({
      value: id,
      label: DATA_PROVIDER_LABELS[id] ? `${DATA_PROVIDER_LABELS[id]}（${id}）` : id,
    }));
  }, [dataProviderIds, mode, editTask?.data_provider]);

  const modeSelectOptions = useMemo(() => {
    const allowedValues =
      mode === "edit"
        ? (editTask?.mode === "backtest" ? ["backtest"] : ["paper", "live"])
        : (allowedModes ?? RUN_MODE_OPTIONS.map((option) => option.value));
    return RUN_MODE_OPTIONS.filter((option) => allowedValues.includes(option.value));
  }, [allowedModes, editTask?.mode, mode]);

  const modeSelectDisabled =
    (mode === "create" && allowedModes != null && allowedModes.length <= 1) ||
    (mode === "edit" && modeSelectOptions.length <= 1);

  return (
    <Card
      className={PANEL_CARD_CLASSNAME}
      {...(hideCardTitle ? {} : { title: "创建实例" })}
    >
      <Form<CreateAgentFormValues>
        layout="vertical"
        form={form}
        initialValues={DEFAULT_SIGNAL_FORM}
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
        onFinish={async (values) => {
          const rawValues = form.getFieldsValue(true);

          const agentSettings = {
            position_constraints: {
              max_single_order_amount:
                rawValues.max_single_order_amount === undefined ||
                rawValues.max_single_order_amount === "" ||
                rawValues.max_single_order_amount === null
                  ? null
                  : rawValues.max_single_order_amount,
              max_position_ratio: rawValues.max_position_ratio ?? 0.3,
              review_equity_fraction: rawValues.review_equity_fraction ?? 1,
              max_task_position_amount:
                rawValues.max_task_position_amount === undefined ||
                rawValues.max_task_position_amount === "" ||
                rawValues.max_task_position_amount === null
                  ? null
                  : rawValues.max_task_position_amount,
              max_task_position_ratio:
                rawValues.max_task_position_ratio === undefined ||
                rawValues.max_task_position_ratio === "" ||
                rawValues.max_task_position_ratio === null
                  ? null
                  : rawValues.max_task_position_ratio,
              lot_size: rawValues.lot_size ?? 1,
              rebalance_hysteresis_lots: rawValues.rebalance_hysteresis_lots ?? 0,
            },
            approval: {
              min_notional_for_approval: rawValues.min_notional_for_approval ?? 1000,
              timeout_seconds: rawValues.approval_timeout_seconds ?? 300,
            },
          };

          const baseSettings = structuredClone(settingsObject);
          const baseAgent =
            baseSettings.agent && typeof baseSettings.agent === "object" && !Array.isArray(baseSettings.agent)
              ? (baseSettings.agent as Record<string, unknown>)
              : {};
          const baseAgentPositionConstraints =
            baseAgent.position_constraints &&
            typeof baseAgent.position_constraints === "object" &&
            !Array.isArray(baseAgent.position_constraints)
              ? (baseAgent.position_constraints as Record<string, unknown>)
              : {};
          const baseAgentApproval =
            baseAgent.approval && typeof baseAgent.approval === "object" && !Array.isArray(baseAgent.approval)
              ? (baseAgent.approval as Record<string, unknown>)
              : {};
          delete baseAgent.react_max_turns;
          delete baseAgent.signal_tool_names;
          delete baseAgent.enabled_skills;

          const accountIdRaw = String(rawValues.account_id ?? "").trim();
          // Optional cache / backfill / continuity policy. Built only from the
          // fields the user explicitly set; when nothing is set we drop the
          // block entirely (below) so the backend keeps its "omitted = defaults"
          // behavior instead of being handed an empty object that overrides it.
          const dataCacheBlock = buildDataCacheSettings(rawValues);
          const settingsPayload = {
            ...baseSettings,
            // Bound trading account; null clears any prior binding so the
            // task falls back to the global account_mode.
            account_id: accountIdRaw ? accountIdRaw : null,
            // Tradable universe selected via CatalogSymbolSelect; backend reads
            // it from settings.universe (see POST/PUT /tasks). Without this the
            // user's selection is silently dropped on save.
            universe: Array.isArray(rawValues.universe_symbols) ? rawValues.universe_symbols : [],
            strategy: {
              definition_id: String(rawValues.strategy_definition_id ?? "").trim(),
              parameter_overrides:
                typeof rawValues.strategy_parameter_overrides === "string"
                  ? JSON.parse(rawValues.strategy_parameter_overrides || "{}")
                  : (rawValues.strategy_parameter_overrides ?? {}),
              execution_profile: rawValues.strategy_execution_profile ?? "default",
            },
            agent: {
              ...baseAgent,
              ...agentSettings,
              position_constraints: {
                ...baseAgentPositionConstraints,
                ...agentSettings.position_constraints,
              },
              approval: {
                ...baseAgentApproval,
                ...agentSettings.approval,
              },
            },
          } as Record<string, unknown>;

          // Patch semantics for the optional data_cache block: write it only
          // when the user set at least one field; otherwise strip any inherited
          // block so an untouched / cleared form submits no data_cache at all.
          if (dataCacheBlock) {
            settingsPayload.data_cache = dataCacheBlock;
          } else {
            delete settingsPayload.data_cache;
          }

          const payload: CreateTaskPayload = {
            name: values.name.trim(),
            mode: values.mode,
            description: normalizeOptionalText(values.description),
            data_provider: normalizeOptionalText(values.data_provider),
            settings: settingsPayload as CreateTaskPayload["settings"],
          };

          setLoading(true);
          try {
            if (mode === "edit" && editTask) {
              await updateTask(editTask.task_id, payload as Parameters<typeof updateTask>[1]);
            } else {
              const created = await createTask(payload as CreateTaskPayload);
              if (values.mode === "backtest") {
                const range = values.backtest_range;
                if (!range?.[0] || !range?.[1]) {
                  throw new Error("回测任务需要选择回测区间");
                }
                await startTaskRun(created.task_id, {
                  range_start: range[0].format("YYYY-MM-DD"),
                  range_end: range[1].format("YYYY-MM-DD"),
                  market_profile: values.backtest_market_profile?.trim() || "cn_a_share",
                  bar_interval: values.backtest_bar_interval?.trim() || "1d",
                  debug_enabled: values.backtest_debug_enabled !== false,
                });
              }
            }
            form.resetFields([
              "name",
              "description",
              "data_provider",
              "account_id",
              "universe_symbols",
              "max_single_order_amount",
              "review_equity_fraction",
              "max_position_ratio",
              "max_task_position_amount",
              "max_task_position_ratio",
              "min_notional_for_approval",
              "approval_timeout_seconds",
              "lot_size",
              "rebalance_hysteresis_lots",
              "strategy_definition_id",
              "strategy_parameter_overrides",
              "strategy_execution_profile",
              "backtest_range",
              "backtest_market_profile",
              "backtest_bar_interval",
              "backtest_debug_enabled",
            ]);
            setSettingsObject(defaultSettingsObject());
            onCreated();
          } catch (error: unknown) {
            const content = error instanceof Error ? error.message : String(error);
            message.error(`${mode === "edit" ? "保存" : "创建"}任务失败：${content}`);
          } finally {
            setLoading(false);
          }
        }}
      >
        <Row gutter={[16, 8]}>
          {/* Row 1: 名称 + 运行模式 */}
          <Col span={24}>
            <Row gutter={16}>
              <Col span={8}>
                <Form.Item
                  name="name"
                  label="名称"
                  rules={[{ required: true, message: "请输入实例名称" }]}
                >
                  <Input placeholder="alpha-growth-paper" />
                </Form.Item>
              </Col>
              <Col span={8}>
                <Form.Item name="mode" label="运行模式">
                  <Select
                    disabled={modeSelectDisabled}
                    options={modeSelectOptions}
                  />
                </Form.Item>
              </Col>
              <Col span={8} />
            </Row>
          </Col>

          {/* Backtest conditional fields */}
          <Col span={24}>
            <Form.Item noStyle shouldUpdate={(prev, next) => prev.mode !== next.mode}>
              {({ getFieldValue }) =>
                mode === "create" && getFieldValue("mode") === "backtest" ? (
                  <Row gutter={16}>
                    <Col span={12}>
                      <Form.Item name="backtest_range" label="回测区间" rules={[{ required: true, message: "请选择回测区间" }]}>
                        <DatePicker.RangePicker className="w-full" />
                      </Form.Item>
                    </Col>
                    <Col span={6}>
                      <Form.Item name="backtest_market_profile" label="市场配置">
                        <Input placeholder="cn_a_share" />
                      </Form.Item>
                    </Col>
                    <Col span={6}>
                      <Form.Item name="backtest_bar_interval" label="K线周期">
                        <Input placeholder="1d" />
                      </Form.Item>
                    </Col>
                    <Col span={24}>
                      <Form.Item
                        name="backtest_debug_enabled"
                        valuePropName="checked"
                        label={
                          <Tooltip title="开启时记录完整调试链路（调试会话 / span / 每根 bar 的 cycle / 模型调用），可在调试页回看。关闭可显著加快回测，但不会留存这些 trace 明细（仍保留运行状态、报告与成交）。">
                            <span>调试模式</span>
                          </Tooltip>
                        }
                      >
                        <Switch checkedChildren="开" unCheckedChildren="关" />
                      </Form.Item>
                    </Col>
                  </Row>
                ) : null
              }
            </Form.Item>
          </Col>

          {/* Row 2: 策略定义 + 交易 universe（核心配置） */}
          <Col span={24}>
            <Row gutter={16}>
              <Col span={12}>
                <Form.Item
                  name="strategy_definition_id"
                  label="策略定义"
                  rules={[{ required: true, message: "请选择策略定义" }]}
                >
                  <Select
                    showSearch
                    allowClear
                    loading={strategyDefinitionsLoading}
                    optionFilterProp="label"
                    placeholder={strategyDefinitionsLoading ? "加载策略定义…" : "选择 definition"}
                    options={strategyDefinitionOptions}
                    notFoundContent={strategyDefinitionsLoading ? "加载中…" : "暂无策略定义"}
                  />
                </Form.Item>
              </Col>
            </Row>
            <Form.Item
              name="universe_symbols"
              label="交易 universe"
              extra="留空则本轮无可交易标的。"
              normalize={(v) => (Array.isArray(v) ? v : [])}
            >
              <CatalogSymbolSelect />
            </Form.Item>
          </Col>

          {/* 高级设置：以下面板内全部字段均有默认值或可留空，默认折叠；
              校验失败时 onFinishFailed 会自动展开对应面板。 */}
          <Col span={24}>
            <Collapse
              ghost
              activeKey={openPanels}
              onChange={(keys) => setOpenPanels(Array.isArray(keys) ? keys.map(String) : [String(keys)])}
              items={[
                {
                  key: "runtime",
                  label: "运行与数据（可选，留空走全局默认）",
                  forceRender: true,
                  children: (
                    <>
                      <Row gutter={16}>
                        <Col span={8}>
                          <Form.Item
                            name="data_provider"
                            label="数据提供器"
                            normalize={(v) => (typeof v === "string" && v.trim() ? v.trim() : undefined)}
                          >
                            <Select
                              allowClear
                              showSearch
                              loading={dataProvidersLoading}
                              optionFilterProp="label"
                              placeholder="留空 = 全局默认"
                              options={dataProviderSelectOptions}
                            />
                          </Form.Item>
                        </Col>
                        <Col span={8}>
                          <Form.Item
                            name="account_id"
                            label="账户"
                            extra="留空 = 沿用全局账户配置"
                            normalize={(v) => (typeof v === "string" && v.trim() ? v.trim() : undefined)}
                          >
                            <Select
                              allowClear
                              showSearch
                              loading={accountsLoading}
                              optionFilterProp="label"
                              placeholder="留空 = 全局默认"
                              options={accountOptions}
                              notFoundContent={accountsLoading ? "加载中…" : "暂无账户"}
                            />
                          </Form.Item>
                        </Col>
                      </Row>
                      <Form.Item name="description" label="描述">
                        <Input.TextArea rows={2} placeholder="填写策略目标、风控偏好等信息" />
                      </Form.Item>
                    </>
                  ),
                },
                {
                  key: "strategy_advanced",
                  label: "策略进阶（可选：执行配置 / 参数覆盖）",
                  forceRender: true,
                  children: (
                    <>
                      <Row gutter={16}>
                        <Col span={8}>
                          <Form.Item name="strategy_execution_profile" label="执行配置">
                            <Input placeholder="default" />
                          </Form.Item>
                        </Col>
                      </Row>
                      <Form.Item
                        name="strategy_parameter_overrides"
                        label="策略参数覆盖"
                        extra="JSON 对象，直接写入 settings.strategy.parameter_overrides。"
                        rules={[
                          {
                            validator: async (_, value) => {
                              if (value == null || value === "") {
                                return;
                              }
                              if (typeof value === "string") {
                                const parsed = JSON.parse(value);
                                if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
                                  return;
                                }
                              }
                              throw new Error("请填写合法的 JSON 对象");
                            },
                          },
                        ]}
                      >
                        <Input.TextArea rows={4} placeholder='{"lookback": 20}' />
                      </Form.Item>
                    </>
                  ),
                },
                {
                  key: "risk",
                  label: "风控与审批（已按默认值填好，通常无需修改）",
                  forceRender: true,
                  children: (
                    <>
                      <Row gutter={16}>
              <Col span={8}>
                <Form.Item
                  name="max_single_order_amount"
                  label="单笔下单上限"
                  extra="留空 = 无上限"
                  rules={[
                    {
                      validator: async (_, value) => {
                        if (value === null || value === undefined || value === "") return;
                        if (typeof value !== "number" || !Number.isFinite(value) || value < 1) throw new Error("须留空或填写 ≥ 1 的正数");
                      },
                    },
                  ]}
                >
                  <InputNumber min={1} step={1000} allowClear className="w-full" placeholder="留空 = 无上限" />
                </Form.Item>
              </Col>
              <Col span={8}>
                <Form.Item
                  name="max_position_ratio"
                  label="单标的仓位占比上限"
                  extra="单票持仓市值 / 账户权益上限。默认 0.3。"
                  rules={[
                    { required: true, message: "请填写单标的仓位占比上限" },
                    {
                      validator: async (_, value) => {
                        if (typeof value !== "number" || !Number.isFinite(value) || value <= 0 || value > 1) {
                          throw new Error("须填写 0～1 之间的正数");
                        }
                      },
                    },
                  ]}
                >
                  <InputNumber min={0.01} max={1} step={0.05} className="w-full" />
                </Form.Item>
              </Col>
              <Col span={8}>
                <Form.Item
                  name="review_equity_fraction"
                  label="复核权益比例"
                  rules={[{ required: true, message: "请填写复核权益比例" }, { type: "number", min: 0.01, max: 1, message: "须在 0.01～1 之间" }]}
                  extra="默认 1（全额权益口径）。"
                >
                  <InputNumber min={0.01} max={1} step={0.05} className="w-full" />
                </Form.Item>
              </Col>
            </Row>
            <Row gutter={16}>
              <Col span={8}>
                <Form.Item
                  name="max_task_position_amount"
                  label="任务总仓位金额上限"
                  extra="留空 = 不限制这个任务的总持仓金额"
                  rules={[
                    {
                      validator: async (_, value) => {
                        if (value === null || value === undefined || value === "") return;
                        if (typeof value !== "number" || !Number.isFinite(value) || value < 1) throw new Error("须留空或填写 ≥ 1 的正数");
                      },
                    },
                  ]}
                >
                  <InputNumber min={1} step={1000} allowClear className="w-full" placeholder="例如 50000" />
                </Form.Item>
              </Col>
              <Col span={8}>
                <Form.Item
                  name="max_task_position_ratio"
                  label="任务总仓位占比上限"
                  extra="留空 = 不限制这个任务的总持仓占权益比例"
                  rules={[
                    {
                      validator: async (_, value) => {
                        if (value === null || value === undefined || value === "") return;
                        if (typeof value !== "number" || !Number.isFinite(value) || value <= 0 || value > 1) throw new Error("须留空或填写 0～1 之间的正数");
                      },
                    },
                  ]}
                >
                  <InputNumber min={0.01} max={1} step={0.05} allowClear className="w-full" placeholder="例如 0.5" />
                </Form.Item>
              </Col>
              <Col span={8}>
                <Form.Item
                  name="min_notional_for_approval"
                  label="人工审批金额门槛"
                  extra="单笔计划名义金额 ≥ 此值时转人工审批。0 = 全部审批。"
                  rules={[
                    { required: true, message: "请填写人工审批金额门槛" },
                    {
                      validator: async (_, value) => {
                        if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
                          throw new Error("须填写 ≥ 0 的数字");
                        }
                      },
                    },
                  ]}
                >
                  <InputNumber min={0} step={1000} className="w-full" />
                </Form.Item>
              </Col>
            </Row>
            <Row gutter={16}>
              <Col span={8}>
                <Form.Item
                  name="lot_size"
                  label="整手股数 (lot)"
                  extra="1=按股；A股网格设 100。仅作用于 target_quantity/target_exposure：买入/部分卖出向下取整到整手，清仓豁免。"
                  rules={[
                    {
                      validator: async (_, value) => {
                        if (value === null || value === undefined || value === "") return;
                        if (!Number.isInteger(value) || value < 1) throw new Error("须为 ≥ 1 的整数");
                      },
                    },
                  ]}
                >
                  <InputNumber min={1} step={100} precision={0} className="w-full" />
                </Form.Item>
              </Col>
              <Col span={8}>
                <Form.Item
                  name="rebalance_hysteresis_lots"
                  label="再平衡防抖 (手)"
                  extra="差额不足 N 手不动，避免档位边界 churn；清仓豁免。0=关闭。"
                  rules={[
                    {
                      validator: async (_, value) => {
                        if (value === null || value === undefined || value === "") return;
                        if (!Number.isInteger(value) || value < 0) throw new Error("须为 ≥ 0 的整数");
                      },
                    },
                  ]}
                >
                  <InputNumber min={0} step={1} precision={0} className="w-full" />
                </Form.Item>
              </Col>
              <Col span={8}>
                <Form.Item
                  name="approval_timeout_seconds"
                  label="审批超时（秒）"
                  extra="超过该时间未审批则自动过期，不下单。默认 300 秒。"
                  rules={[
                    { required: true, message: "请填写审批超时" },
                    {
                      validator: async (_, value) => {
                        if (!Number.isInteger(value) || value < 1) {
                          throw new Error("须填写 ≥ 1 的整数秒数");
                        }
                      },
                    },
                  ]}
                >
                  <InputNumber min={1} step={30} precision={0} className="w-full" />
                </Form.Item>
              </Col>
            </Row>
                    </>
                  ),
                },
                {
                  key: "data_cache",
                  label: "数据缓存 / 数据源（可选，留空走后端默认）",
                  forceRender: true,
                  children: (
                    <>
                      <Row gutter={16}>
                        <Col span={24}>
                          <Form.Item
                            name="data_cache_source_priority"
                            label={
                              <Tooltip title="本地缺失时回填的数据源优先级；选择顺序即为尝试顺序。留空走后端默认（qmt, baostock, akshare, tushare）。">
                                <span>回填数据源优先级</span>
                              </Tooltip>
                            }
                            extra="按选择顺序作为回填优先级；留空 = 后端默认 qmt, baostock, akshare, tushare。"
                          >
                            <Select
                              mode="multiple"
                              allowClear
                              placeholder="留空 = 后端默认顺序"
                              options={DATA_CACHE_SOURCE_OPTIONS}
                              optionFilterProp="label"
                            />
                          </Form.Item>
                        </Col>
                      </Row>
                      <Row gutter={16}>
                        <Col span={8}>
                          <Form.Item
                            name="data_cache_local_first"
                            valuePropName="checked"
                            label={
                              <Tooltip title="开启时先读本地 DB，再在缺失时打上游。默认 true。留空（不切换）= 走后端默认。">
                                <span>优先读本地（local_first）</span>
                              </Tooltip>
                            }
                          >
                            <Switch checkedChildren="开" unCheckedChildren="关" />
                          </Form.Item>
                        </Col>
                        <Col span={8}>
                          <Form.Item
                            name="data_cache_auto_backfill"
                            valuePropName="checked"
                            label={
                              <Tooltip title="本地缺失时自动从上游拉取并落库。默认 true。留空 = 走后端默认。">
                                <span>自动回填（auto_backfill）</span>
                              </Tooltip>
                            }
                          >
                            <Switch checkedChildren="开" unCheckedChildren="关" />
                          </Form.Item>
                        </Col>
                        <Col span={8}>
                          <Form.Item
                            name="data_cache_on_unverifiable_gap"
                            label={
                              <Tooltip title="无法证明缺口是停牌时的处理。fail=拒绝写入（更安全）；degrade=降级放行。默认 fail。留空 = 走后端默认。">
                                <span>不可验证缺口（on_unverifiable_gap）</span>
                              </Tooltip>
                            }
                          >
                            <Select
                              allowClear
                              placeholder="留空 = 后端默认 fail"
                              options={DATA_CACHE_ON_UNVERIFIABLE_GAP_OPTIONS}
                            />
                          </Form.Item>
                        </Col>
                      </Row>
                    </>
                  ),
                },
              ]}
            />
          </Col>

          {/* 提交按钮 + Settings JSON */}
          <Col span={24}>
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <Typography.Text className="text-sm" type="secondary">
                业务字段会在提交前转换为结构化 payload，系统字段仍由后端自动生成。
              </Typography.Text>
              <div className="flex items-center gap-3">
                {settingsJsonButtonPlacement === "inline" && (
                  <Button type="default" onClick={openSettingsJsonModal}>编辑 Settings JSON</Button>
                )}
                <Button className="rounded-xl" type="primary" htmlType="submit" loading={loading}>
                  {mode === "edit" ? "保存" : "创建"}
                </Button>
              </div>
            </div>
          </Col>
        </Row>
      </Form>

      <SettingsJsonEditorModal
        open={settingsModalOpen}
        initialText={settingsModalInitialText}
        onCancel={() => setSettingsModalOpen(false)}
        onApply={handleSettingsModalApply}
      />
    </Card>
  );
});

CreateAgentCard.displayName = "CreateAgentCard";
