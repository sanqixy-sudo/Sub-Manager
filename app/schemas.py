from __future__ import annotations

import ipaddress

from pydantic import BaseModel, Field, HttpUrl, field_validator


DEFAULT_RENAME_TEMPLATE = "{index}|{flag}|{name}|{traffic}|{reset}"


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UpstreamIn(BaseModel):
    id: int | None = None
    name: str = Field(min_length=1, max_length=100)
    url: HttpUrl
    enabled: bool = True
    rename_policy: str = Field(default="inherit", pattern=r"^(inherit|smart|passthrough|disabled)$")
    rename_ignore: str = Field(default="", max_length=1000)
    rename_template: str = Field(default=DEFAULT_RENAME_TEMPLATE, min_length=1, max_length=300)


class OutputIn(BaseModel):
    id: int | None = None
    client_type: str = Field(max_length=32)
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    update_interval_minutes: int = Field(default=60, ge=5, le=10080)
    enabled: bool = True


class SubscriptionIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=300)
    interval_minutes: int = Field(default=30, ge=5, le=10080)
    enabled: bool = True
    rename_mode: str = Field(default="passthrough", pattern=r"^(passthrough|smart)$")
    rename_ignore: str = Field(default="", max_length=1000)
    rename_template: str = Field(default=DEFAULT_RENAME_TEMPLATE, min_length=1, max_length=300)
    ip_whitelist: str = Field(default="", max_length=10000)
    upstreams: list[UpstreamIn] = Field(default_factory=list)
    outputs: list[OutputIn] = Field(min_length=1)

    @field_validator("ip_whitelist")
    @classmethod
    def validate_ip_whitelist(cls, value: str) -> str:
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        if len(lines) > 100:
            raise ValueError("IP 白名单最多 100 行")
        for index, line in enumerate(lines, start=1):
            try:
                ipaddress.ip_network(line, strict=False)
            except ValueError:
                raise ValueError(f"IP 白名单第 {index} 行不是合法的 IP 或网段: {line[:80]}")
        return "\n".join(lines)


class SettingsUpdate(BaseModel):
    site_name: str = Field(min_length=1, max_length=50)
    public_base_url: str = Field(default="", max_length=300)
    default_refresh_interval_minutes: int = Field(default=30, ge=5, le=10080)
    default_output_interval_minutes: int = Field(default=60, ge=5, le=10080)
    default_client_type: str = Field(default="mihomo", max_length=32)
    upstream_timeout_seconds: int = Field(default=30, ge=5, le=180)
    converter_timeout_seconds: int = Field(default=90, ge=20, le=600)
    pseudo_node_filter: str = Field(max_length=800)
    scheduler_enabled: bool = True
    stale_cache_fallback: bool = True
    skip_failed_upstreams: bool = True
    upstream_user_agent: str = Field(max_length=200)
    scheduler_concurrency: int = Field(default=3, ge=1, le=10)
    health_check_enabled: bool = True
    health_check_interval_minutes: int = Field(default=30, ge=10, le=10080)
    health_check_concurrency: int = Field(default=5, ge=1, le=20)
    health_check_timeout_seconds: int = Field(default=8, ge=3, le=30)
    health_notify_enabled: bool = False
    health_notify_webhook: str = Field(default="", max_length=500)
    health_notify_threshold: int = Field(default=3, ge=1, le=20)
    admin_username: str = Field(min_length=1, max_length=64)
    current_password: str | None = Field(default=None, max_length=128)
    new_password: str | None = Field(default=None, min_length=8, max_length=128)


class InspectPayload(BaseModel):
    content: str = Field(min_length=1, max_length=8 * 1024 * 1024)
    source_name: str = Field(default="临时输入", min_length=1, max_length=100)
    rename_mode: str = Field(default="passthrough", pattern=r"^(passthrough|smart)$")
    rename_ignore: str = Field(default="", max_length=1000)
    rename_template: str = Field(default=DEFAULT_RENAME_TEMPLATE, min_length=1, max_length=300)


class NodePreferenceUpdate(BaseModel):
    alias: str | None = Field(default=None, max_length=80)
    confirm: bool = False

    @field_validator("alias")
    @classmethod
    def validate_alias(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if "\n" in cleaned or "\r" in cleaned:
            raise ValueError("节点别名不能包含换行")
        return cleaned or None


class NodeConfirmPayload(BaseModel):
    node_keys: list[str] | None = Field(default=None, max_length=1000)


class NodeOrderPayload(BaseModel):
    node_keys: list[str] = Field(min_length=1, max_length=5000)


class ManualNodeImport(BaseModel):
    content: str = Field(min_length=1, max_length=8 * 1024 * 1024)


class ManualNodeUpdate(BaseModel):
    enabled: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=10000)
    content: str | None = Field(default=None, min_length=1, max_length=8 * 1024 * 1024)


class HealthTestRequest(BaseModel):
    subscription_id: int | None = Field(default=None, ge=1)
    node_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ProxyCategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    icon: str = Field(default="", max_length=16)
    source_ids: list[int] = Field(default_factory=list, max_length=1000)
    node_keys: list[str] = Field(default_factory=list, max_length=5000)

    @field_validator("name")
    @classmethod
    def validate_category_name(cls, value: str) -> str:
        cleaned = value.strip()
        if any(char in cleaned for char in "\r\n,"):
            raise ValueError("分类名称不能包含逗号或换行")
        if cleaned.upper() in {"PROXY", "DIRECT"} or cleaned in {"♻️ 自动选择", "全部节点"}:
            raise ValueError("分类名称与系统代理组冲突")
        return cleaned
