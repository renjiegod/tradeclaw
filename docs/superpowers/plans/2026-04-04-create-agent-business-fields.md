# Create Agent Business Fields Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Create Agent page capture every editable business field from the `instances` table and make the backend API normalize, validate, persist, and return those fields consistently.

**Architecture:** Keep the existing `/instances` creation flow and extend it rather than introducing a parallel endpoint. Backend normalization lives at the API boundary so the service layer receives clean Python values, while the frontend form handles user-friendly string inputs for symbol lists and JSON settings before sending structured payloads.

**Tech Stack:** FastAPI, SQLAlchemy repositories, React, Ant Design, TypeScript, Vite

---

### Task 1: Lock backend response and validation behavior with tests

**Files:**
- Modify: `tests/test_api_app.py`
- Modify: `tests/test_platform_service.py`

- [ ] **Step 1: Write the failing API tests**

```python
def test_create_instance_returns_all_business_fields(self):
    ...

def test_create_instance_rejects_non_object_settings(self):
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api_app.py tests/test_platform_service.py -q`
Expected: FAIL because the fake service / API assertions do not yet cover the expanded payload and response contract.

- [ ] **Step 3: Write minimal implementation**

```python
def _normalize_watch_symbols(value):
    ...

def _normalize_settings(value):
    ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_api_app.py tests/test_platform_service.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_api_app.py tests/test_platform_service.py tradeclaw/api/app.py tradeclaw/platform/service.py
git commit -m "feat: validate create-agent business fields"
```

### Task 2: Extend frontend payload typing and create form inputs

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/components/CreateAgentCard.tsx`
- Modify: `frontend/src/pages/CreateAgentPage.tsx`

- [ ] **Step 1: Write the failing client-side contract in types and submission flow**

```ts
type CreateInstancePayload = {
  data_provider?: string;
  watch_symbols?: string[];
  execution_strategy?: string;
  account_id?: string;
  model_id?: string;
  settings?: Record<string, unknown> | null;
};
```

- [ ] **Step 2: Run test/build to verify it fails**

Run: `npm run build`
Expected: FAIL or type errors until the form and payload transformation are updated together.

- [ ] **Step 3: Write minimal implementation**

```tsx
const symbols = parseWatchSymbols(values.watch_symbols_text);
const settings = parseSettingsJson(values.settings_text);
await createInstance({ ...values, watch_symbols: symbols, settings });
```

- [ ] **Step 4: Run test/build to verify it passes**

Run: `npm run build`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types.ts frontend/src/components/CreateAgentCard.tsx frontend/src/pages/CreateAgentPage.tsx
git commit -m "feat: expand create-agent form fields"
```

### Task 3: Verify the end-to-end create flow contract

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `tradeclaw/api/app.py`
- Modify: `tradeclaw/platform/service.py`
- Test: `tests/test_api_app.py`

- [ ] **Step 1: Add assertions for normalized response shape**

```python
self.assertEqual(body["watch_symbols"], ["AAPL", "MSFT"])
self.assertEqual(body["settings"], {"risk": "low"})
```

- [ ] **Step 2: Run targeted verification**

Run: `pytest tests/test_api_app.py tests/test_platform_service.py tests/test_persistence.py -q && npm run build`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api.ts tradeclaw/api/app.py tradeclaw/platform/service.py tests/test_api_app.py tests/test_platform_service.py
git commit -m "feat: return complete create-agent payload"
```
