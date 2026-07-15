import {
  DatabaseOutlined,
  ExperimentOutlined,
  FundProjectionScreenOutlined,
  LineChartOutlined,
  ReadOutlined,
  RobotOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import type { ReactNode } from "react";

/**
 * Empty-state welcome screen for the assistant chat surface.
 *
 * Mirrors the onboarding pattern used by modern agent products: a centered
 * hero, a row of capability chips that advertise what the agent can reach, and
 * a grid of clickable example prompts grouped by intent. Clicking an example
 * hands the prompt back to the page via {@link onPickExample} so the existing
 * input / submit pipeline stays the single source of truth — the welcome screen
 * never owns conversation state itself.
 */

interface ExampleCard {
  title: string;
  description: string;
  prompt: string;
}

interface ExampleGroup {
  key: string;
  label: string;
  icon: ReactNode;
  /** Accent color for the group header + card hover border. */
  accentClassName: string;
  hoverBorderClassName: string;
  cards: ExampleCard[];
}

const CAPABILITY_CHIPS: string[] = [
  "盘口盯盘",
  "贴板股筛选",
  "板块 / 题材成分",
  "收盘复盘存档",
  "策略 SDK 编写",
  "多市场回测",
  "分钟到日线",
  "因子 IC/IR 分析",
  "K线形态识别",
  "风险指标",
  "Cron 定时调度",
  "Trace / Debug 链路",
];

const EXAMPLE_GROUPS: ExampleGroup[] = [
  {
    key: "shortterm",
    label: "短线看盘 / 复盘",
    icon: <ThunderboltOutlined />,
    accentClassName: "text-emerald-500",
    hoverBorderClassName: "hover:border-emerald-300",
    cards: [
      {
        title: "扫贴板股",
        description: "扫今天贴近涨停 / 近似封板的强势票",
        prompt:
          "帮我拉一下半导体板块成分，扫出今天贴近涨停（近似封板）、且近 10 日放量的票，按涨幅排前 20。",
      },
      {
        title: "盘口盯盘",
        description: "涨停 / 封单缩量 / 炸板打开命中即推送",
        prompt:
          "帮我给『半导体』标签的自选股建一个盯盘：涨停打开（炸板）或涨停封单缩量时提醒我。",
      },
      {
        title: "收盘复盘存档",
        description: "每天收盘自动复盘并写进知识库",
        prompt:
          "帮我建一个每个交易日收盘后（15:30）自动复盘当天交易、并把复盘写进知识库的定时任务。",
      },
    ],
  },
  {
    key: "backtest",
    label: "策略与回测",
    icon: <LineChartOutlined />,
    accentClassName: "text-rose-500",
    hoverBorderClassName: "hover:border-rose-300",
    cards: [
      {
        title: "双均线交叉策略",
        description: "为 000001.SZ 写一个双均线交叉策略，回测 2024 全年",
        prompt: "为 000001.SZ 写一个双均线交叉（快线/慢线）策略定义，绑定到一个回测任务，回测 2024-01-01 到 2024-12-31。",
      },
      {
        title: "MACD 日线策略",
        description: "创建 MACD 策略并跑一次样本外 walk-forward",
        prompt: "创建一个 MACD 日线策略，先在 600519.SH 上回测 2023-2024，再做 walk-forward 样本外验证。",
      },
      {
        title: "迭代现有策略",
        description: "回测完了，下一步该改参数还是改逻辑？",
        prompt: "我刚跑完一个回测，结果不理想，帮我分析报告并给出下一步迭代建议（参数 vs 逻辑）。",
      },
    ],
  },
  {
    key: "analysis",
    label: "数据与分析",
    icon: <FundProjectionScreenOutlined />,
    accentClassName: "text-amber-500",
    hoverBorderClassName: "hover:border-amber-300",
    cards: [
      {
        title: "K线形态识别",
        description: "拉日线并识别形态、支撑阻力、趋势斜率",
        prompt: "拉取 600519.SH 最近一年日线数据，识别 K 线形态、支撑/阻力位与趋势斜率。",
      },
      {
        title: "因子 IC/IR 分析",
        description: "对一篮子标的做 RSI 因子有效性分析",
        prompt: "对沪深300成分股做 RSI 因子的 IC/IR 与分层分析，给出因子有效性结论。",
      },
    ],
  },
  {
    key: "orchestration",
    label: "任务与调度",
    icon: <ExperimentOutlined />,
    accentClassName: "text-indigo-500",
    hoverBorderClassName: "hover:border-indigo-300",
    cards: [
      {
        title: "查看运行中的任务",
        description: "列出全部交易任务，哪些在运行 / 暂停",
        prompt: "列出当前所有交易任务，标明哪些在运行、哪些暂停，并简单说明各自的 universe 与策略。",
      },
      {
        title: "定时盘前提醒",
        description: "每个交易日 9:15 跑一次盘前数据扫描",
        prompt: "创建一个 Cron 任务：每个交易日 09:15 帮我扫描自选股的隔夜新闻和涨跌停接近度。",
      },
    ],
  },
  {
    key: "knowledge",
    label: "复盘与研究",
    icon: <ReadOutlined />,
    accentClassName: "text-sky-500",
    hoverBorderClassName: "hover:border-sky-300",
    cards: [
      {
        title: "交易复盘总结",
        description: "复盘最近一周交易并写一份结构化总结",
        prompt: "帮我复盘最近一周的交易记录，分析盈亏归因，并写一份结构化复盘总结。",
      },
      {
        title: "标的研究笔记",
        description: "查询某只票的历史角色与主题脉络",
        prompt: "帮我查询 300750.SZ 在知识库里的历史角色、所属主题与情绪周期笔记。",
      },
    ],
  },
];

export function AssistantWelcome({
  onPickExample,
  agentName,
}: {
  onPickExample: (prompt: string) => void;
  agentName?: string | null;
}) {
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col items-center gap-8 px-2 py-6">
      {/* Hero */}
      <div className="flex flex-col items-center gap-3 text-center">
        <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-gradient-to-br from-shell-accent via-orange-400 to-chat-accent text-white shadow-shell-card">
          <RobotOutlined className="text-3xl" />
        </div>
        <h1 className="m-0 font-display text-3xl font-semibold text-shell-ink">
          {agentName?.trim() || "DoYouTrade Agent"}
        </h1>
        <p className="m-0 max-w-md text-sm text-shell-muted">
          和你的 A 股交易 Agent 团队一起，用对话完成选股、盯盘、策略回测与收盘复盘。
        </p>
        <p className="m-0 text-sm font-medium text-chat-ink">描述一个交易想法即可开始。</p>
      </div>

      {/* Capability chips */}
      <div className="flex flex-wrap items-center justify-center gap-2">
        {CAPABILITY_CHIPS.map((chip) => (
          <span
            key={chip}
            className="rounded-full border border-shell-line bg-white/70 px-3 py-1 text-xs text-shell-muted"
          >
            {chip}
          </span>
        ))}
      </div>

      {/* Examples */}
      <div className="w-full">
        <div className="mb-3 flex items-center gap-2 text-shell-muted">
          <DatabaseOutlined className="text-sm" />
          <span className="text-sm font-medium">试试这些示例：</span>
        </div>
        <div className="grid grid-cols-1 gap-x-5 gap-y-6 md:grid-cols-2">
          {EXAMPLE_GROUPS.map((group) => (
            <div key={group.key} className="flex flex-col gap-2">
              <div className={`flex items-center gap-2 text-sm font-medium ${group.accentClassName}`}>
                {group.icon}
                <span>{group.label}</span>
              </div>
              <div className="flex flex-col gap-2">
                {group.cards.map((card) => (
                  <button
                    key={card.title}
                    type="button"
                    onClick={() => onPickExample(card.prompt)}
                    className={`group flex w-full flex-col items-start gap-1 rounded-2xl border border-shell-line bg-white/80 px-4 py-3 text-left transition hover:bg-white hover:shadow-shell-card ${group.hoverBorderClassName}`}
                  >
                    <span className="text-sm font-medium text-shell-ink">{card.title}</span>
                    <span className="text-xs text-shell-muted">{card.description}</span>
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
