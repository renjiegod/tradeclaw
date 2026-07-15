import { Collapse, Typography } from "antd";
import { SyntaxHighlighter, oneLight } from "./syntaxHighlighter";

type Props = {
  tools: Array<{
    type?: string;
    name?: string;
    function?: { name?: string; parameters?: unknown };
    id?: string;
    input?: unknown;
  }>;
};

export function ToolCallsTable({ tools }: Props) {
  if (!tools || tools.length === 0) return null;

  const items = tools.map((tool, idx) => {
    const name = tool.name ?? tool.function?.name ?? `tool_${idx}`;
    const rawArgs = tool.input ?? tool.function?.parameters ?? {};
    const argsText = typeof rawArgs === "string" ? rawArgs : JSON.stringify(rawArgs, null, 2);

    return {
      key: String(tool.id ?? idx),
      label: (
        <Typography.Text strong className="font-mono text-sm">
          {name}
        </Typography.Text>
      ),
      children: (
        <div className="pl-3">
          <Typography.Text type="secondary" className="text-xs">
            arguments:
          </Typography.Text>
          <SyntaxHighlighter
            language="json"
            style={oneLight}
            showLineNumbers={false}
            wrapLongLines
            customStyle={{
              margin: "4px 0 0 0",
              fontSize: 11,
              borderRadius: 8,
              padding: "8px 10px",
              background: "#faf8f5",
            }}
            codeTagProps={{ style: { whiteSpace: "pre-wrap", wordBreak: "break-word" } }}
          >
            {argsText}
          </SyntaxHighlighter>
        </div>
      ),
    };
  });

  return (
    <div className="mb-3">
      <Typography.Text strong className="mb-1 block text-sm text-shell-ink">
        工具调用
      </Typography.Text>
      <Collapse items={items} defaultActiveKey={[]} ghost className="tool-calls-collapse" />
    </div>
  );
}
