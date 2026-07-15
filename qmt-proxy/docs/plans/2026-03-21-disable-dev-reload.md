# Disable Dev Reload Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent the gRPC server from being started twice in development by forcing FastAPI startup to run without Uvicorn reload.

**Architecture:** Keep the current startup flow intact and only change the Uvicorn reload decision in `run.py`. Extract the reload decision into a tiny helper so it can be unit-tested without starting REST or gRPC services.

**Tech Stack:** Python 3.12, Uvicorn, pytest

---

### Task 1: Test startup reload policy

**Files:**
- Create: `tests/unit/test_run.py`
- Test: `tests/unit/test_run.py`

**Step 1: Write the failing test**

```python
def test_get_reload_config_disables_reload_when_debug_enabled():
    settings = make_settings(debug=True)
    assert run.get_reload_config(settings) == (False, None)
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_run.py -v`
Expected: FAIL because `run.get_reload_config` does not exist yet.

**Step 3: Write minimal implementation**

```python
def get_reload_config(settings):
    return False, None
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_run.py -v`
Expected: PASS

### Task 2: Wire helper into startup path

**Files:**
- Modify: `run.py`
- Test: `tests/unit/test_run.py`

**Step 1: Replace inline reload logic**

```python
reload_enabled, reload_includes = get_reload_config(settings)
```

**Step 2: Keep behavior explicit**

Document in the helper docstring that reload is disabled because the current process also starts gRPC and reload would duplicate that startup path.

**Step 3: Run targeted verification**

Run: `pytest tests/unit/test_run.py -v`
Expected: PASS

**Step 4: Run lints for edited files**

Run the editor lint check for `run.py` and `tests/unit/test_run.py`.
