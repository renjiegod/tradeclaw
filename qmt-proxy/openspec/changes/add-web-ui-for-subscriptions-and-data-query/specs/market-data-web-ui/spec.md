## ADDED Requirements

### Requirement: Web UI workbench availability
The system SHALL provide a browser-accessible Web UI at `/ui` that is served by the `qmt-proxy` service and allows users to configure the backend base URL and API Key before using subscription and query features.

#### Scenario: Open the Web UI from the backend service
- **WHEN** a user visits `/ui` in a deployment that includes the frontend build artifacts
- **THEN** the system SHALL return the Web UI shell instead of a raw API response

#### Scenario: Persist connection configuration locally
- **WHEN** a user saves the backend base URL and API Key in the Web UI
- **THEN** the system SHALL reuse those values on the next page load in the same browser

### Requirement: Subscription management workspace
The system SHALL allow users to view active subscriptions, inspect subscription details, create new quote subscriptions, and cancel existing subscriptions by using the current REST subscription APIs.

#### Scenario: Load existing subscriptions
- **WHEN** a user opens the subscription workspace with a valid API Key
- **THEN** the system SHALL request `GET /api/v1/data/subscriptions` and display the returned subscriptions and total count

#### Scenario: Create a quote subscription
- **WHEN** a user submits symbols, period, start date, adjust type, and quote subscription type in the Web UI
- **THEN** the system SHALL call `POST /api/v1/data/subscription` and display the created subscription metadata including `subscription_id`

#### Scenario: Inspect one subscription
- **WHEN** a user requests details for an existing subscription from the list
- **THEN** the system SHALL call `GET /api/v1/data/subscription/{subscription_id}` and display the returned fields including symbols, status, timestamps, and queue size

#### Scenario: Cancel one subscription
- **WHEN** a user confirms deletion for an existing subscription
- **THEN** the system SHALL call `DELETE /api/v1/data/subscription/{subscription_id}` and update the visible subscription list to reflect the cancellation result

### Requirement: Real-time quote stream viewer
The system SHALL allow users to connect to the existing quote WebSocket for a selected subscription and observe connection state, heartbeat feedback, and a bounded history of received quote messages.

#### Scenario: Connect to a valid subscription stream
- **WHEN** a user opens the stream viewer for an existing subscription and starts streaming
- **THEN** the system SHALL connect to `GET /ws/quote/{subscription_id}` and show the `connected` event before displaying quote messages

#### Scenario: Display live quote updates
- **WHEN** the WebSocket receives quote messages for the active subscription
- **THEN** the system SHALL append them to the subscription message history and display the latest payload in a readable form

#### Scenario: Handle invalid or failed stream connections
- **WHEN** the WebSocket returns an `error` event or the connection closes unexpectedly
- **THEN** the system SHALL show the failure state and preserve the most recent received messages for troubleshooting

#### Scenario: Prevent unbounded message growth
- **WHEN** the WebSocket stream keeps delivering new quote messages over time
- **THEN** the system SHALL retain only a bounded recent history per subscription instead of accumulating messages without limit

### Requirement: Market data query workspace
The system SHALL provide a query form for the existing market data REST API and SHALL show both successful results and backend error responses without requiring users to leave the Web UI.

#### Scenario: Submit a market data query
- **WHEN** a user submits stock codes, period, date range, and optional fields in the query workspace
- **THEN** the system SHALL call `POST /api/v1/data/market` and display the returned market data results

#### Scenario: Show backend validation or service errors
- **WHEN** the market data request fails because of invalid input, authentication failure, or backend processing errors
- **THEN** the system SHALL display the error message returned by the backend in the query workspace

#### Scenario: Review result data in structured and raw formats
- **WHEN** a market data request succeeds
- **THEN** the system SHALL provide a structured result view and a raw JSON view for the returned payload
