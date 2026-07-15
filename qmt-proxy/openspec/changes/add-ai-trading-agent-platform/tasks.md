## 1. AI trader application scaffold

- [ ] 1.1 Create the `apps/ai_trader` Python package boundary and wire it into the repository build and developer workflow
- [ ] 1.2 Add baseline configuration models for runtime mode, qmt-proxy connectivity, risk defaults, and enabled channels
- [ ] 1.3 Implement the supervisor state machine for `backtest`, `paper`, `shadow`, and `live` modes plus `running` and `paused` lifecycle states

## 2. Market data layer

- [ ] 2.1 Define canonical domain models for bar events, tick events, account snapshots, position snapshots, and market context
- [ ] 2.2 Implement `MarketDataProvider` and `QmtProxyMarketDataProvider` using `qmt_proxy_sdk` history, streaming, and account APIs
- [ ] 2.3 Add bounded in-memory buffers, stream heartbeat monitoring, duplicate filtering, and gap detection for live market feeds
- [ ] 2.4 Persist market event journals needed for replay, backtest input loading, and post-trade diagnosis

## 3. Strategy runtime

- [ ] 3.1 Define the strategy plugin contract for context preparation, signal generation, and trade-plan building
- [ ] 3.2 Implement the mode-aware strategy runtime so the same plugin contract works in `backtest`, `paper`, `shadow`, and `live`
- [ ] 3.3 Implement the LangChain or LangGraph planner wrapper with structured output validation and approved tool bindings
- [ ] 3.4 Add configurable planner triggers for bar close, scheduled rebalance, alert threshold, and operator-requested analysis

## 4. Execution and risk layer

- [ ] 4.1 Define trade-plan, order-intent, broker-order, approval-record, and reconciliation-result models with correlation IDs
- [ ] 4.2 Implement the execution policy engine for position limits, cash checks, trading session checks, and A-share trading constraints
- [ ] 4.3 Implement `manual`, `semi_auto`, and `full_auto` approval flows and connect them to the execution pipeline
- [ ] 4.4 Implement `QmtProxyBrokerAdapter` plus reconciliation workers for orders, fills, positions, and assets

## 5. Storage and audit

- [ ] 5.1 Add persistence for agent runs, strategy decisions, trade plans, order intents, broker orders, fills, approval records, channel commands, and runtime alerts
- [ ] 5.2 Implement query services that reconstruct end-to-end decision and execution trails for investigation and UI display
- [ ] 5.3 Add retention and replay helpers so historical decisions can be inspected without parsing raw logs

## 6. Control channels and operator APIs

- [ ] 6.1 Implement the shared command bus and event bus used by all operator channels
- [ ] 6.2 Add FastAPI control endpoints and streaming APIs for bot lifecycle control, analysis requests, plan approval, and runtime status
- [ ] 6.3 Implement Telegram and Feishu channel adapters with authentication, authorization, and notification delivery
- [ ] 6.4 Extend the React frontend with an agent console for market status, strategy decisions, pending approvals, orders, and mode switching

## 7. Verification and rollout

- [ ] 7.1 Add unit tests for provider normalization, strategy contract validation, risk policy enforcement, and approval workflows
- [ ] 7.2 Add integration tests for qmt-proxy-backed data ingestion, paper trading, shadow evaluation, and command-channel control flows
- [ ] 7.3 Add end-to-end smoke tests for the React console and control API against a simulated runtime
- [ ] 7.4 Update repository documentation with architecture, run modes, configuration, security guardrails, and staged rollout guidance
