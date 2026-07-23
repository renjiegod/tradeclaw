import { Space, Typography } from "antd";
import type { ReactNode } from "react";

type Props = {
  title: string;
  description?: string;
  extra?: ReactNode;
};

export function PageIntro({ title, description, extra }: Props) {
  return (
    <div className="mb-5 flex flex-col gap-3 border-b border-shell-line pb-4 md:flex-row md:items-end md:justify-between">
      <Space direction="vertical" size={2}>
        <Typography.Title level={3} className="!m-0 !font-display !text-shell-ink">
          {title}
        </Typography.Title>
        {description ? (
          <Typography.Text className="text-sm text-shell-muted">{description}</Typography.Text>
        ) : null}
      </Space>
      {extra ? <div className="shrink-0">{extra}</div> : null}
    </div>
  );
}
