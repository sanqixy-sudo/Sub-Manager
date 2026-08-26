from __future__ import annotations

import base64
import re
from pathlib import Path

from app.services.parser import merge_nodes, parse_subscription, render_internal_sources


FIXTURES = Path(__file__).parent / "fixtures"
FILTER = re.compile(r"(?i)(剩余流量|套餐到期|距离下次重置)")


def test_full_clash_yaml_extracts_proxies_and_filters_notice() -> None:
    parsed = parse_subscription((FIXTURES / "full_clash.yaml").read_bytes(), "机场 A", FILTER)
    assert parsed.source_format == "clash_yaml"
    assert [node.name for node in parsed.nodes] == ["US Home"]
    assert parsed.filtered_count == 1


def test_3x_ui_clash_preserves_modern_proxy_fields() -> None:
    parsed = parse_subscription((FIXTURES / "3x_ui_clash.yaml").read_bytes(), "3x-ui", FILTER)
    assert len(parsed.nodes) == 2
    assert parsed.nodes[0].proxy["reality-opts"]["short-id"] == "abcd1234"
    assert parsed.nodes[1].proxy["type"] == "tuic"


def test_uri_and_base64_inputs_are_detected() -> None:
    raw = (FIXTURES / "raw_uris.txt").read_bytes()
    direct = parse_subscription(raw, "URI", FILTER)
    encoded = parse_subscription(base64.b64encode(raw), "Base64", FILTER)
    assert direct.source_format == "uri_list"
    assert encoded.source_format == "base64_uri"
    assert len(direct.nodes) == len(encoded.nodes) == 5


def test_merge_deduplicates_and_internal_sources_do_not_contain_original_config() -> None:
    first = parse_subscription((FIXTURES / "full_clash.yaml").read_bytes(), "A", FILTER)
    second = parse_subscription((FIXTURES / "full_clash.yaml").read_bytes(), "B", FILTER)
    nodes, filtered = merge_nodes([first, second])
    sources = render_internal_sources(nodes)
    assert len(nodes) == 1
    assert filtered == 3
    assert b"proxy-groups" not in sources[0][0]
    assert b"rules:" not in sources[0][0]
