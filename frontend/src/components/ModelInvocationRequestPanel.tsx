import { Alert, Collapse, Space, Tag } from "antd";
import type { Components } from "react-markdown";
import ReactMarkdown from "react-markdown";

import { MODEL_INVOCATION_TEXT_CLASSNAME } from "../styles/classNames";
import { analyzeModelInvocationRequest } from "../utils/modelInvocationRequestPreview";
import { JsonPanel } from "./JsonPanel";

type Props = {
  data: unknown;
  /** Passed through to the JSON block below the optional Markdown preview. */
  maxHeight?: number | string;
  /** Show the system prompt Markdown preview. Defaults to true. */
  showMarkdown?: boolean;
};

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

export function ModelInvocationRequestPanel({ data, maxHeight, showMarkdown = true }: Props) {
  const preview = analyzeModelInvocationRequest(data);

  return (
    <>
      {showMarkdown && preview.kind === "markdown" ? (
        <div className="mb-5">
          <Collapse
            defaultActiveKey={["system-md"]}
            items={[
              {
                key: "system-md",
                label: (
                  <Space size={8} wrap>
                    <span className="text-shell-ink">系统提示词（Markdown）</span>
                    <Tag className="rounded-lg">{preview.protocolLabel}</Tag>
                  </Space>
                ),
                children: (
                  <div className={`${MODEL_INVOCATION_TEXT_CLASSNAME} max-h-[min(50vh,420px)] overflow-y-auto pr-1`}>
                    <ReactMarkdown components={markdownComponents}>{preview.text}</ReactMarkdown>
                  </div>
                ),
              },
            ]}
          />
        </div>
      ) : null}
      {preview.kind === "notice" ? (
        <Alert
          className="mb-5 rounded-xl border-shell-line"
          type="info"
          showIcon
          message={
            <Space size={8} wrap>
              <span>{preview.message}</span>
              <Tag className="rounded-lg">{preview.protocolLabel}</Tag>
            </Space>
          }
        />
      ) : null}
      <JsonPanel title="原始请求 (request)" data={data} maxHeight={maxHeight} />
    </>
  );
}
