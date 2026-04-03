# Tradeclaw Frontend Console Information Architecture Design

## Goal

Upgrade the current single-page frontend prototype into a lightweight admin-console shell that aligns with `docs/design.md`, while staying compatible with the existing backend APIs and preserving the current working instance-management flow.

## Current State

The frontend is currently centered in `frontend/src/App.tsx` and renders one dashboard-style page with:

- top-level platform controls
- instance list
- instance creation card
- approval queue card

This already proves the API loop works, but it does not yet provide the information architecture described in `docs/design.md`:

- no first-level navigation
- no page-level separation of concerns
- no dedicated system or backtest entry points
- instance creation and approvals are embedded in side cards rather than owned pages

## Chosen Approach

Use a lightweight admin-console architecture.

This means:

- keep the current React + Ant Design stack
- keep page switching local to the frontend for now instead of adding a routing library
- introduce a stable application shell with sidebar navigation, top header, and page content region
- split the current monolithic page into a small set of page components and shared UI blocks

This approach is preferred over continuing to grow a single page because it creates clear module boundaries now without forcing a large productization rewrite before the backend is ready.

## Non-Goals

This design does not include:

- URL routing or nested route state
- full agent detail tabs
- backtest charts or report visualizations
- permissions and role-based access control
- new backend APIs just to satisfy the frontend skeleton

Missing backend-supported features should render as explicit empty states or explanatory placeholders instead of speculative fake workflows.

## Layout Design

The frontend should be reorganized into a standard admin shell:

- `Sider`: first-level navigation
- `Header`: global platform actions and high-level status
- `Content`: current page body

### Sidebar Navigation

The first iteration should include these top-level entries:

- `Dashboard`
- `Agent Instances`
- `Create Agent`
- `Approvals`
- `Backtests`
- `System`

These entries map directly to the MVP information architecture from `docs/design.md`, while staying within the set of backend capabilities already present.

### Header Actions

The top header should preserve the platform-wide controls that already exist:

- refresh platform data
- execute one loop tick
- toggle kill switch

These controls should remain globally accessible regardless of which page is active.

### Visual Direction

The console should continue the warm neutral admin style already started in the current UI:

- warm off-white layout background
- white card surfaces
- restrained accent color
- generous whitespace
- minimal shadow and clear card boundaries

The goal is to move closer to the `design.md` “Warm Neutral / Minimal Premium / Calm Dashboard” direction without turning this task into a full visual redesign.

## Page Definitions

### Dashboard

Purpose: provide a top-level health and activity summary.

Content for this iteration:

- platform health
- running instance count
- error instance count
- pending approval count
- kill switch warning banner when enabled
- backend health warning when unavailable

This page should act as the main overview rather than duplicating every workflow in full.

### Agent Instances

Purpose: provide the primary management surface for created agent instances.

Content for this iteration:

- instance table
- instance status
- mode
- cycle count
- error summary
- quick start, pause, and stop actions

The existing instance table component can be retained and moved into this dedicated page.

### Create Agent

Purpose: give instance creation a clear standalone entry point.

Content for this iteration:

- template selection
- name and description input
- mode selection
- orchestrator mode selection
- submit and success feedback

The existing create card behavior should be reused, but the layout should be page-oriented instead of sidebar-oriented.

### Approvals

Purpose: surface pending approvals as a focused operational workflow.

Content for this iteration:

- pending approval list
- approval id and intent id
- created time and expiry time
- approve and reject actions
- empty state when there are no pending approvals

The current approval queue card should be promoted into a first-class page.

### Backtests

Purpose: reserve a stable place in the console for future backtest workflows.

Content for this iteration:

- page heading and scope description
- explicit empty state
- short note that reports and comparisons will be added after backend support exists

This prevents the information architecture from having to change later when backtests become real features.

### System

Purpose: centralize platform-wide state and operational controls.

Content for this iteration:

- kill switch state
- total instance count
- running instance count
- health status
- short operational notes about what the kill switch affects

This page should reuse data already provided by `/health` and `/system/state`.

## Data and API Boundaries

The frontend changes should rely only on existing APIs:

- `GET /health`
- `GET /instances`
- `GET /templates`
- `GET /approvals/pending`
- `GET /system/state`
- `POST /system/kill-switch`
- `POST /system/tick`
- existing instance lifecycle endpoints
- existing approval endpoints

No frontend requirement in this change should depend on a new backend endpoint.

If a page needs richer content than current APIs provide, the first iteration should show an empty state or summary message rather than inventing unsupported data models.

## Frontend Structure

The frontend should be decomposed into focused units.

### Application Shell

`frontend/src/App.tsx` should become the application shell and page coordinator instead of directly owning every card.

Responsibilities:

- load shared platform data
- hold current selected navigation item
- expose global mutations such as refresh and tick
- render the shell layout and active page

### Pages

A new page layer should own first-level screen composition. Expected page components:

- `DashboardPage`
- `InstancesPage`
- `CreateAgentPage`
- `ApprovalsPage`
- `BacktestsPage`
- `SystemPage`

Each page should stay focused on page composition and delegate reusable UI blocks downward.

### Shared Components

Existing components such as instance table and approval queue should remain reusable and be adapted to page use where needed.

Additional shared blocks may include:

- stat summary cards
- empty-state panels
- page section headers

### Types and API Helpers

`frontend/src/types.ts` and `frontend/src/api.ts` should expand only as needed to support page rendering and actions.

Avoid introducing a heavy client-side state management layer in this change.

## Interaction Model

The frontend should continue to refresh platform data from the same polling source already used in the current app.

The shell should expose shared state so that:

- page switches do not refetch unnecessarily unless a mutation occurs
- global actions update every page consistently
- pages consume the same canonical platform snapshot

This keeps the current operational simplicity while making the UI structure more scalable.

## Validation Plan

The implementation should be considered complete when all of the following are true:

- the frontend builds successfully
- the console renders with sidebar, header, and page content regions
- the navigation switches between all planned first-level pages
- existing instance actions still work
- approval actions still work
- kill switch and tick actions still work
- empty-state pages render clearly where backend support is not yet available

## Risks and Mitigations

### Risk: App shell refactor breaks current working actions

Mitigation:

- preserve existing API helpers
- keep global mutations near the application shell
- verify start, pause, stop, approve, reject, tick, and kill switch interactions after refactor

### Risk: Page decomposition becomes over-engineered

Mitigation:

- keep local state simple
- avoid routing and global state libraries in this iteration
- only extract components with clear reuse value

### Risk: Placeholder pages feel unfinished

Mitigation:

- make empty states explicit and intentional
- frame them as planned extension points tied to backend readiness
- ensure high-value operational pages are functional in the first iteration

## Implementation Summary

This change should transform the frontend from a single operational screen into a lightweight admin-console structure that matches the MVP direction in `docs/design.md`. It prioritizes stable navigation, page ownership, and maintainable composition while preserving the current working backend integration and avoiding premature product complexity.
