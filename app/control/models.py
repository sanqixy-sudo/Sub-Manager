from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def hostname(value):
    value = value.strip().rstrip('.')
    try:
        return str(ipaddress.ip_address(value.strip('[]')))
    except ValueError:
        pass
    try:
        value = value.encode('idna').decode('ascii').lower()
    except UnicodeError:
        raise ValueError('请填写有效的 IP 或域名') from None
    if len(value) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', x) for x in value.split('.')):
        raise ValueError('请填写有效的 IP 或域名，不含协议和端口')
    return value


class ServerIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=253)
    enabled: bool = True
    _host = field_validator('host')(hostname)


class NodeIn(BaseModel):
    server_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=80)
    protocol: Literal['anytls', 'vless', 'trojan'] = 'anytls'
    port: int = Field(default=443, ge=1, le=65535)
    server_name: str = Field(min_length=1, max_length=253)
    enabled: bool = True
    next_node_id: int | None = Field(default=None, ge=1)
    multiplier: float = Field(default=1, ge=0.01, le=100)
    certificate_pem: str = Field(default='', max_length=65536)
    private_key_pem: str = Field(default='', max_length=65536)
    _sni = field_validator('server_name')(hostname)

    @field_validator('port')
    @classmethod
    def reserved_port(cls, value):
        if value == 19091:
            raise ValueError('19091 为本机统计服务保留端口')
        return value


class UserIn(BaseModel):
    username: str = Field(pattern=r'^[A-Za-z0-9_-]{2,40}$')
    password: str | None = Field(default=None, min_length=8, max_length=128)
    enabled: bool = True


class PackageIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    quota_gb: float = Field(default=0, ge=0, le=100000)
    cycle_days: int = Field(default=30, ge=1, le=366)
    duration_days: int = Field(default=30, ge=1, le=3660)
    enabled: bool = True
    node_ids: list[int] = Field(default_factory=list, max_length=1000)


class AssignmentIn(BaseModel):
    user_id: int = Field(ge=1)
    package_id: int = Field(ge=1)


class AssignmentUpdate(BaseModel):
    enabled: bool | None = None
    extend_days: int = Field(default=0, ge=0, le=3660)
    reset_traffic: bool = False


class EnrollmentIn(BaseModel):
    token: str = Field(min_length=30, max_length=150)
    instance_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class CounterIn(BaseModel):
    grant_id: int = Field(ge=1)
    upload: int = Field(ge=0, le=2**60)
    download: int = Field(ge=0, le=2**60)


class MetricsIn(BaseModel):
    uptime: float = Field(default=0, ge=0, le=10**12)
    memory_total: int = Field(default=0, ge=0, le=2**60)
    memory_available: int = Field(default=0, ge=0, le=2**60)
    load: float = Field(default=0, ge=0, le=10**6)
    network_upload: int = Field(default=0, ge=0, le=2**63-1)
    network_download: int = Field(default=0, ge=0, le=2**63-1)


class AgentSync(BaseModel):
    instance_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    applied_revision: str = Field(default='', max_length=64, pattern=r'^[a-f0-9]*$')
    runtime_status: Literal['running', 'stopped', 'error'] = 'stopped'
    error_code: Literal['', 'config_invalid', 'start_failed', 'stats_unavailable', 'lease_expired'] = ''
    agent_version: str = Field(default='', max_length=30)
    counters: list[CounterIn] = Field(default_factory=list, max_length=10000)
    metrics: MetricsIn = Field(default_factory=MetricsIn)
