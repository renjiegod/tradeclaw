## ADDED Requirements

### Requirement: Unified qmt-proxy market data provider
The system SHALL provide a `MarketDataProvider` abstraction that retrieves historical market data, real-time quote streams, and account context through `qmt_proxy_sdk` without exposing strategy code to raw transport details.

#### Scenario: Load historical bars for strategy evaluation
- **WHEN** a strategy runtime requests historical bars for one or more symbols and a timeframe
- **THEN** the system SHALL fetch the data through the configured provider and return a normalized historical dataset for strategy use

#### Scenario: Open a real-time quote stream
- **WHEN** a live or paper runtime subscribes to one or more symbols
- **THEN** the system SHALL create or reuse a real-time stream from the configured provider and emit normalized quote events to downstream consumers

### Requirement: Canonical market event model
The system SHALL normalize provider-specific responses into canonical event types that include symbol, timeframe, event time, source, and raw payload references.

#### Scenario: Normalize historical responses
- **WHEN** the provider returns historical bar data
- **THEN** the system SHALL convert the response into canonical bar events with deterministic field names and timestamps

#### Scenario: Normalize WebSocket quote responses
- **WHEN** the provider returns quote messages from a real-time stream
- **THEN** the system SHALL convert the response into canonical tick or quote events while preserving the original payload for audit and debugging

### Requirement: Bounded cache and replay support
The system SHALL keep bounded recent market buffers per symbol and timeframe and SHALL persist consumed market events for replay in backtest and post-trade diagnosis workflows.

#### Scenario: Retain only recent live data in memory
- **WHEN** a long-running stream continues to produce events
- **THEN** the system SHALL evict older in-memory events according to the configured buffer policy instead of growing memory usage without bound

#### Scenario: Replay events for diagnosis
- **WHEN** an operator or test workflow requests replay for a prior decision window
- **THEN** the system SHALL load the persisted market events needed to reconstruct the strategy input context for that window

### Requirement: Data quality and gap detection
The system MUST detect stale streams, duplicate events, and missing historical or real-time intervals before the affected data is used for live trade evaluation.

#### Scenario: Detect a gap in real-time data
- **WHEN** the data layer observes a missing interval or a stale heartbeat for an active subscription
- **THEN** the system SHALL mark the feed as degraded and trigger a resynchronization or fallback procedure before new live trade plans are emitted

#### Scenario: Filter duplicate data
- **WHEN** the provider sends the same event more than once for the same symbol and event time
- **THEN** the system SHALL deduplicate the event before it reaches strategy evaluation
