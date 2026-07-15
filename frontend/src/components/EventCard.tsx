import { CopyOutlined } from "@ant-design/icons";
import { Button, Typography } from "antd";
import { useCallback, useMemo } from "react";

import { JsonCodeBlock } from "./JsonCodeBlock";

export const EVENT_TYPE_COLORS: Record<string, string> = {
  error: "#ff4d4f",
  cycle_aborted: "#ff4d4f",
  signal_user_context: "#722ed1",
  signal_turn: "#1677ff",
  signal_tool: "#13c2c2",
  signal_output: "#52c41a",
  trading_decisions: "#52c41a",
  review_user_context: "#9254de",
  review_turn: "#2f54eb",
  review_tool: "#36cfc9",
  order_intents: "#fa8c16",
  agent_reviews: "#eb2f96",
  risk_decisions: "#faad14",
  approval_result: "#7ab8f5",
  execution: "#a0a0a0",
  summary: "#8c8c8c",
};

export function getEventTypeColor(eventType: string): string {
  return EVENT_TYPE_COLORS[eventType] ?? "#d9d9d9";
}

export type DebugEvent = {
  event_type: string;
  payload: Record<string, unknown>;
};

type Props = {
  event: DebugEvent;
  isSelected: boolean;
  onClick: () => void;
};

export function EventCard({ event, isSelected, onClick }: Props) {
  const color = useMemo(() => getEventTypeColor(event.event_type), [event.event_type]);

  const isError = event.event_type === "error" || event.event_type === "cycle_aborted";

  const handleCopy = useCallback(async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(JSON.stringify(event.payload, null, 2));
    } catch {
      // silent
    }
  }, [event.payload]);

  const borderColor = isError ? "#ff4d4f" : isSelected ? "#1677ff" : color;
  const bgColor = isError ? "#fff2f0" : isSelected ? "#e6f4ff" : "#fff";

  return (
    <div
      onClick={onClick}
      style={{
        margin: "4px 0 4px 20px",
        borderRadius: 8,
        border: `1px solid ${borderColor}`,
        borderLeft: `4px solid ${borderColor}`,
        background: bgColor,
        cursor: "pointer",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          padding: "6px 10px 4px",
          gap: 8,
        }}
      >
        <Typography.Text
          style={{
            fontSize: 11,
            fontWeight: 600,
            background: isError ? "#ff4d4f" : color,
            color: "#fff",
            padding: "1px 6px",
            borderRadius: 4,
          }}
        >
          {event.event_type}
        </Typography.Text>
        <Button
          type="text"
          size="small"
          icon={<CopyOutlined />}
          onClick={handleCopy}
          style={{ marginLeft: "auto", fontSize: 11 }}
        />
      </div>
      <div style={{ maxHeight: 72, overflow: "auto" }}>
        <JsonCodeBlock value={event.payload} maxHeight={72} />
      </div>
      <div style={{ padding: "4px 10px 6px", textAlign: "right" }}>
        <Typography.Text style={{ fontSize: 11, color: "#1677ff" }}>
          {isSelected ? "查看详情 ✓" : "查看详情 →"}
        </Typography.Text>
      </div>
    </div>
  );
}
