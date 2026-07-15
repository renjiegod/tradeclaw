import { CopyOutlined } from "@ant-design/icons";
import { Button, message, Tooltip } from "antd";
import { SyntaxHighlighter, oneLight } from "./syntaxHighlighter";

type Props = {
  code: string;
  /** Defaults to "python". */
  language?: string;
  /** Show line numbers on the left. Off by default for long code. */
  showLineNumbers?: boolean;
  /** CSS length, e.g. 360 or "min(70vh, 720px)". */
  maxHeight?: number | string;
  /** Show copy button. */
  copyable?: boolean;
  /** Title bar shown above the code block. */
  title?: string;
};

export function CodeBlock({
  code,
  language = "python",
  showLineNumbers = false,
  maxHeight = "min(70vh, 720px)",
  copyable = false,
  title,
}: Props) {
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      void message.success("已复制到剪贴板");
    } catch {
      void message.error("复制失败");
    }
  };

  return (
    <div className="relative">
      {title && (
        <div className="mb-1 px-1 text-xs font-medium text-shell-muted">{title}</div>
      )}
      {copyable ? (
        <Tooltip title="复制代码">
          <Button
            type="text"
            size="small"
            className="absolute right-2 top-2 z-10 !h-7 !w-7 !min-w-0 rounded-lg border border-shell-line bg-[rgba(255,253,249,0.92)] p-0 text-shell-muted shadow-sm hover:text-shell-ink"
            icon={<CopyOutlined />}
            onClick={() => void handleCopy()}
            aria-label="复制代码"
          />
        </Tooltip>
      ) : null}
      <SyntaxHighlighter
        language={language}
        style={oneLight}
        showLineNumbers={showLineNumbers}
        wrapLongLines
        customStyle={{
          margin: 0,
          fontSize: 12.5,
          lineHeight: 1.6,
          borderRadius: 10,
          maxHeight,
          overflow: "auto",
          padding: "12px 14px",
          background: "#faf8f5",
        }}
        codeTagProps={{
          style: {
            display: "block",
          },
        }}
      >
        {code}
      </SyntaxHighlighter>
    </div>
  );
}
