import { ReloadOutlined } from "@ant-design/icons";
import { Button, Select } from "antd";

export type MainIndicator = "MA" | "BOLL" | "none";
export type SubIndicator = "MACD" | "KDJ" | "RSI" | "WR";

type LocalMarketKlineToolbarProps = {
  interval: string;
  provider: string;
  mainIndicator: MainIndicator;
  subIndicator: SubIndicator;
  loading: boolean;
  onIntervalChange: (value: string) => void;
  onProviderChange: (value: string) => void;
  onMainIndicatorChange: (value: MainIndicator) => void;
  onSubIndicatorChange: (value: SubIndicator) => void;
  onRefresh: () => void;
};

const INTERVAL_OPTIONS = [
  { value: "1d", label: "日线" },
  { value: "5m", label: "5 分钟" },
  { value: "60m", label: "60 分钟" },
];

const PROVIDER_OPTIONS = [{ value: "auto", label: "自动源" }];

const MAIN_OPTIONS: { value: MainIndicator; label: string }[] = [
  { value: "MA", label: "MA" },
  { value: "BOLL", label: "BOLL" },
  { value: "none", label: "隐藏" },
];

const SUB_OPTIONS: { value: SubIndicator; label: string }[] = [
  { value: "MACD", label: "MACD" },
  { value: "KDJ", label: "KDJ" },
  { value: "RSI", label: "RSI" },
  { value: "WR", label: "WR" },
];

export function LocalMarketKlineToolbar({
  interval,
  provider,
  mainIndicator,
  subIndicator,
  loading,
  onIntervalChange,
  onProviderChange,
  onMainIndicatorChange,
  onSubIndicatorChange,
  onRefresh,
}: LocalMarketKlineToolbarProps) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Select value={interval} options={INTERVAL_OPTIONS} onChange={onIntervalChange} style={{ width: 110 }} />
      <Select value={provider} options={PROVIDER_OPTIONS} onChange={onProviderChange} style={{ width: 100 }} />
      <span className="rounded border border-slate-200 bg-slate-50 px-2 py-1 text-xs text-slate-600">前复权</span>
      <span className="rounded border border-slate-200 bg-slate-50 px-2 py-1 text-xs text-slate-600">向左滚动自动加载历史</span>
      <Select
        value={mainIndicator}
        options={MAIN_OPTIONS}
        onChange={(value) => onMainIndicatorChange(value as MainIndicator)}
        style={{ width: 88 }}
      />
      <Select
        value={subIndicator}
        options={SUB_OPTIONS}
        onChange={(value) => onSubIndicatorChange(value as SubIndicator)}
        style={{ width: 96 }}
      />
      <Button icon={<ReloadOutlined />} onClick={onRefresh} loading={loading}>
        刷新
      </Button>
    </div>
  );
}
