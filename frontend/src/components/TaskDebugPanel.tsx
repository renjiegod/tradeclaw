import { Button, Alert, DatePicker, Form, Input, Space, Typography, message } from "antd";
import type { Dayjs } from "dayjs";
import { useState } from "react";

import { createDebugSession } from "../api";
import { CatalogSymbolSelect } from "./CatalogSymbolSelect";
import type { TaskStatus } from "../types";
import {
  CYCLE_PICKER_FORMAT,
  cycleTimePickerToApiIso,
} from "../utils/datetime";

type PanelProps = {
  task: TaskStatus;
  onDebugSessionCreated?: () => void;
};

type FormValues = {
  universe?: string[];
  debugNote?: string;
  marketPricesJson?: string;
  ticksJson?: string;
  barsRequestsJson?: string;
  /** Simulated logical clock in Asia/Shanghai (UTC+8); serialized to UTC `Z` in input_overrides.cycle_time. */
  cycleTimeUtc8?: Dayjs | null;
};

function parseJsonField(raw: string | undefined, fieldName: string): unknown {
  const text = raw?.trim();
  if (!text) return undefined;
  try {
    return JSON.parse(text);
  } catch (error: unknown) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(`${fieldName} JSON 解析失败：${detail}`);
  }
}

function buildPayload(values: FormValues): {
  input_overrides?: Record<string, unknown>;
} {
  const input: Record<string, unknown> = {};

  if (values.universe?.length) {
    const cleaned = values.universe.map((s) => String(s).trim()).filter(Boolean);
    if (cleaned.length) {
      input.universe = cleaned;
    }
  }
  if (values.debugNote?.trim()) input.debug_note = values.debugNote.trim();
  const cycleIso = cycleTimePickerToApiIso(values.cycleTimeUtc8 ?? undefined);
  if (cycleIso) input.cycle_time = cycleIso;

  const marketPrices = parseJsonField(values.marketPricesJson, "行情价格");
  if (marketPrices !== undefined) input.market_prices = marketPrices;
  const ticks = parseJsonField(values.ticksJson, "Tick 覆盖");
  if (ticks !== undefined) input.ticks = ticks;
  const barsRequests = parseJsonField(values.barsRequestsJson, "Bars 覆盖");
  if (barsRequests !== undefined) input.bars_requests = barsRequests;

  return {
    input_overrides: Object.keys(input).length ? input : undefined,
  };
}

export function TaskDebugPanel({ task, onDebugSessionCreated }: PanelProps) {
  const [form] = Form.useForm<FormValues>();
  const [starting, setStarting] = useState(false);

  const taskRunning = task.status === "running";
  const runDisabled = taskRunning || starting;
  const runDebugTitle = taskRunning && !starting ? "任务运行中无法新建调试运行，请先暂停或停止" : undefined;

  return (
    <Space direction="vertical" size={16} className="w-full">
      <Space align="center" wrap className="w-full justify-between">
        <Typography.Title level={5} className="!mb-0">
          调试
        </Typography.Title>
        <Button
          type="primary"
          loading={starting}
          disabled={runDisabled}
          title={runDebugTitle}
          onClick={async () => {
            setStarting(true);
            try {
              await createDebugSession(task.task_id, buildPayload(form.getFieldsValue()));
              message.success("已创建调试会话");
              onDebugSessionCreated?.();
            } catch (error: unknown) {
              const detail = error instanceof Error ? error.message : String(error);
              message.error(`启动调试失败：${detail}`);
            } finally {
              setStarting(false);
            }
          }}
        >
          运行调试
        </Button>
      </Space>
      <Alert
        type="warning"
        showIcon
        message="调试运行与追踪"
        description="在「周期运行」Tab 的表格中点击某一行可打开详情：含该 run 的摘要、关联 debug session（若有）、仅该轮 trace 的事件流，以及仅该 run_id 的模型调用。实例运行中时不能新建调试运行；「运行调试」不是 dry-run。"
        className="rounded-2xl"
      />

      {taskRunning ? (
        <Alert
          type="info"
          showIcon
          message="运行中仅可查看"
          description="覆盖参数已锁定；暂停或停止任务后可编辑并启动调试运行。"
          className="rounded-2xl"
        />
      ) : null}

      <Form<FormValues> form={form} layout="vertical" disabled={taskRunning}>
        <div className="rounded-2xl border border-shell-line bg-card-bg p-4">
          <Typography.Title level={5} className="!mb-3">
            输入覆盖
          </Typography.Title>
          <Form.Item
            name="universe"
            label="universe"
            extra="从列表搜索添加，或直接输入代码回车；留空则不覆盖实例 universe。"
            normalize={(v) => (Array.isArray(v) ? v : [])}
          >
            <CatalogSymbolSelect />
          </Form.Item>
          <Form.Item name="debugNote" label="debug_note">
            <Input.TextArea rows={3} placeholder="给 signal agent 的本次调试说明" />
          </Form.Item>
          <Form.Item
            name="cycleTimeUtc8"
            label="模拟逻辑时间（UTC+8）"
            extra="写入 input_overrides.cycle_time（UTC ISO）；存在时 cycle_runs.clock_mode=simulated。以下为北京时间。"
          >
            <DatePicker
              showTime
              format={CYCLE_PICKER_FORMAT}
              className="w-full max-w-md"
              placeholder="选择日期与时间"
              allowClear
            />
          </Form.Item>
          <Form.Item
            name="marketPricesJson"
            label="market_prices（JSON）"
            extra="覆盖数据提供方行情（工具 / get_market_context）；不写入 signal 用户消息。"
          >
            <Input.TextArea rows={4} placeholder={'{\n  "600000.SH": 10.3,\n  "601318.SH": 49.8\n}'} />
          </Form.Item>
          <Form.Item
            name="ticksJson"
            label="ticks（JSON）"
            extra="同上：覆盖提供方逐笔/快照；不写入 signal 用户消息。"
          >
            <Input.TextArea rows={4} placeholder={'{\n  "600000.SH": { "last": 10.3, "bid": 10.29 }\n}'} />
          </Form.Item>
          <Form.Item name="barsRequestsJson" label="bars_requests（JSON）">
            <Input.TextArea
              rows={6}
              placeholder={
                '[\n  {\n    "symbol": "600000.SH",\n    "start_time": "2026-04-01",\n    "end_time": "2026-04-05",\n    "interval": "1d",\n    "bars": []\n  }\n]'
              }
            />
          </Form.Item>
        </div>
      </Form>
    </Space>
  );
}
