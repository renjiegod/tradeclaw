## ADDED Requirements

### Requirement: Strategy plugin contract
The system SHALL provide a stable strategy plugin contract for preparing context, generating signals, and building trade plans from canonical market and portfolio inputs.

#### Scenario: Load a valid strategy plugin
- **WHEN** an operator enables a strategy that implements the required contract
- **THEN** the runtime SHALL instantiate the strategy and make it available to backtest, paper, shadow, and live workflows

#### Scenario: Reject an invalid strategy plugin
- **WHEN** a strategy package is missing a required hook or emits an invalid result schema
- **THEN** the runtime SHALL refuse to start the strategy and report the validation error through the operator channels

### Requirement: Structured AI planning workflow
The system SHALL allow a LangChain-based agent planner to analyze approved market context and strategy state, and the planner MUST emit a structured decision object rather than free-form text.

#### Scenario: Produce a structured trade plan candidate
- **WHEN** the planner is triggered for a symbol, watchlist, or portfolio review event
- **THEN** the planner SHALL return a structured result containing an action, rationale, confidence, risk notes, and one or more candidate order intents

#### Scenario: Reject unstructured planner output
- **WHEN** the planner response cannot be validated against the decision schema
- **THEN** the runtime SHALL treat the planning attempt as failed and SHALL NOT forward it to the execution layer

### Requirement: Mode-aware strategy runtime
The system SHALL support `backtest`, `paper`, `shadow`, and `live` modes while preserving the same strategy input and output contracts across modes.

#### Scenario: Reuse the same strategy in backtest and live modes
- **WHEN** an operator runs the same strategy in `backtest` and later in `paper` or `live`
- **THEN** the runtime SHALL evaluate the strategy through the same hooks and only swap the surrounding data and execution adapters

#### Scenario: Compare shadow decisions without sending orders
- **WHEN** a strategy is running in `shadow` mode
- **THEN** the runtime SHALL generate trade plans and evaluations but SHALL NOT send broker orders

### Requirement: Controlled reasoning cadence
The system MUST separate high-frequency market ingestion from slower AI reasoning so that planner invocations happen only on configured triggers.

#### Scenario: Avoid planner calls on every tick
- **WHEN** a real-time quote stream delivers frequent tick updates
- **THEN** the runtime SHALL update caches and features without invoking the AI planner for every incoming event

#### Scenario: Trigger planning on bar close or explicit request
- **WHEN** a configured trigger such as bar close, scheduled rebalance, alert threshold, or operator command occurs
- **THEN** the runtime SHALL invoke the planner for the relevant scope and record the resulting decision
