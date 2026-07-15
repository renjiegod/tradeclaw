import { CopyOutlined } from "@ant-design/icons";
import { Button, message, Tooltip } from "antd";
import { SyntaxHighlighter, oneLight } from "./syntaxHighlighter";

type Props = {
  value: unknown;
  /** CSS length, e.g. 360 or "min(70vh, 720px)" */
  maxHeight?: number | string;
  /** When true, show a compact copy control for the serialized JSON (model invocation raw payloads, etc.). */
  copyable?: boolean;
};

/**
 * Pretty-printed JSON with soft wrap. Line numbers are omitted because
 * react-syntax-highlighter misaligns them when lines wrap.
 */
export function JsonCodeBlock({ value, maxHeight = "min(70vh, 720px)", copyable = false }: Props) {
  const text = value == null ? "(empty)" : JSON.stringify(value, null, 2);
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      void message.success("已复制到剪贴板");
    } catch {
      void message.error("复制失败");
    }
  };
  return (
    <div className="relative">
      {copyable ? (
        <Tooltip title="复制原始 JSON">
          <Button
            type="text"
            size="small"
            className="absolute right-2 top-2 z-10 !h-7 !w-7 !min-w-0 rounded-lg border border-shell-line bg-[rgba(255,253,249,0.92)] p-0 text-shell-muted shadow-sm hover:text-shell-ink"
            icon={<CopyOutlined />}
            onClick={() => void handleCopy()}
            aria-label="复制原始 JSON"
          />
        </Tooltip>
      ) : null}
      <SyntaxHighlighter
        language="json"
        style={oneLight}
        showLineNumbers={false}
        wrapLongLines
        customStyle={{
          margin: 0,
          fontSize: 12,
          lineHeight: 1.55,
          borderRadius: 12,
          maxHeight,
          overflow: "auto",
          padding: "12px 14px",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          overflowWrap: "anywhere",
        }}
        codeTagProps={{
          style: {
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            overflowWrap: "anywhere",
            display: "block",
          },
        }}
      >
        {text}
      </SyntaxHighlighter>
    </div>
  );
}
