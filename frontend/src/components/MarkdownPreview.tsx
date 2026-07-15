import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { SyntaxHighlighter, oneLight } from "./syntaxHighlighter";

type Props = {
  source: string;
  stripFrontmatter?: boolean;
};

function stripFm(src: string): string {
  if (!src.startsWith("---")) return src;
  const parts = src.split("---");
  if (parts.length < 3) return src;
  return parts.slice(2).join("---").replace(/^\n+/, "");
}

export default function MarkdownPreview({ source, stripFrontmatter }: Props) {
  const body = stripFrontmatter ? stripFm(source) : source;
  return (
    <div className="markdown-preview">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          code(props: any) {
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
                style={oneLight as any}
                language={match[1]}
                PreTag="div"
              >
                {String(children).replace(/\n$/, "")}
              </SyntaxHighlighter>
            );
          },
        }}
      >
        {body}
      </ReactMarkdown>
    </div>
  );
}
