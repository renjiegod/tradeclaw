"""Flow skills: a mermaid flowchart embedded in SKILL.md drives a multi-step
process node by node.

A skill whose frontmatter declares ``type: flow`` must embed exactly one
```` ```mermaid ```` fenced block in its body. The flowchart is parsed into a
:class:`Flow` graph; the assistant runtime keeps the current node in
``session.config["active_flow"]``, injects a per-attempt ``<system-reminder>``
describing the node and its branches, and advances when the model ends a
reply with ``<choice>...</choice>``.

Supported mermaid subset (anything else raises :class:`FlowParseError` —
authoring errors must fail at load time, not silently degrade at runtime):

* header line ``flowchart TB|TD|LR|RL|BT`` or ``graph ...`` (ignored)
* node definitions ``ID[label]`` / ``ID(label)`` / ``ID{label}`` /
  ``ID(["label"])`` / ``ID((label))`` with optional double-quoted labels
* edges ``A --> B``, ``A -->|label| B``, ``A -- label --> B`` and chains
  ``A --> B --> C``; ``---``, ``-.->`` and ``==>`` normalise to ``-->``
* exactly one node labelled ``BEGIN`` and one labelled ``END``
  (case-insensitive); BEGIN has exactly one outgoing edge; END is reachable
* every node with more than one outgoing edge is a decision node and all of
  its edges must carry unique labels

Node kinds: ``begin`` / ``end`` markers, ``task`` (single outgoing edge,
advanced with ``<choice>next</choice>``), ``decision`` (advanced with
``<choice><edge label></choice>``). ``<choice>abort-flow</choice>`` abandons
the flow from any node.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

FlowNodeKind = Literal["begin", "end", "task", "decision"]

ABORT_CHOICE = "abort-flow"
TASK_ADVANCE_CHOICE = "next"

_CHOICE_RE = re.compile(r"<choice>([^<]*)</choice>")
_MERMAID_FENCE_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.S)
_HEADER_RE = re.compile(r"^(?:flowchart|graph)\b", re.I)
# ``-- label -->`` is rewritten to ``-->|label|`` before arrow splitting.
_INLINE_LABEL_RE = re.compile(r"--\s+([^-><|][^->]*?)\s+-->")
_ARROW_RE = re.compile(r"\s*(?:-\.+->|={2,}>|-{2,}>|-{3,})\s*(?:\|([^|]*)\|\s*)?")
_NODE_RE = re.compile(
    r"^(?P<id>[\w.-]+)\s*"
    r"(?P<body>\(\[.*\]\)|\[\[.*\]\]|\(\(.*\)\)|\[.*\]|\(.*\)|\{.*\})?$",
    re.S,
)
_BRACKET_PAIRS = (("([", "])"), ("[[", "]]"), ("((", "))"), ("[", "]"), ("(", ")"), ("{", "}"))


class FlowError(ValueError):
    """Base error for flow parsing / validation."""


class FlowParseError(FlowError):
    """The mermaid source (or its embedding in SKILL.md) is malformed."""


class FlowValidationError(FlowError):
    """The flowchart parsed but violates flow-skill structural rules."""


@dataclass(frozen=True)
class FlowNode:
    id: str
    label: str
    kind: FlowNodeKind


@dataclass(frozen=True)
class FlowEdge:
    src: str
    dst: str
    label: str | None


@dataclass
class Flow:
    nodes: dict[str, FlowNode]
    outgoing: dict[str, list[FlowEdge]] = field(default_factory=dict)
    begin_id: str = ""
    end_id: str = ""

    def entry_node_id(self) -> str:
        """The first actionable node: BEGIN's single successor."""
        return self.outgoing[self.begin_id][0].dst

    def edges_from(self, node_id: str) -> list[FlowEdge]:
        return list(self.outgoing.get(node_id, []))


def parse_choice(text: str) -> str | None:
    """Last ``<choice>...</choice>`` payload in *text*, or None."""
    matches = _CHOICE_RE.findall(text or "")
    if not matches:
        return None
    return matches[-1].strip()


def strip_choice_tags(text: str) -> str:
    """Remove every ``<choice>`` tag (flow control, not user-facing prose)."""
    return _CHOICE_RE.sub("", text or "").rstrip()


def extract_flow_from_skill_body(body: str) -> Flow:
    """Parse the single ```` ```mermaid ```` block in a flow skill body."""
    blocks = _MERMAID_FENCE_RE.findall(body or "")
    if len(blocks) != 1:
        raise FlowParseError(
            f"flow skill body must contain exactly one ```mermaid block, found {len(blocks)}"
        )
    return parse_mermaid_flowchart(blocks[0])


