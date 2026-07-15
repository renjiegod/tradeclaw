import React from "react";
import { Button, Modal, Segmented, Space, Tag, message } from "antd";
import CodeMirror from "@uiw/react-codemirror";
import { markdown } from "@codemirror/lang-markdown";
import { python } from "@codemirror/lang-python";
import { json } from "@codemirror/lang-json";

import MarkdownPreview from "./MarkdownPreview";
import type { SkillFile } from "../types";

type Props = {
  file: SkillFile;
  onSave: (content: string, ifUnmodifiedSince: string) => Promise<{ mtime: string }>;
  onRefresh: () => Promise<void>;
};

function languageFor(mime: string, path: string) {
  if (mime === "text/markdown" || path.endsWith(".md")) return [markdown()];
  if (mime.includes("python") || path.endsWith(".py")) return [python()];
  if (mime === "application/json" || path.endsWith(".json")) return [json()];
  return [];
}

export default function SkillFileEditor({ file, onSave, onRefresh }: Props) {
  const [mode, setMode] = React.useState<"preview" | "edit">(
    file.mime === "text/markdown" ? "preview" : "edit"
  );
  const [content, setContent] = React.useState(file.content);
  const [dirty, setDirty] = React.useState(false);
  const [saving, setSaving] = React.useState(false);

  React.useEffect(() => {
    setContent(file.content);
    setDirty(false);
    setMode(file.mime === "text/markdown" ? "preview" : "edit");
  }, [file.path, file.mtime, file.content, file.mime]);

  const handleSave = async () => {
    setSaving(true);
    try {
      await onSave(content, file.mtime);
      setDirty(false);
      message.success("已保存");
    } catch (err: any) {
      // mtime conflict handling
      if (err?.status === 409) {
        Modal.confirm({
          title: "文件已被外部修改",
          content: "覆盖远端内容？取消则放弃本地修改并重新加载。",
          okText: "覆盖",
          cancelText: "放弃本地修改",
          onOk: async () => {
            // force overwrite with no-precondition (caller passes empty mtime)
            await onSave(content, "");
            setDirty(false);
          },
          onCancel: async () => {
            await onRefresh();
          },
        });
      } else {
        message.error(String(err?.message ?? err));
      }
    } finally {
      setSaving(false);
    }
  };

  const isBinary = file.encoding === "base64";
  const isImage = file.mime.startsWith("image/");

  return (
    <div>
      <Space style={{ marginBottom: 8 }}>
        <Tag>{file.path}</Tag>
        {!isBinary && (
          <Segmented
            options={[
              { label: "预览", value: "preview" },
              { label: "编辑", value: "edit" },
            ]}
            value={mode}
            onChange={(v) => setMode(v as any)}
          />
        )}
        <Button type="primary" disabled={!dirty} loading={saving} onClick={handleSave}>
          保存
        </Button>
        {dirty && <Tag color="orange">未保存</Tag>}
      </Space>
      {isBinary ? (
        isImage ? (
          <img src={`data:${file.mime};base64,${file.content}`} alt={file.path} style={{ maxWidth: "100%" }} />
        ) : (
          <div style={{ opacity: 0.6 }}>二进制文件，无法在浏览器内编辑。</div>
        )
      ) : mode === "preview" ? (
        <MarkdownPreview source={content} stripFrontmatter={file.path === "SKILL.md"} />
      ) : (
        <CodeMirror
          value={content}
          height="60vh"
          extensions={languageFor(file.mime, file.path)}
          onChange={(v) => { setContent(v); setDirty(v !== file.content); }}
        />
      )}
    </div>
  );
}
