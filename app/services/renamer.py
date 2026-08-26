from __future__ import annotations

import re
from html import unescape
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import HTTPException

from .parser import NormalizedNode


DEFAULT_TEMPLATE = "{index}|{flag}|{name}|{traffic}|{reset}"
ALLOWED_FIELDS = {"index", "flag", "country", "name", "traffic", "reset", "source", "original"}
_FIELD_RE = re.compile(r"\{([a-z_]+)\}", re.I)
_INDEX_RE = re.compile(r"^(\d{1,3})(?:[-_\s]+|$)")
_COUNTRY_RE = re.compile(r"^([A-Za-z]{2})(?:[-_\s]+|$)")
_FLAG_RE = re.compile(r"^([\U0001F1E6-\U0001F1FF]{2})(?:[-_\s]+|$)")
_TRAFFIC_RE = re.compile(r"(?:📊\s*|流量\s*[:：]?\s*)?(\d+(?:\.\d+)?)\s*([KMGT]B)\b", re.I)
_RESET_RE = re.compile(r"(?:⌛|⏳|重置\s*[:：]?)?\s*(\d+)\s*(?:D|天)\b", re.I)


@dataclass(frozen=True)
class RenameParts:
    index: str = ""
    flag: str = ""
    country: str = ""
    name: str = ""
    traffic: str = ""
    reset: str = ""
    source: str = ""
    original: str = ""


def validate_template(template: str) -> None:
    fields = set(_FIELD_RE.findall(template))
    unknown = fields - ALLOWED_FIELDS
    if unknown:
        raise HTTPException(400, f"节点改名模板包含未知字段: {', '.join(sorted(unknown))}")
    if not fields:
        raise HTTPException(400, "节点改名模板至少需要一个占位符")


def _flag(country: str) -> str:
    normalized = {"UK": "GB"}.get(country.upper(), country.upper())
    if len(normalized) != 2 or not normalized.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(char) - ord("A")) for char in normalized)


def _country_from_flag(flag: str) -> str:
    try:
        return "".join(chr(ord(char) - 0x1F1E6 + ord("A")) for char in flag)
    except (TypeError, ValueError):
        return ""


def _normalized_number(value: str) -> str:
    return value.rstrip("0").rstrip(".") if "." in value else value


def _ignored_prefixes(value: str) -> list[str]:
    return sorted(
        {item.strip().strip("-_| ") for item in re.split(r"[\r\n,，;；]+", value) if item.strip().strip("-_| ")},
        key=len, reverse=True,
    )


def parse_name(original: str, source: str, ignored: str) -> RenameParts:
    original = unescape(original).replace("\xa0", " ").strip()
    segments = [item.strip() for item in re.split(r"[|｜]", original)]
    core = segments[0].strip()
    traffic = ""
    reset = ""
    for segment in segments[1:]:
        traffic_match = _TRAFFIC_RE.search(segment)
        if traffic_match and not traffic:
            traffic = _normalized_number(traffic_match.group(1)) + traffic_match.group(2).upper()
        reset_match = _RESET_RE.search(segment)
        if reset_match and not reset:
            reset = str(int(reset_match.group(1))) + "D"
    for prefix in _ignored_prefixes(ignored):
        if core.casefold() == prefix.casefold():
            core = ""
            break
        if core.casefold().startswith(prefix.casefold()) and core[len(prefix):len(prefix) + 1] in {"-", "_", " ", "|"}:
            core = core[len(prefix):].lstrip("-_| ")
            break

    index = ""
    match = _INDEX_RE.match(core)
    if match:
        index = match.group(1)
        core = core[match.end():]

    country = ""
    flag = ""
    flag_match = _FLAG_RE.match(core)
    if flag_match:
        flag = flag_match.group(1)
        country = _country_from_flag(flag)
        core = core[flag_match.end():]
    else:
        country_match = _COUNTRY_RE.match(core)
        if country_match:
            country = country_match.group(1).upper()
            flag = _flag(country)
            core = core[country_match.end():]

    name = core.strip("-_| ") or original.strip()
    return RenameParts(index, flag, country, name, traffic, reset, source, original)