def _strip_label(body: str | None, node_id: str) -> str:
    if not body:
        return node_id
    text = body.strip()
    for opener, closer in _BRACKET_PAIRS:
        if text.startswith(opener) and text.endswith(closer):
            text = text[len(opener):-len(closer)].strip()
            break
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    return text.replace('\\"', '"').strip() or node_id


def parse_mermaid_flowchart(source: str) -> Flow:
    """Parse the supported mermaid subset into a validated :class:`Flow`."""
    labels: dict[str, str] = {}
    explicit: set[str] = set()
    edges: list[FlowEdge] = []
    order: list[str] = []

    def _register(token: str, line_no: int) -> str:
        match = _NODE_RE.match(token.strip())
        if match is None:
            raise FlowParseError(f"line {line_no}: cannot parse node token {token.strip()!r}")
        node_id = match.group("id")
        body = match.group("body")
        if node_id not in labels:
            labels[node_id] = _strip_label(body, node_id)
            order.append(node_id)
        elif body:
            # A later definition with an explicit label wins over a bare
            # reference, but conflicting explicit labels are an authoring bug.
            label = _strip_label(body, node_id)
            if node_id in explicit and labels[node_id] != label:
                raise FlowParseError(
                    f"line {line_no}: node {node_id!r} redefined with a different "
                    f"label ({labels[node_id]!r} vs {label!r})"
                )
            labels[node_id] = label
        if body:
            explicit.add(node_id)
        return node_id

    for line_no, raw_line in enumerate((source or "").splitlines(), start=1):
        line = raw_line.split("%%", 1)[0].strip()
        if not line:
            continue
        if _HEADER_RE.match(line):
            continue
        if re.match(r"^(subgraph\b|end$|style\b|classDef\b|class\b|click\b)", line):
            raise FlowParseError(
                f"line {line_no}: {line.split()[0]!r} is not supported in flow skills"
            )
        line = _INLINE_LABEL_RE.sub(lambda m: f"-->|{m.group(1).strip()}|", line)
        parts = _ARROW_RE.split(line)
        # re.split with one capture group yields [seg, label, seg, label, ..., seg]
        segments = parts[0::2]
        edge_labels = parts[1::2]
        if len(segments) == 1:
            _register(segments[0], line_no)
            continue
        node_ids = [_register(segment, line_no) for segment in segments]
        for index, label in enumerate(edge_labels):
            cleaned = label.strip() if isinstance(label, str) else None
            edges.append(
                FlowEdge(src=node_ids[index], dst=node_ids[index + 1], label=cleaned or None)
            )

    if not edges:
        raise FlowParseError("flowchart defines no edges")

    nodes: dict[str, FlowNode] = {}
    begin_ids: list[str] = []
    end_ids: list[str] = []
    outgoing: dict[str, list[FlowEdge]] = {node_id: [] for node_id in order}
    for edge in edges:
        outgoing[edge.src].append(edge)

    for node_id in order:
        label = labels[node_id]
        marker = label.strip().upper()
        if marker == "BEGIN":
            kind: FlowNodeKind = "begin"
            begin_ids.append(node_id)
        elif marker == "END":
            kind = "end"
            end_ids.append(node_id)
        elif len(outgoing[node_id]) > 1:
            kind = "decision"
        else:
            kind = "task"
        nodes[node_id] = FlowNode(id=node_id, label=label, kind=kind)

    if len(begin_ids) != 1 or len(end_ids) != 1:
        raise FlowValidationError(
            f"flowchart must have exactly one BEGIN and one END node, "
            f"found {len(begin_ids)} BEGIN / {len(end_ids)} END"
        )
    begin_id, end_id = begin_ids[0], end_ids[0]

    if len(outgoing[begin_id]) != 1:
        raise FlowValidationError(
            f"BEGIN must have exactly one outgoing edge, found {len(outgoing[begin_id])}"
        )
    if outgoing[end_id]:
        raise FlowValidationError("END must not have outgoing edges")

    for node_id, node_edges in outgoing.items():
        if node_id != end_id and not node_edges:
            raise FlowValidationError(
                f"node {node_id!r} ({labels[node_id]!r}) has no outgoing edge and is not END"
            )
        if len(node_edges) > 1:
            edge_label_list = [edge.label for edge in node_edges]
            if any(label is None for label in edge_label_list):
                raise FlowValidationError(
                    f"decision node {node_id!r} has unlabelled outgoing edges; "
                    "every branch of a decision must carry a unique label"
                )
            if len(set(edge_label_list)) != len(edge_label_list):
                raise FlowValidationError(
                    f"decision node {node_id!r} has duplicate branch labels"
                )

    reachable: set[str] = set()
    frontier = [begin_id]
    while frontier:
        current = frontier.pop()
        if current in reachable:
            continue
        reachable.add(current)
        frontier.extend(edge.dst for edge in outgoing[current])
    if end_id not in reachable:
        raise FlowValidationError("END is not reachable from BEGIN")

    return Flow(nodes=nodes, outgoing=outgoing, begin_id=begin_id, end_id=end_id)


