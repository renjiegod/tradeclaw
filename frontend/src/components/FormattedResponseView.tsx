import { Alert, Collapse, Space, Tag } from "antd";
import type { Components } from "react-markdown";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { MODEL_INVOCATION_PROSE_CLASSNAME } from "../styles/classNames";
import { ToolCallsTable } from "./ToolCallsTable";
import { JsonCodeBlock } from "./JsonCodeBlock";

const markdownComponents: Partial<Components> = {
  a: ({ href, children, ...rest }) => {
    const external = href != null && /^https?:\/\//i.test(href);
    return (
      <a
        href={href}
        {...rest}
        {...(external ? { target: "_blank", rel: "noopener noreferrer" } : {})}
      >
        {children}
      </a>
    );
  },
};

type AnthropicContentBlock =
  | { type: "text"; text: string }
  | { type: "tool_use"; id: string; name: string; input: unknown }
  | { type: "thinking"; thinking: string }
  | { type: string; [key: string]: unknown };

type OpenAIMessage = {
  role?: string;
  content?: unknown;
  tool_calls?: Array<{
    id?: string;
    type?: string;
    function?: { name?: string; arguments?: string | Record<string, unknown> };
  }>;
};

type Props = {
  data: unknown;
  /** Adapter/API family: "anthropic" | "openai_compatible" | "lmstudio" */
  provider_kind: string;
  maxHeight?: number | string;
};

/** True when data has an Anthropic-style `content` array with structured blocks. */
function hasAnthropicContentShape(r: Record<string, unknown>): boolean {
  const content = r["content"];
  return Array.isArray(content) && content.length > 0 && typeof (content[0] as Record<string, unknown>)["type"] === "string";
}

export function FormattedResponseView({ data, provider_kind, maxHeight }: Props) {
  if (data == null || typeof data !== "object" || Array.isArray(data)) {
    return <JsonCodeBlock value={data} maxHeight={maxHeight} />;
  }

  const r = data as Record<string, unknown>;

  // Use Anthropic rendering when the provider_kind is "anthropic",
  // or the response shape matches the Anthropic content-block pattern
  // (e.g. MiniMax, OpenAI-compatible endpoints that mimic Anthropic's format).
  const isAnthropic = provider_kind === "anthropic" || hasAnthropicContentShape(r);

  if (isAnthropic) {
    return renderAnthropicResponse(r, maxHeight);
  } else {
    return renderOpenAiResponse(r, maxHeight);
  }
}

function renderAnthropicResponse(r: Record<string, unknown>, maxHeight?: number | string) {
  const content = r["content"];
  if (!Array.isArray(content)) {
    return <JsonCodeBlock value={r} maxHeight={maxHeight} />;
  }

  const blocks = content as AnthropicContentBlock[];

  const items = blocks.map((block, idx) => {
    if (block["type"] === "text") {
      const text = block["text"];
      if (typeof text !== "string" || text.length === 0) return null;
      return (
        <div key={idx} className={`mb-3 ${MODEL_INVOCATION_PROSE_CLASSNAME}`}>
          <ReactMarkdown components={markdownComponents} remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
        </div>
      );
    }

    if (block["type"] === "tool_use") {
      const toolUse = block as { type: "tool_use"; id: string; name: string; input: unknown };
      return (
        <div key={idx} className="mb-2">
          <ToolCallsTable
            tools={[{ id: toolUse.id, name: toolUse.name, input: toolUse.input }]}
          />
        </div>
      );
    }

    if (block["type"] === "thinking") {
      const thinking = block["thinking"];
      if (typeof thinking !== "string") return null;
      return (
        <div key={idx} className="mb-2">
          <Collapse
            items={[
              {
                key: `thinking-${idx}`,
                label: (
                  <Space size={8} wrap>
                    <span className="text-shell-ink">思考过程（Thinking）</span>
                    <Tag className="rounded-lg">默认折叠</Tag>
                  </Space>
                ),
                children: (
                  <div className={MODEL_INVOCATION_PROSE_CLASSNAME}>
                    <ReactMarkdown components={markdownComponents} remarkPlugins={[remarkGfm]}>{thinking}</ReactMarkdown>
                  </div>
                ),
              },
            ]}
          />
        </div>
      );
    }

    // Unknown block type - render as JSON
    return (
      <div key={idx} className="mb-2">
        <JsonCodeBlock value={block} maxHeight={200} />
      </div>
    );
  });

  return <div>{items}</div>;
}

function renderOpenAiResponse(r: Record<string, unknown>, maxHeight?: number | string) {
  const choices = r["choices"];
  if (!Array.isArray(choices) || choices.length === 0) {
    return <JsonCodeBlock value={r} maxHeight={maxHeight} />;
  }

  const firstChoice = choices[0] as Record<string, unknown>;
  const message = firstChoice?.["message"] as OpenAIMessage | undefined;

  if (!message) {
    return <JsonCodeBlock value={r} maxHeight={maxHeight} />;
  }

  const content = message["content"];
  const toolCalls = message["tool_calls"];
  const hasContent = (typeof content === "string" && content.length > 0) ||
    (Array.isArray(toolCalls) && toolCalls.length > 0);

  return (
    <div>
      {typeof content === "string" && content.length > 0 && (
        <div className={`mb-3 ${MODEL_INVOCATION_PROSE_CLASSNAME}`}>
          <ReactMarkdown components={markdownComponents} remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
        </div>
      )}

      {Array.isArray(toolCalls) && toolCalls.length > 0 && (
        <ToolCallsTable
          tools={toolCalls.map((tc) => ({
            id: tc.id,
            name: tc.function?.name,
            input: tc.function?.arguments,
          }))}
        />
      )}

      {!hasContent && (
        <Alert
          type="info"
          showIcon
          message="无法解析为结构化格式，请查看 Origin Tab"
          className="rounded-xl border-shell-line"
        />
      )}
    </div>
  );
}
