// frontend/src/components/assistant/panels/AssistantChartBlock.tsx
//
// Agent 面板的通用图表块：内联数据 + recharts（折线 / 柱状 / 面积 / 饼图）。
// 适合净值曲线、分类聚合、占比等小数据集；K 线走 AssistantKlineBlock。

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { ChartBlock } from "./panelSpec";

// dataviz 安全的分类色板（light surface），系列数超板长时按 index 取模循环。
const SERIES_PALETTE = [
  "#3b6fd4",
  "#c0504d",
  "#2f8f6b",
  "#b8508f",
  "#b26a1f",
  "#7b5fc0",
  "#1f8f8f",
  "#6b7f2e",
];

function seriesColor(index: number): string {
  return SERIES_PALETTE[((index % SERIES_PALETTE.length) + SERIES_PALETTE.length) % SERIES_PALETTE.length];
}

function seriesName(block: ChartBlock, field: string): string {
  return block.series_names[field] ?? field;
}

export function AssistantChartBlock({ block }: { block: ChartBlock }) {
  const { chart_type, data } = block;

  const chart = () => {
    if (chart_type === "pie") {
      const nameKey = block.category_field!;
      const dataKey = block.value_field!;
      return (
        <PieChart>
          <Tooltip />
          <Legend />
          <Pie data={data} nameKey={nameKey} dataKey={dataKey} outerRadius="80%" label>
            {data.map((_, index) => (
              <Cell key={index} fill={seriesColor(index)} />
            ))}
          </Pie>
        </PieChart>
      );
    }

    const axes = (
      <>
        <CartesianGrid strokeDasharray="3 3" stroke="#eee" />
        <XAxis dataKey={block.x_field} tick={{ fontSize: 11 }} />
        <YAxis tick={{ fontSize: 11 }} unit={block.unit} width={48} />
        <Tooltip />
        <Legend />
      </>
    );

    if (chart_type === "bar") {
      return (
        <BarChart data={data}>
          {axes}
          {block.y_fields.map((field, index) => (
            <Bar
              key={field}
              dataKey={field}
              name={seriesName(block, field)}
              fill={seriesColor(index)}
              stackId={block.stacked ? "stack" : undefined}
            />
          ))}
        </BarChart>
      );
    }

    if (chart_type === "area") {
      return (
        <AreaChart data={data}>
          {axes}
          {block.y_fields.map((field, index) => (
            <Area
              key={field}
              type="monotone"
              dataKey={field}
              name={seriesName(block, field)}
              stroke={seriesColor(index)}
              fill={seriesColor(index)}
              fillOpacity={0.25}
              stackId={block.stacked ? "stack" : undefined}
            />
          ))}
        </AreaChart>
      );
    }

    // line（默认）
    return (
      <LineChart data={data}>
        {axes}
        {block.y_fields.map((field, index) => (
          <Line
            key={field}
            type="monotone"
            dataKey={field}
            name={seriesName(block, field)}
            stroke={seriesColor(index)}
            dot={false}
          />
        ))}
      </LineChart>
    );
  };

  return (
    <div className="w-full overflow-x-auto">
      <div style={{ width: "100%", height: block.height }} data-testid="assistant-chart-block">
        <ResponsiveContainer width="100%" height="100%">
          {chart()}
        </ResponsiveContainer>
      </div>
    </div>
  );
}
