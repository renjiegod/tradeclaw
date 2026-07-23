// frontend/src/components/assistant/__tests__/MessageContentRenderer.test.tsx

import { cleanup, fireEvent, render as rtlRender, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MessageContentRenderer } from "../MessageContentRenderer";
import type { ToolCallEntry } from "../types";

// The renderer transitively mounts ``InlineToolCallCard`` which now uses
// ``useNavigate`` for the backtest jump button. Wrap all renders in a
// router so the navigate hook resolves cleanly under jsdom.
const render: typeof rtlRender = (ui, options) =>
  rtlRender(<MemoryRouter>{ui}</MemoryRouter>, options);

// Explicit teardown — the new footer-jump tests assert on a single
// ``data-testid`` and otherwise the DOM from prior cases would
// "Found multiple elements" the lookup.
afterEach(() => {
  cleanup();
});

const makeEntry = (overrides: Partial<ToolCallEntry["tool"]> = {}): ToolCallEntry => ({
  tool: {
    type: "tool_use",
    id: "t1",
    name: "test_tool",
    category: "default",
    input: {},
    status: "completed",
    ...overrides,
  },
});

describe("MessageContentRenderer", () => {
  it("renders text content", () => {
    render(<MessageContentRenderer text="Hello **world**" />);
    expect(screen.getByText("Hello")).toBeInTheDocument();
  });

  it("keeps markdown prose full width for long session ids", () => {
    const { container } = render(
      <MessageContentRenderer text="- session_id: `asst-91f127490abe1249abcdef`" />,
    );

    const prose = container.querySelector(".prose");
    expect(prose).toBeTruthy();
    expect(prose?.className).toContain("max-w-none");
  });

  it("renders thinking block when provided", () => {
    render(<MessageContentRenderer text="" thinking="thinking content" />);
    expect(screen.getByText("thinking content")).toBeInTheDocument();
  });

  it("renders separate thinking blocks when provided", () => {
    render(
      <MessageContentRenderer
        text=""
        thinking="legacy thinking"
        thinkingBlocks={[
          { turn: 0, content: "first thinking" },
          { turn: 1, content: "second thinking" },
        ]}
      />,
    );
    expect(screen.getByText("first thinking")).toBeInTheDocument();
    expect(screen.getByText("second thinking")).toBeInTheDocument();
    expect(screen.queryByText("legacy thinking")).not.toBeInTheDocument();
  });

  it("splits inline <think> markup out of the plain text prop", () => {
    render(<MessageContentRenderer text="<think>internal reasoning</think>the visible answer" />);
    expect(screen.getByText("internal reasoning")).toBeInTheDocument();
    expect(screen.getByText("the visible answer")).toBeInTheDocument();
    expect(screen.queryByText(/<think>/)).not.toBeInTheDocument();
  });

  it("splits inline <think> markup out of a text content block", () => {
    render(
      <MessageContentRenderer
        text=""
        contentBlocks={[
          { type: "text", content: "<think>step one</think>final reply" },
        ]}
      />,
    );
    expect(screen.getByText("step one")).toBeInTheDocument();
    expect(screen.getByText("final reply")).toBeInTheDocument();
    expect(screen.queryByText(/<think>/)).not.toBeInTheDocument();
  });

  it("renders tool calls when provided", () => {
    const entries = [makeEntry({ name: "my_tool" })];
    render(<MessageContentRenderer text="" toolCalls={entries} />);
    expect(screen.getByText("my_tool")).toBeInTheDocument();
  });

  it("renders content blocks in recorded order", () => {
    const entries = [makeEntry({ id: "call_1", name: "my_tool" })];
    const { container } = render(
      <MessageContentRenderer
        text="ordered final text"
        thinkingBlocks={[{ turn: 0, content: "legacy first" }]}
        toolCalls={entries}
        contentBlocks={[
          { type: "thinking", turn: 0, content: "first thinking" },
          { type: "tool_call", tool_call_id: "call_1" },
          { type: "thinking", turn: 1, content: "second thinking" },
          { type: "text", content: "ordered final text" },
        ]}
      />,
    );

    const withinRender = Array.from(container.querySelectorAll("*"));
    const byText = (text: string) => withinRender.find((node) => node.textContent === text) as Element;
    const ordered = ["first thinking", "my_tool", "second thinking", "ordered final text"].map(byText);
    expect(ordered[0].compareDocumentPosition(ordered[1])).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(ordered[1].compareDocumentPosition(ordered[2])).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(ordered[2].compareDocumentPosition(ordered[3])).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(screen.queryByText("legacy first")).not.toBeInTheDocument();
    // When item.content matches the trailing text block in content_blocks
    // (the normal case), the fallback is suppressed — only one copy renders.
    expect(screen.getAllByText("ordered final text").length).toBe(1);
  });

  it("renders tool calls from content blocks when external tool state is missing", () => {
    const { container } = render(
      <MessageContentRenderer
        text=""
        contentBlocks={[
          { type: "thinking", content: "first thinking" },
          {
            type: "tool_call",
            tool_call_id: "call_1",
            name: "fallback_tool",
            arguments: { symbol: "600000.SH" },
            status: "completed",
            result_preview: '{"status":"ok"}',
            is_error: false,
          },
          { type: "thinking", content: "second thinking" },
        ]}
      />,
    );

    expect(screen.getByText("fallback_tool")).toBeInTheDocument();
    const nodes = Array.from(container.querySelectorAll("*"));
    const first = nodes.find((node) => node.textContent === "first thinking") as Element;
    const tool = nodes.find((node) => node.textContent === "fallback_tool") as Element;
    const second = nodes.find((node) => node.textContent === "second thinking") as Element;
    expect(first.compareDocumentPosition(tool)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(tool.compareDocumentPosition(second)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });

  it("treats error preview content blocks as failed tool calls", () => {
    render(
      <MessageContentRenderer
        text=""
        contentBlocks={[
          {
            type: "tool_call",
            tool_call_id: "call_err",
            name: "bind_strategy_instance_to_task",
            arguments: {},
            status: "completed",
            result_preview: '{"status":"error","error":"strategy instance not found: "}',
          },
        ]}
      />,
    );

    expect(screen.getByText("bind_strategy_instance_to_task")).toBeInTheDocument();
    expect(screen.getAllByText("失败")[0]).toBeInTheDocument();
  });

  it("renders nothing when all props are empty", () => {
    const { container } = render(<MessageContentRenderer text="" />);
    const div = container.querySelector("div");
    expect(div?.textContent).toBe("");
  });

  it("falls back to item text when content_blocks ends with a tool_call but a final answer exists", () => {
    render(
      <MessageContentRenderer
        text="Final summary answer"
        contentBlocks={[
          { type: "thinking", content: "thinking before tool" },
          {
            type: "tool_call",
            tool_call_id: "call_x",
            name: "execute_bash",
            arguments: {},
            status: "completed",
          },
        ]}
      />,
    );
    expect(screen.getByText("execute_bash")).toBeInTheDocument();
    expect(screen.getByText("Final summary answer")).toBeInTheDocument();
  });

  it("does not duplicate when content_blocks already ends with a text block matching text", () => {
    render(
      <MessageContentRenderer
        text="Final answer"
        contentBlocks={[
          {
            type: "tool_call",
            tool_call_id: "call_x",
            name: "execute_bash",
            arguments: {},
            status: "completed",
          },
          { type: "text", content: "Final answer" },
        ]}
      />,
    );
    const matches = screen.getAllByText("Final answer");
    expect(matches.length).toBe(1);
  });

  describe("message footer backtest jump", () => {
    it("renders the jump button when a run_strategy_backtest tool_call carries task_id in arguments", () => {
      render(
        <MessageContentRenderer
          text="## 回测报告\n\n- 收益率：+7.90%"
          contentBlocks={[
            {
              type: "tool_call",
              tool_call_id: "call_bt_1",
              name: "run_strategy_backtest",
              arguments: { task_id: "task-from-args" },
              status: "completed",
            },
            { type: "text", content: "## 回测报告\n\n- 收益率：+7.90%" },
          ]}
        />,
      );
      // The card renders its own button + the message footer renders one,
      // so 2 buttons are expected.
      const buttons = screen.getAllByText("查看回测任务详情");
      expect(buttons.length).toBeGreaterThanOrEqual(1);
      // The footer-specific one is exposed via data-testid.
      expect(screen.getByTestId("message-backtest-jump")).toBeInTheDocument();
    });

    it("extracts task_id from result_preview when arguments don't carry it", () => {
      render(
        <MessageContentRenderer
          text="OK"
          contentBlocks={[
            {
              type: "tool_call",
              tool_call_id: "call_bt_2",
              name: "run_strategy_backtest",
              arguments: { instance_id: "si-1" },
              status: "completed",
              result_preview: JSON.stringify({
                status: "ok",
                backtest_job: { task_id: "task-from-preview" },
              }),
            },
            { type: "text", content: "OK" },
          ]}
        />,
      );
      expect(screen.getByTestId("message-backtest-jump")).toBeInTheDocument();
    });

    it("renders the jump button when execute_bash invokes `doyoutrade-cli backtest run` and the envelope carries task_id", () => {
      render(
        <MessageContentRenderer
          text="Backtest completed."
          contentBlocks={[
            {
              type: "tool_call",
              tool_call_id: "call_bash_bt",
              name: "execute_bash",
              arguments: {
                command:
                  "doyoutrade-cli backtest run --instance si-1740048518d4 --universe 600522.SH --range-start 2026-03-24 --range-end 2026-05-24",
              },
              status: "completed",
              result_preview: JSON.stringify({
                ok: true,
                data: {
                  status: "ok",
                  report_path: "/tmp/btjob-x.md",
                  auto_created_task_id: "fe0dfc4a-5a59-4bcf-86e2-0f3c4c4a1707",
                  backtest_job: {
                    run_id: "btjob-xyz",
                    task_id: "fe0dfc4a-5a59-4bcf-86e2-0f3c4c4a1707",
                    status: "completed",
                  },
                },
                meta: {},
              }),
            },
            { type: "text", content: "Backtest completed." },
          ]}
        />,
      );
      expect(screen.getByTestId("message-backtest-jump")).toBeInTheDocument();
    });

    it("does NOT trigger on execute_bash calls unrelated to backtest CLI", () => {
      render(
        <MessageContentRenderer
          text="Lookup done."
          contentBlocks={[
            {
              type: "tool_call",
              tool_call_id: "call_bash_lookup",
              name: "execute_bash",
              arguments: { command: "doyoutrade-cli stock lookup 600522" },
              status: "completed",
              result_preview: JSON.stringify({
                ok: true,
                data: { task_id: "should-be-ignored" },
              }),
            },
            { type: "text", content: "Lookup done." },
          ]}
        />,
      );
      expect(screen.queryByTestId("message-backtest-jump")).toBeNull();
    });

    it("does NOT render the footer button when no backtest tool_call exists", () => {
      render(
        <MessageContentRenderer
          text="plain answer"
          contentBlocks={[
            {
              type: "tool_call",
              tool_call_id: "call_x",
              name: "some_unrelated_tool",
              arguments: {},
              status: "completed",
            },
            { type: "text", content: "plain answer" },
          ]}
        />,
      );
      expect(screen.queryByTestId("message-backtest-jump")).toBeNull();
    });

    it("falls back to toolCalls (legacy in-memory path) when content_blocks is absent", () => {
      render(
        <MessageContentRenderer
          text="report body"
          toolCalls={[
            {
              tool: {
                type: "tool_use",
                id: "call_bt_3",
                name: "run_strategy_backtest",
                category: "strategy",
                input: { task_id: "task-from-legacy" },
                status: "completed",
              },
              result: {
                type: "tool_result",
                tool_use_id: "call_bt_3",
                output: { status: "ok" },
                is_error: false,
              },
            },
          ]}
        />,
      );
      expect(screen.getByTestId("message-backtest-jump")).toBeInTheDocument();
    });
  });

  describe("simple (non-debug) render mode", () => {
    const processBlocks = [
      { type: "thinking" as const, content: "先想一步" },
      {
        type: "tool_call" as const,
        tool_call_id: "call_1",
        name: "stock_lookup",
        arguments: { keyword: "600000" },
        status: "completed" as const,
        result_preview: '{"status":"ok"}',
      },
      { type: "text" as const, content: "最终答案" },
    ];

    it("folds thinking + tool calls into one collapsed process card, keeping the answer text visible", () => {
      render(
        <MessageContentRenderer text="最终答案" contentBlocks={processBlocks} debugMode={false} />,
      );
      const card = screen.getByTestId("collapsed-process-card");
      expect(card).toBeInTheDocument();
      expect(screen.getByTestId("process-stage-text").textContent).toBe(
        "思考过程 · 1 段思考 · 1 个工具调用",
      );
      // Collapsed by default: no per-tool card, no thinking body.
      expect(screen.queryByText("stock_lookup")).toBeNull();
      expect(screen.queryByText("先想一步")).toBeNull();
      expect(screen.getByText("最终答案")).toBeInTheDocument();

      fireEvent.click(screen.getByTestId("process-card-header"));
      expect(screen.getByText("stock_lookup")).toBeInTheDocument();
      expect(screen.getByText("先想一步")).toBeInTheDocument();
    });

    it("keeps a single process card per message even when narration text appears between tool calls", () => {
      const { container } = render(
        <MessageContentRenderer
          text="最终答案"
          debugMode={false}
          contentBlocks={[
            { type: "thinking", content: "先想一步" },
            {
              type: "tool_call",
              tool_call_id: "call_1",
              name: "first_tool",
              arguments: {},
              status: "completed",
            },
            { type: "text", content: "查完了，再看一下细节" },
            {
              type: "tool_call",
              tool_call_id: "call_2",
              name: "second_tool",
              arguments: {},
              status: "completed",
            },
            { type: "text", content: "最终答案" },
          ]}
        />,
      );
      // 一次 Agent loop 只出现一张过程卡；中间叙述文本收进卡内。
      expect(screen.getAllByTestId("collapsed-process-card").length).toBe(1);
      expect(screen.queryByText("查完了，再看一下细节")).toBeNull();
      expect(screen.getByText("最终答案")).toBeInTheDocument();

      // 展开后 thinking / 工具 / 中间文本按原始顺序排列。
      fireEvent.click(screen.getByTestId("process-card-header"));
      const nodes = Array.from(container.querySelectorAll("*"));
      const byText = (text: string) =>
        nodes.find((node) => node.textContent === text) as Element;
      const ordered = [
        "先想一步",
        "first_tool",
        "查完了，再看一下细节",
        "second_tool",
      ].map(byText);
      expect(ordered[0].compareDocumentPosition(ordered[1])).toBe(
        Node.DOCUMENT_POSITION_FOLLOWING,
      );
      expect(ordered[1].compareDocumentPosition(ordered[2])).toBe(
        Node.DOCUMENT_POSITION_FOLLOWING,
      );
      expect(ordered[2].compareDocumentPosition(ordered[3])).toBe(
        Node.DOCUMENT_POSITION_FOLLOWING,
      );
    });

    it("renders the trailing process card as a live placeholder while streaming", () => {
      render(
        <MessageContentRenderer
          text=""
          debugMode={false}
          streaming
          contentBlocks={[
            { type: "thinking", content: "思考片段" },
            {
              type: "tool_call",
              tool_call_id: "call_run",
              name: "execute_bash",
              arguments: {},
              status: "running",
            },
          ]}
        />,
      );
      const card = screen.getByTestId("collapsed-process-card");
      expect(card.getAttribute("data-streaming")).toBe("true");
      expect(screen.getByTestId("process-stage-text").textContent).toBe("execute_bash 调用中…");
    });

    it("keeps user_question cards interactive outside the process card", () => {
      const onAnswer = vi.fn();
      render(
        <MessageContentRenderer
          text=""
          debugMode={false}
          onAnswerUserQuestion={onAnswer}
          pendingQuestionId="uq-simple"
          contentBlocks={[
            { type: "thinking", content: "想一想" },
            {
              type: "user_question",
              question_id: "uq-simple",
              question: "选哪个？",
              options: [{ label: "A" }, { label: "B" }],
            },
          ]}
        />,
      );
      expect(screen.getByTestId("collapsed-process-card")).toBeInTheDocument();
      expect(screen.getByTestId("assistant-user-question")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "A" }));
      expect(onAnswer).toHaveBeenCalledWith("uq-simple", { selected: ["A"], custom: undefined });
    });

    it("folds legacy toolCalls into the process card when content_blocks is absent", () => {
      render(
        <MessageContentRenderer
          text="report body"
          debugMode={false}
          toolCalls={[makeEntry({ name: "legacy_tool" })]}
        />,
      );
      expect(screen.getByTestId("collapsed-process-card")).toBeInTheDocument();
      expect(screen.queryByText("legacy_tool")).toBeNull();
      expect(screen.getByText("report body")).toBeInTheDocument();
      fireEvent.click(screen.getByTestId("process-card-header"));
      expect(screen.getByText("legacy_tool")).toBeInTheDocument();
    });

    it("still surfaces the backtest footer jump in simple mode", () => {
      render(
        <MessageContentRenderer
          text="OK"
          debugMode={false}
          contentBlocks={[
            {
              type: "tool_call",
              tool_call_id: "call_bt",
              name: "run_strategy_backtest",
              arguments: { task_id: "task-simple" },
              status: "completed",
            },
            { type: "text", content: "OK" },
          ]}
        />,
      );
      expect(screen.getByTestId("message-backtest-jump")).toBeInTheDocument();
    });
  });

  describe("user_question card", () => {
    const askBlock = {
      type: "user_question" as const,
      question_id: "uq-1",
      question: "选哪个？",
      header: "选择",
      options: [
        { label: "A", description: "选项 A" },
        { label: "B", description: "选项 B" },
      ],
    };

    it("is interactive and answers on click when it is the pending question", () => {
      const onAnswer = vi.fn();
      render(
        <MessageContentRenderer
          text=""
          contentBlocks={[askBlock]}
          onAnswerUserQuestion={onAnswer}
          pendingQuestionId="uq-1"
        />,
      );
      expect(screen.getByText("需要你的选择")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "A" }));
      expect(onAnswer).toHaveBeenCalledWith("uq-1", { selected: ["A"], custom: undefined });
      // The selection collapses into the card as a read-only recap (fizz-style):
      // no separate user bubble, and the option buttons are gone.
      expect(screen.getByTestId("assistant-user-question-recap")).toHaveTextContent("A");
      expect(screen.queryByRole("button", { name: "A" })).toBeNull();
    });

    it("renders a read-only recap once a different question becomes pending", () => {
      const onAnswer = vi.fn();
      render(
        <MessageContentRenderer
          text=""
          contentBlocks={[askBlock]}
          onAnswerUserQuestion={onAnswer}
          pendingQuestionId="uq-other"
        />,
      );
      expect(screen.getByText("该问题已处理")).toBeInTheDocument();
      // Superseded cards render read-only — no clickable options.
      expect(screen.queryByRole("button", { name: "A" })).toBeNull();
      expect(onAnswer).not.toHaveBeenCalled();
    });

    it("renders a read-only recap when nothing is pending", () => {
      render(
        <MessageContentRenderer text="" contentBlocks={[askBlock]} pendingQuestionId={null} />,
      );
      expect(screen.getByText("该问题已处理")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "A" })).toBeNull();
    });

    it("shows the answered selection recap from a persisted block", () => {
      render(
        <MessageContentRenderer
          text=""
          contentBlocks={[{ ...askBlock, answered: true, selected: ["B"] }]}
          pendingQuestionId={null}
        />,
      );
      expect(screen.getByText("该问题已回答")).toBeInTheDocument();
      expect(screen.getByTestId("assistant-user-question-recap")).toHaveTextContent("B");
      expect(screen.queryByRole("button", { name: "A" })).toBeNull();
    });

    it("answers with free-text custom input on single-select", () => {
      const onAnswer = vi.fn();
      render(
        <MessageContentRenderer
          text=""
          contentBlocks={[askBlock]}
          onAnswerUserQuestion={onAnswer}
          pendingQuestionId="uq-1"
        />,
      );
      fireEvent.change(screen.getByTestId("assistant-user-question-custom"), {
        target: { value: "我自己写的答案" },
      });
      fireEvent.click(screen.getByTestId("assistant-user-question-custom-send"));
      expect(onAnswer).toHaveBeenCalledWith("uq-1", { selected: [], custom: "我自己写的答案" });
      expect(screen.getByTestId("assistant-user-question-recap")).toHaveTextContent("我自己写的答案");
    });

    it("defaults to interactive when the caller doesn't track pendingQuestionId", () => {
      const onAnswer = vi.fn();
      render(
        <MessageContentRenderer text="" contentBlocks={[askBlock]} onAnswerUserQuestion={onAnswer} />,
      );
      fireEvent.click(screen.getByRole("button", { name: "A" }));
      expect(onAnswer).toHaveBeenCalledWith("uq-1", { selected: ["A"], custom: undefined });
    });

    it("supports multi_select: toggling options doesn't answer until confirm is clicked", () => {
      const onAnswer = vi.fn();
      const multiBlock = {
        type: "user_question" as const,
        question_id: "uq-2",
        question: "都选哪些？",
        options: [{ label: "A" }, { label: "B" }, { label: "C" }],
        multi_select: true,
      };
      render(
        <MessageContentRenderer
          text=""
          contentBlocks={[multiBlock]}
          onAnswerUserQuestion={onAnswer}
          pendingQuestionId="uq-2"
        />,
      );
      expect(screen.getByText("需要你的选择（可多选）")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "A" }));
      fireEvent.click(screen.getByRole("button", { name: "C" }));
      expect(onAnswer).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "确认选择" }));
      expect(onAnswer).toHaveBeenCalledWith("uq-2", { selected: ["A", "C"], custom: undefined });
    });
  });
});
