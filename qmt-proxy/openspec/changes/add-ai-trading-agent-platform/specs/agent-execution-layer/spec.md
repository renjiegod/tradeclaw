## ADDED Requirements

### Requirement: Intent-based execution pipeline
The system SHALL transform approved trade plans into order intents that pass through validation, policy checks, broker routing, and order-state reconciliation.

#### Scenario: Submit an approved order intent
- **WHEN** a trade plan has passed validation and any required approval steps
- **THEN** the execution layer SHALL convert it into a broker-ready order request and submit it through the configured broker adapter

#### Scenario: Reject an invalid order intent
- **WHEN** a trade plan produces an order intent with invalid symbol, side, quantity, or price constraints
- **THEN** the execution layer SHALL reject the intent before broker submission and record the rejection reason

### Requirement: Hard risk guardrails
The system MUST enforce deterministic risk rules before any live broker order is sent, including position limits, cash checks, trading session windows, and China A-share trading constraints.

#### Scenario: Block an order that exceeds configured limits
- **WHEN** an order intent would breach configured position size, notional, or cash availability limits
- **THEN** the execution layer SHALL reject the order intent and publish a risk rejection event

#### Scenario: Block an order that violates A-share trading rules
- **WHEN** an order intent violates lot size, price-limit, T+1, or exchange session constraints for the instrument
- **THEN** the execution layer SHALL reject the order intent before it reaches the broker adapter

### Requirement: Configurable approval modes
The system SHALL support `manual`, `semi_auto`, and `full_auto` execution modes for live-capable strategies.

#### Scenario: Require operator approval in manual mode
- **WHEN** a live-capable strategy emits a trade plan while the runtime is in `manual` mode
- **THEN** the execution layer SHALL keep the plan in a pending state until an authorized operator approves or rejects it

#### Scenario: Auto-submit in full_auto mode
- **WHEN** a trade plan passes all validations while the runtime is in `full_auto` mode
- **THEN** the execution layer SHALL submit the resulting broker order without waiting for manual approval

### Requirement: Auditable execution and reconciliation
The system SHALL persist every trade decision, order intent, broker request, broker response, fill, cancel event, and reconciliation result with correlation identifiers.

#### Scenario: Reconcile local and broker order state
- **WHEN** the execution layer detects that local order state differs from the broker-reported order state
- **THEN** the system SHALL run reconciliation, update local state, and publish an alert that references the affected order identifiers

#### Scenario: Inspect an execution audit trail
- **WHEN** an operator investigates a submitted or rejected trade
- **THEN** the system SHALL expose the linked strategy decision, approval record, order intent, broker response, and final order status
