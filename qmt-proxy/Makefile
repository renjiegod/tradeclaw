.DEFAULT_GOAL := help

UV_PYTHON ?= python
PYTHON_VERSION ?= 3.12
APP_MODE ?= dev
POWERSHELL := powershell -NoProfile -ExecutionPolicy Bypass -File scripts\make.ps1

.PHONY: help bootstrap-uv install sync lock start prod dev start-bg stop force-stop restart status logs clean ui-install ui-dev ui-build ui-preview ui-test

help:
	@$(POWERSHELL) -Action help -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)" -AppMode "$(APP_MODE)"

bootstrap-uv:
	@$(POWERSHELL) -Action bootstrap-uv -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)" -AppMode "$(APP_MODE)"

install: bootstrap-uv
	@$(POWERSHELL) -Action install -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)" -AppMode "$(APP_MODE)"

sync: bootstrap-uv
	@$(POWERSHELL) -Action sync -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)" -AppMode "$(APP_MODE)"

lock: bootstrap-uv
	@$(POWERSHELL) -Action lock -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)" -AppMode "$(APP_MODE)"

start: bootstrap-uv
	@$(POWERSHELL) -Action start -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)" -AppMode "$(APP_MODE)"

prod: bootstrap-uv
	@$(POWERSHELL) -Action prod -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)"

dev: bootstrap-uv
	@$(POWERSHELL) -Action dev -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)" -AppMode "$(APP_MODE)"

start-bg: bootstrap-uv
	@$(POWERSHELL) -Action start-bg -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)" -AppMode "$(APP_MODE)"

stop:
	@$(POWERSHELL) -Action stop -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)" -AppMode "$(APP_MODE)"

force-stop:
	@$(POWERSHELL) -Action force-stop -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)" -AppMode "$(APP_MODE)"

restart:
	@$(POWERSHELL) -Action restart -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)" -AppMode "$(APP_MODE)"

status:
	@$(POWERSHELL) -Action status -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)" -AppMode "$(APP_MODE)"

logs:
	@$(POWERSHELL) -Action logs -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)" -AppMode "$(APP_MODE)"

clean:
	@$(POWERSHELL) -Action clean -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)" -AppMode "$(APP_MODE)"

ui-install:
	@$(POWERSHELL) -Action ui-install -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)" -AppMode "$(APP_MODE)"

ui-dev:
	@$(POWERSHELL) -Action ui-dev -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)" -AppMode "$(APP_MODE)"

ui-build:
	@$(POWERSHELL) -Action ui-build -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)" -AppMode "$(APP_MODE)"

ui-preview:
	@$(POWERSHELL) -Action ui-preview -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)" -AppMode "$(APP_MODE)"

ui-test:
	@$(POWERSHELL) -Action ui-test -PythonExe "$(UV_PYTHON)" -PythonVersion "$(PYTHON_VERSION)" -AppMode "$(APP_MODE)"
