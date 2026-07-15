import CodeMirror from "@uiw/react-codemirror";
import { json } from "@codemirror/lang-json";
import { oneDark } from "@codemirror/theme-one-dark";
import { Button, Modal, Space, message } from "antd";
import { useEffect, useState } from "react";

function parseSettingsObject(text: string): Record<string, unknown> {
  const trimmed = text.trim();
  if (!trimmed) {
    throw new Error("内容不能为空。");
  }
  const parsed: unknown = JSON.parse(trimmed);
  if (parsed === null) {
    throw new Error("Settings 不能为 null。");
  }
  if (typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Settings 必须是 JSON 对象。");
  }
  return parsed as Record<string, unknown>;
}

type Props = {
  open: boolean;
  /** Shown when the modal opens (typically formatted JSON). */
  initialText: string;
  onCancel: () => void;
  /** Called with validated object; modal should be closed by parent. */
  onApply: (obj: Record<string, unknown>) => void;
};

export function SettingsJsonEditorModal({ open, initialText, onCancel, onApply }: Props) {
  const [draft, setDraft] = useState(initialText);

  useEffect(() => {
    if (open) {
      setDraft(initialText);
    }
  }, [open, initialText]);

  const handleFormat = () => {
    try {
      const obj = parseSettingsObject(draft);
      setDraft(JSON.stringify(obj, null, 2));
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : String(e));
    }
  };

  const handleOk = () => {
    try {
      const obj = parseSettingsObject(draft);
      onApply(obj);
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <Modal
      title="编辑 Settings（原始 JSON）"
      open={open}
      onCancel={onCancel}
      width="min(920px, 96vw)"
      destroyOnClose
      footer={
        <Space wrap>
          <Button onClick={handleFormat}>格式化</Button>
          <Button onClick={onCancel}>取消</Button>
          <Button type="primary" onClick={handleOk}>
            确定
          </Button>
        </Space>
      }
    >
      <p className="mb-2 text-xs text-neutral-500">
        JSON 语法高亮；建议仅查看或调整任务级运行参数，系统内置的 Agent 固定配置不建议在这里维护。
      </p>
      <div className="overflow-hidden rounded-md border border-shell-line">
        <CodeMirror
          value={draft}
          height="min(52vh, 480px)"
          extensions={[json(), oneDark]}
          onChange={(v) => setDraft(v)}
          basicSetup={{ lineNumbers: true, foldGutter: true }}
        />
      </div>
    </Modal>
  );
}
