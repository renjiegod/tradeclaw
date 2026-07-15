"""Unit tests for the flow-skill mermaid parser and advance semantics."""

import unittest

from doyoutrade.skills.flow import (
    FlowParseError,
    FlowValidationError,
    advance_flow,
    build_flow_reminder_text,
    extract_flow_from_skill_body,
    parse_choice,
    parse_mermaid_flowchart,
    strip_choice_tags,
)

_VALID = """
flowchart TB
    A(["BEGIN"]) --> B[收集需求]
    B --> C{需求清晰吗}
    C -->|清晰| D[实现]
    C -->|不清晰| B
    D -- 验证 --> E(["END"])
"""


class MermaidParserTests(unittest.TestCase):
    def test_parses_nodes_edges_and_kinds(self):
        flow = parse_mermaid_flowchart(_VALID)
        self.assertEqual(flow.begin_id, "A")
        self.assertEqual(flow.end_id, "E")
        self.assertEqual(flow.nodes["B"].kind, "task")
        self.assertEqual(flow.nodes["C"].kind, "decision")
        self.assertEqual(flow.nodes["C"].label, "需求清晰吗")
        self.assertEqual(flow.entry_node_id(), "B")
        labels = sorted(edge.label for edge in flow.edges_from("C"))
        self.assertEqual(labels, ["不清晰", "清晰"])
        # ``-- label -->`` inline form
        self.assertEqual(flow.edges_from("D")[0].label, "验证")

    def test_chain_edges_and_comments(self):
        flow = parse_mermaid_flowchart(
            """
            %% comment line
            A[BEGIN] --> B[step] --> C[END]  %% trailing comment
            """
        )
        self.assertEqual(flow.entry_node_id(), "B")
        self.assertEqual(flow.edges_from("B")[0].dst, "C")

    def test_missing_end_rejected(self):
        with self.assertRaises(FlowValidationError):
            parse_mermaid_flowchart("A[BEGIN] --> B[task]\nB --> B")

    def test_unlabelled_decision_branch_rejected(self):
        with self.assertRaises(FlowValidationError):
            parse_mermaid_flowchart(
                "A[BEGIN] --> B[pick]\nB -->|yes| C[END]\nB --> D[other]\nD --> C"
            )

    def test_duplicate_decision_labels_rejected(self):
        with self.assertRaises(FlowValidationError):
            parse_mermaid_flowchart(
                "A[BEGIN] --> B[pick]\nB -->|x| C[END]\nB -->|x| D[t]\nD --> C"
            )

    def test_unreachable_end_rejected(self):
        with self.assertRaises(FlowValidationError):
            parse_mermaid_flowchart(
                "A[BEGIN] --> B[loop]\nB -->|again| B\nB -->|more| B\nE[END]\nF[x] --> E"
            )

    def test_subgraph_rejected_loudly(self):
        with self.assertRaises(FlowParseError):
            parse_mermaid_flowchart("subgraph S\nA[BEGIN] --> B[END]\nend")

    def test_dead_end_task_rejected(self):
        with self.assertRaises(FlowValidationError):
            parse_mermaid_flowchart("A[BEGIN] --> B[task]\nA2[x] --> E[END]\nB --> A2\nC[dead]")

    def test_body_must_have_exactly_one_mermaid_block(self):
        with self.assertRaises(FlowParseError):
            extract_flow_from_skill_body("no fence here")
        with self.assertRaises(FlowParseError):
            extract_flow_from_skill_body(
                "```mermaid\nA[BEGIN] --> B[END]\n```\n```mermaid\nA[BEGIN] --> B[END]\n```"
            )
        flow = extract_flow_from_skill_body(
            "intro\n```mermaid\nA[BEGIN] --> B[step]\nB --> C[END]\n```\noutro"
        )
        self.assertEqual(flow.entry_node_id(), "B")


class ChoiceAndAdvanceTests(unittest.TestCase):
    def setUp(self):
        self.flow = parse_mermaid_flowchart(_VALID)

    def test_parse_and_strip_choice(self):
        self.assertEqual(parse_choice("做完了\n<choice>next</choice>"), "next")
        self.assertIsNone(parse_choice("没有标签"))
        # Last tag wins; stripping removes every tag.
        text = "<choice>a</choice> mid <choice>b</choice>"
        self.assertEqual(parse_choice(text), "b")
        self.assertEqual(strip_choice_tags(text), " mid")

    def test_task_advances_with_next(self):
        outcome = advance_flow(self.flow, "B", "收集完毕 <choice>next</choice>")
        self.assertEqual(outcome.status, "advanced")
        self.assertEqual(outcome.next_node_id, "C")

    def test_decision_advances_by_label(self):
        outcome = advance_flow(self.flow, "C", "<choice>清晰</choice>")
        self.assertEqual(outcome.status, "advanced")
        self.assertEqual(outcome.next_node_id, "D")

    def test_decision_loop_back_branch(self):
        outcome = advance_flow(self.flow, "C", "<choice>不清晰</choice>")
        self.assertEqual(outcome.next_node_id, "B")

    def test_edge_to_end_completes(self):
        outcome = advance_flow(self.flow, "D", "<choice>next</choice>")
        self.assertEqual(outcome.status, "completed")

    def test_task_accepts_edge_label_alias(self):
        outcome = advance_flow(self.flow, "D", "<choice>验证</choice>")
        self.assertEqual(outcome.status, "completed")

    def test_invalid_choice_reported(self):
        outcome = advance_flow(self.flow, "C", "<choice>也许</choice>")
        self.assertEqual(outcome.status, "invalid")
        self.assertEqual(outcome.choice, "也许")

    def test_abort_choice(self):
        outcome = advance_flow(self.flow, "B", "<choice>abort-flow</choice>")
        self.assertEqual(outcome.status, "aborted")

    def test_no_tag_stays(self):
        self.assertEqual(advance_flow(self.flow, "B", "还在进行").status, "stay")

    def test_missing_node_is_broken(self):
        outcome = advance_flow(self.flow, "ZZ", "<choice>next</choice>")
        self.assertEqual(outcome.status, "broken")

    def test_reminder_text_shapes(self):
        task_text = build_flow_reminder_text(
            skill_name="demo", flow=self.flow, node_id="B"
        )
        self.assertIn("收集需求", task_text)
        self.assertIn("<choice>next</choice>", task_text)
        decision_text = build_flow_reminder_text(
            skill_name="demo", flow=self.flow, node_id="C", invalid_choice="也许"
        )
        self.assertIn("<choice>清晰</choice>", decision_text)
        self.assertIn("<choice>不清晰</choice>", decision_text)
        self.assertIn("也许", decision_text)
        self.assertIsNone(
            build_flow_reminder_text(skill_name="demo", flow=self.flow, node_id="ZZ")
        )


if __name__ == "__main__":
    unittest.main()
