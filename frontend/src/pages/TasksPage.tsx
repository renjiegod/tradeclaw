import { PlusOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { Button, Badge, Input, message, Modal, Select, Space, Tabs } from "antd";
import { useEffect, useRef, useState } from "react";

import {
  deleteTask,
  deleteTasks,
  getTaskDuplicatePreset,
  listStrategyDefinitions,
  listTaskRuns,
  listTaskTriggers,
  listTasksPage,
} from "../api";
import { CreateAgentCard } from "../components/CreateAgentCard";
import type { CreateAgentCardHandle, CreateAgentFormValues } from "../components/CreateAgentCard";
import { BacktestTaskTable } from "../components/BacktestTaskTable";
import { TradingTaskTable } from "../components/TradingTaskTable";
import { summarizeTriggers, TRADING_MODES } from "../components/taskTableShared";
import type { TriggerSummary } from "../components/taskTableShared";
import { PageIntro } from "../components/PageIntro";
import { usePageRefreshToken } from "../pageRefreshContext";
import type { StrategyDefinitionRow, TaskDuplicatePreset, TaskStatus } from "../types";

type Props = {
  onMutated: () => void;
};

type TaskTab = "trading" | "backtest";

const DEFAULT_PAGE_SIZE = 20;

export function TasksPage({ onMutated }: Props) {
  const pageRefreshToken = usePageRefreshToken();
  const [tab, setTab] = useState<TaskTab>("trading");
  const [tasks, setTasks] = useState<TaskStatus[]>([]);
  const [loading, setLoading] = useState(false);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [search, setSearch] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [statusFilter, setStatusFilter] = useState<string | undefined>(undefined);
  const [modeFilter, setModeFilter] = useState<string | undefined>(undefined);
  const [definitionFilter, setDefinitionFilter] = useState<string | undefined>(undefined);
  const [strategyDefinitions, setStrategyDefinitions] = useState<StrategyDefinitionRow[]>([]);
  const [strategyDefinitionsLoading, setStrategyDefinitionsLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createInitialValues, setCreateInitialValues] = useState<Partial<CreateAgentFormValues> | null>(null);
  const [latestRunStatusByTaskId, setLatestRunStatusByTaskId] = useState<Record<string, string | undefined>>({});
  const [triggerSummaryByTaskId, setTriggerSummaryByTaskId] = useState<Record<string, TriggerSummary | undefined>>({});
  const [selectedTaskIds, setSelectedTaskIds] = useState<string[]>([]);
  const createCardRef = useRef<CreateAgentCardHandle | null>(null);

  // The backtest tab queries a single mode; the trading tab groups every
  // non-backtest mode, narrowed by the optional 实盘/模拟/信号 sub-filter.
  const activeModes = tab === "backtest" ? ["backtest"] : modeFilter ? [modeFilter] : [...TRADING_MODES];

  const refreshCurrentPage = () => {
    setLoading(true);
    void listTasksPage({
      q: search,
      status: statusFilter,
      modes: activeModes,
      definition_id: definitionFilter,
      limit: pageSize,
      offset: (page - 1) * pageSize,
    })
      .then((result) => {
        setTasks(result.items);
        setTotal(result.total);
        setSelectedTaskIds((current) =>
          current.filter((taskId) => result.items.some((task) => task.task_id === taskId && task.status !== "running")),
        );
      })
      .catch((error: unknown) => {
        setTasks([]);
        setTotal(0);
        setSelectedTaskIds([]);
        const content = error instanceof Error ? error.message : String(error);
        message.error(`加载任务列表失败：${content}`);
      })
      .finally(() => {
        setLoading(false);
      });
  };

  useEffect(() => {
    refreshCurrentPage();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, page, pageSize, search, statusFilter, modeFilter, definitionFilter, pageRefreshToken]);

  // Debounced search: query while the user types instead of waiting for Enter.
  // The main refresh effect above reacts to `search`, so we only update `search`
  // here after a short pause — one network fetch per typing pause, not per keystroke.
  useEffect(() => {
    const handle = window.setTimeout(() => {
      const next = searchInput.trim();
      setSearch((current) => (current === next ? current : next));
      setPage(1);
    }, 350);
    return () => window.clearTimeout(handle);
  }, [searchInput]);

  useEffect(() => {
    setStrategyDefinitionsLoading(true);
    void listStrategyDefinitions()
      .then(({ items }) => {
        setStrategyDefinitions(items);
      })
      .catch(() => {
        setStrategyDefinitions([]);
      })
      .finally(() => {
        setStrategyDefinitionsLoading(false);
      });
  }, [pageRefreshToken]);

  // Backtest tab: resolve each task's latest run status (a backtest's display
  // status pivots on its newest run, not the task row).
  useEffect(() => {
    if (tab !== "backtest") {
      setLatestRunStatusByTaskId({});
      return;
    }
    const backtestTaskIds = tasks.filter((task) => task.mode === "backtest").map((task) => task.task_id);
    if (backtestTaskIds.length === 0) {
      setLatestRunStatusByTaskId({});
      return;
    }
    let cancelled = false;
    void Promise.all(
      backtestTaskIds.map(async (taskId) => {
        try {
          const { items } = await listTaskRuns(taskId, { limit: 1, offset: 0 });
          return [taskId, items[0]?.status] as const;
        } catch {
          return [taskId, undefined] as const;
        }
      }),
    ).then((entries) => {
      if (cancelled) return;
      setLatestRunStatusByTaskId(Object.fromEntries(entries));
    });
    return () => {
      cancelled = true;
    };
  }, [tab, tasks]);

  // Trading tab: roll up each task's triggers for the 触发器 column. A started
  // task does nothing without a trigger, so the chip flags the empty case.
  useEffect(() => {
    if (tab !== "trading") {
      setTriggerSummaryByTaskId({});
      return;
    }
    if (tasks.length === 0) {
      setTriggerSummaryByTaskId({});
      return;
    }
    let cancelled = false;
    void Promise.all(
      tasks.map(async (task) => {
        try {
          const triggers = await listTaskTriggers(task.task_id);
          return [task.task_id, summarizeTriggers(triggers)] as const;
        } catch {
          return [task.task_id, undefined] as const;
        }
      }),
    ).then((entries) => {
      if (cancelled) return;
      setTriggerSummaryByTaskId(Object.fromEntries(entries));
    });
    return () => {
      cancelled = true;
    };
  }, [tab, tasks]);

  const handleTabChange = (nextKey: string) => {
    const nextTab = nextKey === "backtest" ? "backtest" : "trading";
    if (nextTab === tab) return;
    setTab(nextTab);
    setPage(1);
    setModeFilter(undefined);
    setSelectedTaskIds([]);
  };

  const openPlainCreate = () => {
    setCreateInitialValues(tab === "backtest" ? { mode: "backtest" } : { mode: "paper" });
    setCreateOpen(true);
  };

  const clearFilters = () => {
    setSearchInput("");
    setSearch("");
    setStatusFilter(undefined);
    setModeFilter(undefined);
    setDefinitionFilter(undefined);
    setPage(1);
  };

  const presetToCreateInitialValues = (preset: TaskDuplicatePreset): Partial<CreateAgentFormValues> => ({
    name: preset.name,
    mode: preset.mode,
    description: preset.description ?? "",
    data_provider: preset.data_provider ?? undefined,
    universe_symbols: [...(preset.universe_symbols ?? [])],
    strategy_definition_id: preset.strategy?.definition_id ?? "",
    strategy_parameter_overrides: JSON.stringify(preset.strategy?.parameter_overrides ?? {}, null, 2),
    strategy_execution_profile: preset.strategy?.execution_profile ?? "default",
  });

  const handleDuplicateTask = async (task: TaskStatus) => {
    try {
      const preset = await getTaskDuplicatePreset(task.task_id);
      setCreateInitialValues(presetToCreateInitialValues(preset));
      setCreateOpen(true);
    } catch {
      message.error("读取复制预填失败，请稍后重试。");
      return;
    }

    if (task.mode !== "backtest") {
      return;
    }

    void listTaskRuns(task.task_id, { limit: 1, offset: 0 })
      .then(({ items }) => {
        const latest = items[0];
        if (!latest) {
          return;
        }
        const backtestPatch: Partial<CreateAgentFormValues> = {
          backtest_range: [dayjs(latest.range_start_utc), dayjs(latest.range_end_utc)],
          backtest_market_profile: latest.market_profile,
          backtest_bar_interval: latest.bar_interval,
        };
        if (createCardRef.current) {
          createCardRef.current.applyCreatePatch(backtestPatch, { onlyWhenEmpty: true });
          return;
        }
        // Modal mount can lag behind API resolution; retry once on the next tick.
        setTimeout(() => {
          createCardRef.current?.applyCreatePatch(backtestPatch, { onlyWhenEmpty: true });
        }, 0);
      })
      .catch(() => {
        message.warning("未能自动回填回测区间，请手动选择。");
      });
  };

  const handleDeleteTask = (task: TaskStatus) => {
    Modal.confirm({
      title: "删除任务",
      content: `确定删除「${task.name}」吗？该操作不可恢复，持久化记录将被移除。`,
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          await deleteTask(task.task_id);
          message.success("已删除任务");
          onMutated();
          refreshCurrentPage();
        } catch (error) {
          const content = error instanceof Error ? error.message : String(error);
          message.error(`删除失败：${content}`);
        }
      },
    });
  };

  const handleBulkDelete = () => {
    if (selectedTaskIds.length === 0) {
      return;
    }
    Modal.confirm({
      title: "批量删除任务",
      content: `确定删除已选中的 ${selectedTaskIds.length} 个任务吗？该操作不可恢复，持久化记录将被移除。`,
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          await deleteTasks(selectedTaskIds);
          setSelectedTaskIds([]);
          message.success(`已删除 ${selectedTaskIds.length} 个任务`);
          onMutated();
          refreshCurrentPage();
        } catch (error) {
          const content = error instanceof Error ? error.message : String(error);
          message.error(`批量删除失败：${content}`);
        }
      },
    });
  };

  const paginationProps = {
    current: page,
    pageSize,
    total,
    onChange: (nextPage: number, nextPageSize: number) => {
      if (nextPageSize !== pageSize) {
        setPage(1);
        setPageSize(nextPageSize);
        return;
      }
      setPage(nextPage);
    },
  };

  // Active filter count drives the reset button's badge so the user can see, at
  // a glance, how many filters are narrowing the list (and that reset will do something).
  const activeFilterCount = [
    search,
    statusFilter,
    tab === "trading" ? modeFilter : undefined,
    definitionFilter,
  ].filter(Boolean).length;

  return (
    <>
      <PageIntro
        title="Tasks"
        description="交易任务（实盘 / 模拟 / 信号）与回测分属两个标签页，列表按各自特点展示；运行中可暂停，非运行态可启动。"
        extra={
          <Button type="primary" className="rounded-xl" icon={<PlusOutlined />} onClick={openPlainCreate}>
            创建任务
          </Button>
        }
      />
      <Modal
        title={tab === "backtest" ? "创建回测任务" : "创建交易任务"}
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        footer={null}
        width={760}
        destroyOnHidden
        styles={{ body: { maxHeight: "calc(100dvh - 160px)", overflowY: "auto", paddingTop: 8 } }}
      >
        <CreateAgentCard
          ref={createCardRef}
          hideCardTitle
          createInitialValues={createInitialValues}
          allowedModes={tab === "backtest" ? ["backtest"] : ["paper", "live"]}
          onCreated={() => {
            onMutated();
            refreshCurrentPage();
            setCreateOpen(false);
          }}
        />
      </Modal>
      <Tabs
        activeKey={tab}
        onChange={handleTabChange}
        items={[
          { key: "trading", label: "交易任务" },
          { key: "backtest", label: "回测" },
        ]}
      />
      <Space wrap style={{ marginBottom: 12 }}>
        <Input.Search
          allowClear
          placeholder="搜索任务名 / Task ID"
          value={searchInput}
          onChange={(event) => setSearchInput(event.target.value)}
          onSearch={(value) => {
            setPage(1);
            setSearch(value.trim());
          }}
          style={{ width: 280 }}
        />
        <Select
          allowClear
          placeholder="状态"
          value={statusFilter}
          style={{ width: 160 }}
          onChange={(value) => {
            setPage(1);
            setStatusFilter(value);
          }}
          options={[
            { label: "已配置", value: "configured" },
            { label: "运行中", value: "running" },
            { label: "已暂停", value: "paused" },
            { label: "已停止", value: "stopped" },
            { label: "异常", value: "error" },
            { label: "已完成", value: "completed" },
          ]}
        />
        {tab === "trading" ? (
          <Select
            allowClear
            placeholder="模式"
            value={modeFilter}
            style={{ width: 140 }}
            onChange={(value) => {
              setPage(1);
              setModeFilter(value);
            }}
            options={[
              { label: "模拟盘", value: "paper" },
              { label: "实盘", value: "live" },
              { label: "信号", value: "signal_only" },
            ]}
          />
        ) : null}
        <Select
          allowClear
          showSearch
          optionFilterProp="label"
          placeholder="策略"
          value={definitionFilter}
          style={{ width: 220 }}
          loading={strategyDefinitionsLoading}
          notFoundContent={strategyDefinitionsLoading ? "加载中…" : "暂无策略"}
          onChange={(value) => {
            setPage(1);
            setDefinitionFilter(value);
          }}
          options={strategyDefinitions.map((definition) => ({
            label: definition.name ? `${definition.name} (${definition.definition_id})` : definition.definition_id,
            value: definition.definition_id,
          }))}
        />
        <Badge count={activeFilterCount} offset={[-4, 4]} size="small">
          <Button onClick={clearFilters}>重置筛选</Button>
        </Badge>
      </Space>
      {tab === "backtest" ? (
        <BacktestTaskTable
          tasks={tasks}
          loading={loading}
          onDuplicate={handleDuplicateTask}
          onDelete={handleDeleteTask}
          onBulkDelete={handleBulkDelete}
          latestRunStatusByTaskId={latestRunStatusByTaskId}
          selectedTaskIds={selectedTaskIds}
          onSelectedTaskIdsChange={setSelectedTaskIds}
          pagination={paginationProps}
        />
      ) : (
        <TradingTaskTable
          tasks={tasks}
          loading={loading}
          onDuplicate={handleDuplicateTask}
          onDelete={handleDeleteTask}
          onBulkDelete={handleBulkDelete}
          triggerSummaryByTaskId={triggerSummaryByTaskId}
          selectedTaskIds={selectedTaskIds}
          onSelectedTaskIdsChange={setSelectedTaskIds}
          pagination={paginationProps}
        />
      )}
    </>
  );
}
