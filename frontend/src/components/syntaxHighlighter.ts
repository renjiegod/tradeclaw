// Single configured syntax highlighter for the whole console.
//
// The five call sites (JsonCodeBlock / CodeBlock / MarkdownPreview /
// ToolCallsTable / InlineToolCallCard) previously each imported the full
// `Prism` build from react-syntax-highlighter, which bundles refractor with
// every language it ships (~300). That landed as a single ~630 kB chunk even
// though this app only ever renders JSON, Python, shell and a handful of doc
// languages. `PrismLight` ships only refractor core; we register the languages
// we actually use here. Anything unregistered (an exotic fence in a markdown
// doc) degrades gracefully to unhighlighted plain text rather than erroring.
import { PrismLight } from "react-syntax-highlighter";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import diff from "react-syntax-highlighter/dist/esm/languages/prism/diff";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import markdown from "react-syntax-highlighter/dist/esm/languages/prism/markdown";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import toml from "react-syntax-highlighter/dist/esm/languages/prism/toml";
import typescript from "react-syntax-highlighter/dist/esm/languages/prism/typescript";
import yaml from "react-syntax-highlighter/dist/esm/languages/prism/yaml";
import { oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";

PrismLight.registerLanguage("bash", bash);
PrismLight.registerLanguage("diff", diff);
PrismLight.registerLanguage("javascript", javascript);
PrismLight.registerLanguage("json", json);
PrismLight.registerLanguage("markdown", markdown);
PrismLight.registerLanguage("python", python);
PrismLight.registerLanguage("sql", sql);
PrismLight.registerLanguage("toml", toml);
PrismLight.registerLanguage("typescript", typescript);
PrismLight.registerLanguage("yaml", yaml);

export { PrismLight as SyntaxHighlighter, oneLight };
