from __future__ import annotations

from pydantic import BaseModel


class RootInfo(BaseModel):
    app_name: str
    app_version: str
    xtquant_mode: str
    description: str
    docs_url: str
    redoc_url: str


class AppInfo(BaseModel):
    name: str
    version: str
    debug: bool
    host: str
    port: int
    log_level: str
    xtquant_mode: str
    allow_real_trading: bool


class HealthStatus(BaseModel):
    status: str
    app_name: str
    app_version: str
    xtquant_mode: str
    timestamp: str


class ServiceStatus(BaseModel):
    status: str