def render_name(parts: RenameParts, template: str) -> str:
    validate_template(template)
    values = parts.__dict__
    rendered = _FIELD_RE.sub(lambda match: values.get(match.group(1).lower(), ""), template)
    rendered = unescape(rendered).replace("\xa0", " ")
    rendered = re.sub(r"\|(?:\s*\|)+", "|", rendered)
    rendered = re.sub(r"-{2,}", "-", rendered)
    rendered = re.sub(r"\s{2,}", " ", rendered).strip("-_| /·")
    return rendered[:160] or parts.original[:160]


def _set_name(node: NormalizedNode, name: str) -> None:
    node.name = name[:160]
    if node.proxy is not None:
        node.proxy["name"] = node.name
    elif node.uri:
        node.uri = node.uri.rsplit("#", 1)[0] + "#" + quote(node.name, safe="")


def rename_nodes(nodes: list[NormalizedNode], mode: str, ignored: str, template: str,
                 upstreams: list[dict[str, object]] | None = None) -> None:
    by_id = {int(item["id"]): item for item in (upstreams or []) if item.get("id") is not None}
    for node in nodes:
        selected_mode, selected_ignore, selected_template = mode, ignored, template
        # Manual nodes deliberately never inherit the group-level smart rule. Their
        # imported name remains stable until the administrator sets a node alias.
        if node.source_id is None and node.source_name == "手动节点":
            selected_mode = "passthrough"
            selected_ignore = ""
            selected_template = DEFAULT_TEMPLATE
        upstream = by_id.get(node.source_id or -1)
        policy = str(upstream.get("rename_policy", "inherit")) if upstream else "inherit"
        if policy == "disabled":
            policy = "passthrough"
        if policy != "inherit":
            selected_mode = policy
            selected_ignore = str(upstream.get("rename_ignore", ""))
            selected_template = str(upstream.get("rename_template") or DEFAULT_TEMPLATE)
        parts = parse_name(node.original_name, node.source_name, selected_ignore)
        node.traffic, node.reset = parts.traffic, parts.reset
        if selected_mode == "smart":
            validate_template(selected_template)
            new_name = render_name(parts, selected_template)
        else:
            new_name = unescape(node.original_name).replace("\xa0", " ").strip()
        node.rule_name = new_name
        _set_name(node, new_name)
    ensure_unique_names(nodes)


def apply_alias(node: NormalizedNode, alias: str | None, warning: bool = False) -> None:
    node.alias = alias
    if alias:
        dynamic = "|".join(item for item in (node.traffic, node.reset) if item)
        name = alias + (f"|{dynamic}" if dynamic else "")
    else:
        name = node.rule_name or node.name
    _set_name(node, ("⚠️" if warning else "") + name)


def ensure_unique_names(nodes: list[NormalizedNode]) -> None:

    counts: dict[str, int] = {}
    for node in nodes:
        counts[node.name] = counts.get(node.name, 0) + 1
    seen: dict[str, int] = {}
    for node in nodes:
        if counts[node.name] <= 1:
            continue
        original_name = node.name
        seen[original_name] = seen.get(original_name, 0) + 1
        suffix = f" · {node.source_name}"
        if seen[original_name] > 1:
            suffix += f" {seen[original_name]}"
        _set_name(node, original_name + suffix)


def comparison_name(original: str) -> str:
    value = unescape(original).replace("\xa0", " ").strip()
    segments = [item.strip() for item in re.split(r"[|｜]", value)]
    kept = []
    for item in segments:
        if _TRAFFIC_RE.search(item) or _RESET_RE.search(item):
            continue
        kept.append(item)
    return re.sub(r"\s+", " ", "|".join(kept)).strip("-_| ")[:160]
