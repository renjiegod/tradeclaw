import { FileOutlined } from "@ant-design/icons";
import { Tree, Typography } from "antd";
import { useMemo, useState } from "react";

import { CodeBlock } from "./CodeBlock";
import type { StrategyDefinitionFile } from "../types";

type Props = {
  files: StrategyDefinitionFile[];
};

function defaultSelection(files: StrategyDefinitionFile[]): string | null {
  const main = files.find((f) => f.path === "strategy.py");
  if (main) return main.path;
  return files[0]?.path ?? null;
}

export function StrategyFileTree({ files }: Props) {
  const [selectedPath, setSelectedPath] = useState<string | null>(() =>
    defaultSelection(files),
  );

  const treeData = useMemo(
    () =>
      files.map((f) => ({
        key: f.path,
        title: f.path,
        icon: <FileOutlined />,
        isLeaf: true,
      })),
    [files],
  );

  const selected = useMemo(
    () => files.find((f) => f.path === selectedPath) ?? null,
    [files, selectedPath],
  );

  if (files.length === 0) {
    return (
      <Typography.Text type="secondary">暂无版本文件（策略尚未发布）。</Typography.Text>
    );
  }

  return (
    <div style={{ display: "flex", gap: 12, alignItems: "flex-start" }}>
      <div style={{ minWidth: 160, flexShrink: 0 }}>
        <Tree
          showIcon
          defaultExpandAll
          treeData={treeData}
          selectedKeys={selectedPath ? [selectedPath] : []}
          onSelect={(keys) => {
            const key = keys[0];
            if (typeof key === "string") setSelectedPath(key);
          }}
        />
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        {selected === null ? (
          <Typography.Text type="secondary">请在左侧选择文件</Typography.Text>
        ) : selected.content === null ? (
          <Typography.Text type="secondary">
            文件过大，无法内联展示（{selected.size_bytes?.toLocaleString() ?? "?"}
            {" "}字节）
          </Typography.Text>
        ) : (
          <CodeBlock
            code={selected.content}
            language="python"
            copyable
            maxHeight="min(68vh, 880px)"
          />
        )}
      </div>
    </div>
  );
}
