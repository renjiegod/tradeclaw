## ADDED Requirements

### Requirement: Pluggable control channel framework
The system SHALL provide a channel framework that exposes a shared command and event model to React UI, Telegram, Feishu, and future operator channels.

#### Scenario: Register multiple channels against the same command model
- **WHEN** the runtime enables React UI and Telegram at the same time
- **THEN** both channels SHALL send commands to the same command bus and receive notifications from the same event bus

#### Scenario: Add a new channel adapter
- **WHEN** developers implement a new operator channel adapter
- **THEN** the adapter SHALL integrate without changing core strategy or execution logic

### Requirement: Unified operational commands
The system SHALL support bot lifecycle control, mode switching, market analysis requests, portfolio inspection, and trade approval actions from any enabled channel.

#### Scenario: Pause the bot from a remote channel
- **WHEN** an authorized operator sends a pause command from Telegram, Feishu, or the React console
- **THEN** the supervisor SHALL transition the bot into a paused state and stop new trade-plan execution until resumed

#### Scenario: Approve a pending trade plan from a control channel
- **WHEN** an authorized operator approves a pending trade plan from any enabled channel
- **THEN** the command SHALL be recorded once and forwarded to the execution layer using the shared approval workflow

### Requirement: Structured alerts and summaries
The system SHALL publish structured notifications for analysis results, pending approvals, risk rejections, order lifecycle changes, and runtime failures.

#### Scenario: Notify operators about a pending approval
- **WHEN** the execution layer places a trade plan into a pending approval state
- **THEN** the channel framework SHALL deliver a structured notification containing the plan summary and the available approval actions

#### Scenario: Notify operators about runtime failures
- **WHEN** the strategy runtime, data layer, or execution layer encounters a critical failure
- **THEN** the channel framework SHALL deliver an alert that includes the failure type, affected component, and correlation identifier

### Requirement: Channel authentication and permission control
The system MUST authenticate operator identities per channel and enforce at least separate permissions for read-only actions and trade-affecting actions.

#### Scenario: Deny trade approval without approval permission
- **WHEN** an authenticated operator without trade-approval permission attempts to approve a pending plan
- **THEN** the system SHALL reject the command and record the authorization failure

#### Scenario: Allow read-only access without trade permissions
- **WHEN** an authenticated read-only operator requests strategy, position, or order status
- **THEN** the system SHALL return the requested status without granting approval or mode-switch privileges
