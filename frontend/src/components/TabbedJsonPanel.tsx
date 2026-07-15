import { Tabs, Typography } from "antd";
import type { ReactNode } from "react";

type Props = {
  title: string;
  originContent: ReactNode;
  formatContent: ReactNode;
};

export function TabbedJsonPanel({ title, originContent, formatContent }: Props) {
  return (
    <div className="mb-5">
      <Typography.Text strong className="mb-2 block text-shell-ink">
        {title}
      </Typography.Text>
      <Tabs
        defaultActiveKey="origin"
        className="model-invocation-tabs"
        items={[
          {
            key: "origin",
            label: "Origin",
            children: originContent,
          },
          {
            key: "format",
            label: "Format",
            children: formatContent,
          },
        ]}
      />
    </div>
  );
}
