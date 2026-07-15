import unittest

from doyoutrade.models.reasoning_tags import ReasoningTagStreamPartitioner, strip_reasoning_tags


class StripReasoningTagsTests(unittest.TestCase):
    def test_no_tags_passthrough(self) -> None:
        visible, thinking = strip_reasoning_tags("hello world")
        self.assertEqual(visible, "hello world")
        self.assertEqual(thinking, "")

    def test_single_think_tag(self) -> None:
        visible, thinking = strip_reasoning_tags("before<think>reasoning here</think>after")
        self.assertEqual(visible, "beforeafter")
        self.assertEqual(thinking, "reasoning here")

    def test_thinking_variant_case_insensitive(self) -> None:
        visible, thinking = strip_reasoning_tags("x<THINKING>steps</THINKING>y")
        self.assertEqual(visible, "xy")
        self.assertEqual(thinking, "steps")

    def test_multiple_tags(self) -> None:
        visible, thinking = strip_reasoning_tags(
            "a<think>one</think>b<think>two</think>c"
        )
        self.assertEqual(visible, "abc")
        self.assertEqual(thinking, "onetwo")

    def test_unclosed_tag_treated_as_thinking(self) -> None:
        visible, thinking = strip_reasoning_tags("before<think>never closes")
        self.assertEqual(visible, "before")
        self.assertEqual(thinking, "never closes")

    def test_non_reasoning_tag_left_alone(self) -> None:
        visible, thinking = strip_reasoning_tags("<div>plain html</div>")
        self.assertEqual(visible, "<div>plain html</div>")
        self.assertEqual(thinking, "")

    def test_empty_string(self) -> None:
        visible, thinking = strip_reasoning_tags("")
        self.assertEqual(visible, "")
        self.assertEqual(thinking, "")


class ReasoningTagStreamPartitionerTests(unittest.TestCase):
    def test_tag_within_single_chunk(self) -> None:
        p = ReasoningTagStreamPartitioner()
        parts = p.push("hi <think>steps</think> done") + p.flush()
        self.assertEqual(
            parts,
            [("text", "hi "), ("thinking", "steps"), ("text", " done")],
        )

    def test_tag_split_across_chunks(self) -> None:
        p = ReasoningTagStreamPartitioner()
        out: list[tuple[str, str]] = []
        out += p.push("Hello <th")
        out += p.push("ink>reasoning steps</think>done")
        out += p.flush()
        collapsed = _collapse(out)
        self.assertEqual(
            collapsed,
            [("text", "Hello "), ("thinking", "reasoning steps"), ("text", "done")],
        )

    def test_closing_tag_split_across_chunks(self) -> None:
        p = ReasoningTagStreamPartitioner()
        out: list[tuple[str, str]] = []
        out += p.push("<think>partial reasoning</th")
        out += p.push("ink>visible text")
        out += p.flush()
        collapsed = _collapse(out)
        self.assertEqual(
            collapsed,
            [("thinking", "partial reasoning"), ("text", "visible text")],
        )

    def test_no_tags_streamed_incrementally(self) -> None:
        p = ReasoningTagStreamPartitioner()
        out = p.push("he") + p.push("llo")
        self.assertEqual(out, [("text", "he"), ("text", "llo")])
        self.assertEqual(p.flush(), [])

    def test_dangling_partial_tag_flushed_as_text(self) -> None:
        p = ReasoningTagStreamPartitioner()
        out = p.push("done <th")
        self.assertEqual(out, [("text", "done ")])
        flushed = p.flush()
        self.assertEqual(flushed, [("text", "<th")])

    def test_non_reasoning_angle_bracket_not_buffered(self) -> None:
        p = ReasoningTagStreamPartitioner()
        out = p.push("value <5 and >10")
        self.assertEqual(_collapse(out), [("text", "value <5 and >10")])


def _collapse(parts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    collapsed: list[tuple[str, str]] = []
    for kind, text in parts:
        if collapsed and collapsed[-1][0] == kind:
            collapsed[-1] = (kind, collapsed[-1][1] + text)
        else:
            collapsed.append((kind, text))
    return collapsed


if __name__ == "__main__":
    unittest.main()
