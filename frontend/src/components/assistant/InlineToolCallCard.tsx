// frontend/src/components/assistant/InlineToolCallCard.tsx

import { Button, Collapse, Tag, Typography } from "antd";
import type { CollapseProps } from "antd";
import { DownOutlined, ExportOutlined, UpOutlined } from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { SyntaxHighlighter, oneLight } from "../syntaxHighlighter";
import React from "react";
import { useNavigate } from "react-router-dom";

import { MODEL_INVOCATION_PROSE_CLASSNAME } from "../../styles/classNames";
import {
  BACKTEST_NAV_TOOLS,
  extractTaskIdFromToolResult,
} from "./backtestNavigation";
import { toolStatusFromResult, type ToolUseBlock, type ToolResultBlock } from "./types";

const CATEGORY_THEME: Record<
  string,
  { color: string; bg: string; label: string }
> = {
  macro:      { color: "#3B82F6", bg: "#EFF6FF", label: "宏观数据" },
  kline:      { color: "#F97316", bg: "#FFF7ED", label: "K线查询" },
  stock_list: { color: "#22C55E", bg: "#F0FDF4", label: "股票列表" },
  financial:  { color: "#3B82F6", bg: "#EFF6FF", label: "财务数据" },
  summary:    { color: "#6B7280", bg: "#F9FAFB", label: "总结" },
  default:    { color: "#8B5CF6", bg: "#F5F3FF", label: "工具" },
};

const STATUS_CONFIG: Record<
  string,
  { color: string; text: string; dotClass: string }
> = {
  pending:   { color: "default", text: "等待中", dotClass: "bg-gray-400" },
  running:   { color: "processing", text: "调用中", dotClass: "bg-blue-500 animate-pulse" },
  completed: { color: "success", text: "已完成", dotClass: "bg-green-500" },
  error:     { color: "error", text: "失败", dotClass: "bg-red-500" },
};

interface InlineToolCallCardProps {
  tool: ToolUseBlock;
  result?: ToolResultBlock;
  defaultExpanded?: boolean;
}

function JsonView({ value }: { value: unknown }) {
  const text = value == null ? "(empty)" : JSON.stringify(value, null, 2);
  return (
    <SyntaxHighlighter
      language="json"
      style={oneLight}
      showLineNumbers={false}
      wrapLongLines
      customStyle={{
        margin: 0,
        fontSize: 11,
        borderRadius: 8,
        padding: "8px 10px",
        background: "#f8f8f8",
      }}
      codeTagProps={{
        style: {
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
        },
      }}
    >
      {text}
    </SyntaxHighlighter>
  );
}

// Renders a tool's output. Strings are treated as Markdown so reports that
// embed ```json fences, tables, bullet lists or headings render readably
// instead of as one long escaped string. Structured values (objects/arrays)
// keep the JSON syntax-highlighted view.
function ResultView({ value }: { value: unknown }) {
  if (value == null) {
    return <JsonView value={value} />;
  }
  if (typeof value === "string") {
    return (
      <div
        className={`${MODEL_INVOCATION_PROSE_CLASSNAME} max-w-none text-xs leading-6`}
        data-testid="tool-result-markdown"
      >
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            code(props: React.HTMLAttributes<HTMLElement> & { inline?: boolean }) {
              const { inline, className, children, ...rest } = props;
              const match = /language-(\w+)/.exec(className || "");
              if (inline || !match) {
                return (
                  <code className={className} {...rest}>
                    {children}
                  </code>
                );
              }
              return (
                <SyntaxHighlighter
                  language={match[1]}
                  style={oneLight}
                  PreTag="div"
                  customStyle={{
                    margin: 0,
                    fontSize: 11,
                    borderRadius: 8,
                    padding: "8px 10px",
                    background: "#f8f8f8",
                  }}
                  codeTagProps={{
                    style: {
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-word",
                    },
                  }}
                >
                  {String(children).replace(/\n$/, "")}
                </SyntaxHighlighter>
              );
            },
          }}
        >
          {value}
        </ReactMarkdown>
      </div>
    );
  }
  return <JsonView value={value} />;
}