@dataclass(frozen=True)
class FlowAdvanceOutcome:
    """Result of applying a model reply to the current flow node.

    ``status``:

    * ``stay`` — no ``<choice>`` tag; the node is still in progress.
    * ``advanced`` — moved to ``next_node_id``.
    * ``completed`` — the chosen edge leads to END; the flow is done.
    * ``aborted`` — the model chose ``abort-flow``.
    * ``invalid`` — the choice matched no branch; node unchanged.
    * ``broken`` — the recorded node no longer exists in the flowchart
      (skill edited mid-flow); callers must surface this, not skip it.
    """

    status: Literal["stay", "advanced", "completed", "aborted", "invalid", "broken"]
    choice: str | None = None
    next_node_id: str | None = None
    reason: str | None = None


def advance_flow(flow: Flow, node_id: str, reply_text: str) -> FlowAdvanceOutcome:
    """Apply the model's reply to the current node and decide the transition."""
    choice = parse_choice(reply_text)
    if choice is None:
        return FlowAdvanceOutcome(status="stay")
    node = flow.nodes.get(node_id)
    if node is None:
        return FlowAdvanceOutcome(
            status="broken",
            choice=choice,
            reason=f"current node {node_id!r} not present in flowchart",
        )
    if choice == ABORT_CHOICE:
        return FlowAdvanceOutcome(status="aborted", choice=choice)

    edges = flow.edges_from(node_id)
    target: FlowEdge | None = None
    if node.kind == "decision":
        for edge in edges:
            if edge.label == choice:
                target = edge
                break
        if target is None:
            folded = choice.casefold()
            for edge in edges:
                if (edge.label or "").casefold() == folded:
                    target = edge
                    break
    else:
        # task (or begin, defensively): accept the generic "next" or the
        # single edge's label when the author gave it one.
        only = edges[0] if edges else None
        if only is not None and (
            choice == TASK_ADVANCE_CHOICE
            or (only.label is not None and choice.casefold() == only.label.casefold())
        ):
            target = only

    if target is None:
        return FlowAdvanceOutcome(
            status="invalid",
            choice=choice,
            reason=f"choice {choice!r} matches no branch of node {node_id!r}",
        )
    if flow.nodes[target.dst].kind == "end":
        return FlowAdvanceOutcome(status="completed", choice=choice, next_node_id=target.dst)
    return FlowAdvanceOutcome(status="advanced", choice=choice, next_node_id=target.dst)


def build_flow_reminder_text(
    *,
    skill_name: str,
    flow: Flow,
    node_id: str,
    invalid_choice: str | None = None,
) -> str | None:
    """Per-attempt ``<system-reminder>`` describing the current flow node.

    Returns None when *node_id* no longer exists (callers abort the flow
    with a visible event instead of injecting a stale prompt).
    """
    node = flow.nodes.get(node_id)
    if node is None:
        return None
    lines = [
        "<system-reminder>",
        "# activeFlow",
        f"You are executing the flow skill '{skill_name}'. "
        f"Current step ({node.kind}): {node.label}",
    ]
    if node.kind == "decision":
        lines.append("When you have decided, end your reply with exactly one of:")
        for edge in flow.edges_from(node_id):
            dst = flow.nodes[edge.dst]
            lines.append(f"- <choice>{edge.label}</choice> → {dst.label}")
    else:
        edges = flow.edges_from(node_id)
        dst_label = flow.nodes[edges[0].dst].label if edges else "END"
        lines.append(
            "Work on this step now. Only once it is fully done in this reply, "
            f"end the reply with <choice>{TASK_ADVANCE_CHOICE}</choice> (next: {dst_label}). "
            "If the step is still in progress or you are waiting on the user, "
            "do not emit any <choice> tag."
        )
    if invalid_choice:
        lines.append(
            f"Note: your previous choice <choice>{invalid_choice}</choice> matched no "
            "branch above — pick one of the listed choices this time."
        )
    lines.append(
        f"To abandon this flow entirely, end the reply with <choice>{ABORT_CHOICE}</choice>."
    )
    lines.append("</system-reminder>")
    return "\n".join(lines)
