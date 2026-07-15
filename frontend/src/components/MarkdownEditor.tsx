import React, { useState } from "react";
import CodeMirror from "@uiw/react-codemirror";
import { markdown as markdownLang } from "@codemirror/lang-markdown";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Button, Space } from "antd";

type Props = {
  value: string;
  onChange: (value: string) => void;
  minHeight?: number;
};

export const MarkdownEditor: React.FC<Props> = ({
  value,
  onChange,
  minHeight = 120,
}) => {
  const [editMode, setEditMode] = useState(true);

  return (
    <div>
      <Space style={{ marginBottom: 8 }}>
        <Button
          type={editMode ? "primary" : "text"}
          size="small"
          onClick={() => setEditMode(true)}
        >
          Edit
        </Button>
        <Button
          type={!editMode ? "primary" : "text"}
          size="small"
          onClick={() => setEditMode(false)}
        >
          Preview
        </Button>
      </Space>
      {editMode ? (
        <CodeMirror
          value={value}
          height={`${minHeight}px`}
          extensions={[markdownLang()]}
          onChange={(v) => onChange(v)}
          basicSetup={{ lineNumbers: false, foldGutter: false }}
          className="overflow-hidden rounded-md border border-shell-line"
        />
      ) : value ? (
        <div
          className="rounded-md border border-shell-line bg-card-bg px-3 py-2"
          style={{ minHeight: `${minHeight}px` }}
        >
          <Markdown remarkPlugins={[remarkGfm]}>{value}</Markdown>
        </div>
      ) : (
        <div
          className="rounded-md border border-shell-line px-3 py-2 italic text-shell-muted"
          style={{ minHeight: `${minHeight}px` }}
        >
          Nothing to preview
        </div>
      )}
    </div>
  );
};
