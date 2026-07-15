## 1. Frontend workspace setup

- [x] 1.1 Create a new `web/` frontend workspace with Vite, React, TypeScript, and Ant Design dependencies
- [x] 1.2 Add frontend scripts for install, dev, build, preview, and test execution
- [x] 1.3 Establish the base application shell, theme tokens, and single-workbench layout for `/ui`

## 2. Shared configuration and transport

- [x] 2.1 Implement a shared client configuration layer for backend base URL and API Key with `localStorage` persistence
- [x] 2.2 Implement a REST client wrapper that sends `Authorization: Bearer <api-key>` and normalizes backend errors for the UI
- [x] 2.3 Implement a WebSocket helper that derives `ws://` or `wss://` endpoints from the configured backend base URL

## 3. Subscription management and live stream UI

- [x] 3.1 Build the subscription list view that loads `GET /api/v1/data/subscriptions` and shows summary fields for each subscription
- [x] 3.2 Build the subscription creation form for quote subscriptions and wire it to `POST /api/v1/data/subscription`
- [x] 3.3 Build the subscription detail and deletion actions using `GET /api/v1/data/subscription/{subscription_id}` and `DELETE /api/v1/data/subscription/{subscription_id}`
- [x] 3.4 Build the real-time stream viewer that connects to `/ws/quote/{subscription_id}`, shows connection state, and renders a bounded message history

## 4. Market data query workspace

- [x] 4.1 Build the market data query form for stock codes, period, date range, fields, adjust type, and fill options
- [x] 4.2 Wire the query form to `POST /api/v1/data/market` and render successful results in structured and raw JSON views
- [x] 4.3 Add loading, empty, and backend error states so failed queries remain diagnosable in the UI

## 5. Backend integration, testing, and documentation

- [x] 5.1 Mount the production frontend build output in FastAPI at `/ui` without changing the existing `/` API response
- [x] 5.2 Add frontend tests for configuration persistence, subscription workflows, stream handling, and market query rendering
- [x] 5.3 Add backend integration coverage or smoke tests for `/ui` static delivery and update README with Web UI setup and usage instructions
