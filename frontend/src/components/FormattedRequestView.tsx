import { Alert, Collapse, Space, Tag, Typography } from "antd";
import type { Components } from "react-markdown";
import ReactMarkdown from "react-markdown";

import { MODEL_INVOCATION_TEXT_CLASSNAME } from "../styles/classNames";
import { tryParseJsonString } from "../utils/jsonSyntaxHint";
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

type MessageBlock = {
  role?: string;
  content?: unknown;
};

type AnthropicRequestContentBlock =
  | { type: "text"; text?: string }
  | { type: "tool_use"; id?: string; name?: string; input?: unknown }
  | { type: "tool_result"; tool_use_id?: string; content?: unknown; is_error?: boolean }
  | { type: string; [key: string]: unknown };

type Props = {
  data: unknown;
  provider: string;
};

function tryParseJsonLoose(raw: string): unknown | null {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function renderToolResultPayload(payload: unknown): React.ReactNode {
  if (typeof payload === "string") {
    const parsed = tryParseJsonLoose(payload);
    if (parsed !== null) {
      return <JsonCodeBlock value={parsed} maxHeight={280} />;
    }
    return (
      <div className={MODEL_INVOCATION_TEXT_CLASSNAME}>
        <ReactMarkdown components={markdownComponents}>{payload}</ReactMarkdown>
      </div>
    );
  }

  if (Array.isArray(payload)) {
    return (
      <div className="space-y-2">
        {payload.map((item, i) => (
          <div key={i}>{renderToolResultPayload(item)}</div>
        ))}
      </div>
    );
  }

  if (payload != null && typeof payload === "object") {
    const block = payload as Record<string, unknown>;
    if (block["type"] === "text" && typeof block["text"] === "string") {
      return (
        <div className={MODEL_INVOCATION_TEXT_CLASSNAME}>
          <ReactMarkdown components={markdownComponents}>{block["text"]}</ReactMarkdown>
        </div>
      );
    }
  }

  return <JsonCodeBlock value={payload} maxHeight={280} />;
}

/** True when `messages[].content` is a Messages-API style block array (Anthropic, many compat gateways). */
function isBlockArrayContent(content: unknown): boolean {
  if (!Array.isArray(content) || content.length === 0) return false;
  return content.every(
    (item) =>
      item != null &&
      typeof item === "object" &&
      typeof (item as Record<string, unknown>)["type"] === "string"
  );
}

/** Render content arrays: text (Markdown), tool_use (table), tool_result (JSON / Markdown), other blocks as JSON. */
function renderBlockArrayMessageContent(content: unknown): React.ReactNode | null {
  if (!Array.isArray(content)) return null;

  const blocks = content as AnthropicRequestContentBlock[];

  const items = blocks.map((block, idx) => {
    if (block["type"] === "text") {
      const text = block["text"];
      if (typeof text !== "string" || text.length === 0) return null;
      return (
        <div key={idx} className={MODEL_INVOCATION_TEXT_CLASSNAME}>
          <ReactMarkdown components={markdownComponents}>{text}</ReactMarkdown>
        </div>
      );
    }

    if (block["type"] === "tool_use") {
      const toolUse = block as { type: "tool_use"; id?: string; name?: string; input?: unknown };
      const id = typeof toolUse.id === "string" ? toolUse.id : `tool-${idx}`;
      const name = typeof toolUse.name === "string" ? toolUse.name : "tool";
      return (
        <div key={idx} className="mb-2">
          <ToolCallsTable tools={[{ id, name, input: toolUse.input }]} />
        </div>
      );
    }

    if (block["type"] === "tool_result") {
      const tr = block as { type: "tool_result"; tool_use_id?: string; content?: unknown; is_error?: boolean };
      const toolUseId = tr.tool_use_id ?? "—";
      return (
        <div key={idx} className="mb-2 rounded-lg border border-dashed border-shell-line bg-shell-surface/40 p-2">
          <Typography.Text type="secondary" className="mb-1 block text-xs">
            tool_result · {toolUseId}
            {tr.is_error ? " · error" : ""}
          </Typography.Text>
          {renderToolResultPayload(tr.content)}
        </div>
      );
    }

    return (
      <div key={idx} className="mb-2">
        <JsonCodeBlock value={block} maxHeight={200} />
      </div>
    );
  });

  const filtered = items.filter(Boolean);
  if (filtered.length === 0) return null;
  return <div className="space-y-2">{filtered}</div>;
}

export function FormattedRequestView({ data, provider }: Props) {
  if (data == null || typeof data !== "object") {
    return <JsonCodeBlock value={data} />;
  }

  const r = data as Record<string, unknown>;
  const isAnthropic = provider === "anthropic";

  const systemPrompt = isAnthropic ? r["system"] : extractOpenAiSystemPrompt(r["messages"]);
  const messages = (r["messages"] as MessageBlock[] | undefined) ?? [];
  const tools = (r["tools"] as Props["data"][] | undefined) ?? [];

  return (
    <div>
      {/* System prompt — Markdown */}
      {systemPrompt != null && typeof systemPrompt === "string" && systemPrompt.length > 0 && (
        <div className="mb-3">
          <Collapse
            defaultActiveKey={["system-md"]}
            items={[
              {
                key: "system-md",
                label: (
                  <Space size={8} wrap>
                    <span className="text-shell-ink">系统提示词（Markdown）</span>
                    <Tag className="rounded-lg">{isAnthropic ? "Anthropic" : "OpenAI"}</Tag>
                  </Space>
                ),
                children: (
                  <div className={`${MODEL_INVOCATION_TEXT_CLASSNAME} max-h-[min(50vh,420px)] overflow-y-auto pr-1`}>
                    <ReactMarkdown components={markdownComponents}>{systemPrompt}</ReactMarkdown>
                  </div>
                ),
              },
            ]}
          />
        </div>
      )}

      {/* Messages */}
      {messages.length > 0 && (
        <div className="mb-3">
          <div className="mb-1">
            <span className="text-sm font-medium text-shell-ink">消息</span>
          </div>
          {messages.map((msg, idx) => {
            const content = msg["content"];
            const blockArray = isBlockArrayContent(content) ? renderBlockArrayMessageContent(content) : null;
            const jsonParsed = blockArray == null ? tryParseJsonString(content) : null;
            const role = msg["role"] ?? "unknown";

            return (
              <div key={idx} className="mb-2 rounded-lg border border-shell-line bg-white p-3">
                <Typography.Text type="secondary" className="mb-1 block text-xs">
                  {role}
                </Typography.Text>
                {blockArray != null ? (
                  blockArray
                ) : jsonParsed ? (
                  <JsonCodeBlock value={jsonParsed} maxHeight={200} />
                ) : typeof content === "string" ? (
                  <div className={MODEL_INVOCATION_TEXT_CLASSNAME}>
                    <ReactMarkdown components={markdownComponents}>{content}</ReactMarkdown>
                  </div>
                ) : (
                  <JsonCodeBlock value={content} maxHeight={200} />
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Tools */}
      {tools.length > 0 && (
        <ToolCallsTable
          tools={tools as Array<{
            type?: string;
            name?: string;
            function?: { name?: string; parameters?: unknown };
            id?: string;
            input?: unknown;
          }>}
        />
      )}

      {/* Fallback */}
      {!systemPrompt && messages.length === 0 && tools.length === 0 && (
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

function extractOpenAiSystemPrompt(messages: unknown): string | null {
  if (!Array.isArray(messages)) return null;
  for (const m of messages) {
    if (m == null || typeof m !== "object") continue;
    const msg = m as Record<string, unknown>;
    const role = typeof msg["role"] === "string" ? msg["role"].toLowerCase() : "";
    if (role !== "system") continue;
    const c = msg["content"];
    if (typeof c === "string" && c.length > 0) return c;
  }
  return null;
}