export function InlineToolCallCard({
  tool,
  result,
  defaultExpanded = false,
}: InlineToolCallCardProps) {
  const [inputOpen, setInputOpen] = React.useState(defaultExpanded);
  const [outputOpen, setOutputOpen] = React.useState(defaultExpanded);
  const navigate = useNavigate();

  const theme =
    CATEGORY_THEME[tool.category] ?? CATEGORY_THEME["default"];
  const effectiveStatus = toolStatusFromResult(tool, result);
  const statusCfg = STATUS_CONFIG[effectiveStatus] ?? STATUS_CONFIG["pending"];

  // For backtest-producing tools, expose a jump-to-task-detail affordance
  // once the call has come back. Probing the output is best-effort: a
  // half-streamed or failed call yields ``null`` and the button stays
  // hidden so the card layout doesn't churn.
  const backtestTaskId = React.useMemo(() => {
    if (!BACKTEST_NAV_TOOLS.has(tool.name)) return null;
    if (!result || result.is_error) return null;
    if (effectiveStatus !== "completed") return null;
    // First try the tool input (task_id is often passed in for task-mode
    // backtests; this works even if the output JSON was truncated by the
    // tool_result_max_chars budget).
    if (tool.input && typeof tool.input === "object") {
      const fromInput = (tool.input as Record<string, unknown>)["task_id"];
      if (typeof fromInput === "string" && fromInput) return fromInput;
    }
    return extractTaskIdFromToolResult(result.output);
  }, [tool.name, tool.input, result, effectiveStatus]);

  const collapseItems: CollapseProps["items"] = [
    {
      key: "input",
      label: (
        <div className="flex items-center gap-2">
          {inputOpen ? <UpOutlined /> : <DownOutlined />}
          <span className="text-xs font-medium text-gray-500">输入参数</span>
        </div>
      ),
      children: <JsonView value={tool.input} />,
    },
  ];

  if (result != null) {
    collapseItems.push({
      key: "output",
      label: (
        <div className="flex items-center gap-2">
          {outputOpen ? <UpOutlined /> : <DownOutlined />}
          <span className="text-xs font-medium text-gray-500">输出结果</span>
          {result.is_error && (
            <Tag color="error" className="text-xs">错误</Tag>
          )}
        </div>
      ),
      children: <ResultView value={result.output} />,
    });
  }

  return (
    <div
      className="mb-3 rounded-xl border border-shell-line bg-white shadow-sm"
      style={{ borderLeft: `3px solid ${theme.color}` }}
    >
      {/* Card Header */}
      <div
        className="flex cursor-pointer items-center justify-between px-4 py-3"
        onClick={() => {
          setInputOpen((v) => !v);
          if (result != null) setOutputOpen((v) => !v);
        }}
      >
        <div className="flex items-center gap-2">
          <span
            className={`inline-block h-2 w-2 rounded-full ${statusCfg.dotClass}`}
          />
          <span
            className="rounded px-1.5 py-0.5 text-xs font-medium"
            style={{ color: theme.color, background: theme.bg }}
          >
            {theme.label}
          </span>
          <Typography.Text strong className="font-mono text-sm">
            {tool.name}
          </Typography.Text>
        </div>
        <div className="flex items-center gap-2">
          <Tag color={statusCfg.color} className="text-xs">
            {statusCfg.text}
          </Tag>
          <span className="text-gray-400">
            {inputOpen && (!result || outputOpen) ? (
              <UpOutlined />
            ) : (
              <DownOutlined />
            )}
          </span>
        </div>
      </div>

      {/* Collapsible Content */}
      {(inputOpen || outputOpen) && (
        <div className="border-t border-shell-line px-4 py-3">
          <Collapse
            activeKey={
              [
                inputOpen ? "input" : null,
                outputOpen ? "output" : null,
              ].filter(Boolean) as string[]
            }
            onChange={(keys) => {
              setInputOpen(keys.includes("input"));
              setOutputOpen(keys.includes("output"));
            }}
            items={collapseItems}
            ghost
            className="tool-call-collapse"
          />
        </div>
      )}

      {backtestTaskId ? (
        <div
          className="flex items-center justify-end border-t border-shell-line px-4 py-2"
          data-testid="inline-tool-call-backtest-jump"
        >
          <Button
            type="link"
            size="small"
            icon={<ExportOutlined />}
            onClick={(event) => {
              event.stopPropagation();
              navigate(`/tasks/${encodeURIComponent(backtestTaskId)}`);
            }}
          >
            查看回测任务详情
          </Button>
        </div>
      ) : null}
    </div>
  );
}
