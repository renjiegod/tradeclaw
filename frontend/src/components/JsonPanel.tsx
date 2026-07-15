import { Typography } from "antd";

import { JsonCodeBlock } from "./JsonCodeBlock";

type Props = {
  title: string;
  data: unknown;
  /** Passed through to JsonCodeBlock; defaults match model invocation detail modal. */
  maxHeight?: number | string;
  /** Show one-click copy for serialized JSON (on by default for model invocation panels). */
  showCopy?: boolean;
};

export function JsonPanel({ title, data, maxHeight = "min(70vh, 720px)", showCopy = true }: Props) {
  return (
    <div className="mb-5">
      <Typography.Text strong className="mb-2 block text-shell-ink">
        {title}
      </Typography.Text>
      <JsonCodeBlock value={data} maxHeight={maxHeight} copyable={showCopy} />
    </div>
  );
}
