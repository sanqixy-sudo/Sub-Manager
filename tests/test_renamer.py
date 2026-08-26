from __future__ import annotations

from app.services.parser import NormalizedNode
from app.services.renamer import DEFAULT_TEMPLATE, parse_name, rename_nodes, render_name


def test_smart_name_with_traffic_and_reset() -> None:
    parts = parse_name("DMIT-US-04-HK-BoilHKT-Home|📊500.00GB|⌛25D", "Home", "DMIT-US")
    assert render_name(parts, DEFAULT_TEMPLATE) == "04|🇭🇰|BoilHKT-Home|500GB|25D"


def test_smart_name_omits_missing_reset_without_extra_separator() -> None:
    parts = parse_name("DMIT-US-01-US-双ISP家宽|📊193.18GB", "Home", "DMIT-US")
    assert render_name(parts, DEFAULT_TEMPLATE) == "01|🇺🇸|双ISP家宽|193.18GB"


def test_html_space_and_empty_template_fields_are_cleaned() -> None:
    parts = parse_name("DMIT-US-01-US-双ISP家宽|📊192.83GB &#x20;", "Home", "DMIT-US")
    assert render_name(parts, DEFAULT_TEMPLATE) == "01|🇺🇸|双ISP家宽|192.83GB"


def test_passthrough_and_custom_template_are_applied_to_downstream_model() -> None:
    original = "EDGE-02-JP-Tokyo"
    passthrough = NormalizedNode(original, "one", "Provider", proxy={"name": original, "type": "ss"})
    rename_nodes([passthrough], "passthrough", "EDGE", "{flag} {name}")
    assert passthrough.name == original and passthrough.proxy["name"] == original

    smart = NormalizedNode(original, "two", "Provider", proxy={"name": original, "type": "ss"})
    rename_nodes([smart], "smart", "EDGE", "{flag} {name} · {source}")
    assert smart.name == "🇯🇵 Tokyo · Provider"
    assert smart.proxy["name"] == smart.name


def test_remote_passthrough_can_override_smart_group_rule() -> None:
    original = "UNSTRUCTURED Provider Name"
    node = NormalizedNode(original, "remote", "Special Provider", source_id=42,
                          proxy={"name": original, "type": "ss"})
    rename_nodes([node], "smart", "EDGE", "{flag}|{name}", [{
        "id": 42, "rename_policy": "disabled", "rename_ignore": "", "rename_template": DEFAULT_TEMPLATE,
    }])
    assert node.name == original
    assert node.rule_name == original


def test_manual_node_never_inherits_group_smart_rule_but_alias_can_override() -> None:
    original = "DMIT-US-04-HK-BoilHKT-Home|📊500.00GB|⌛25D"
    node = NormalizedNode(original, "manual", "手动节点", proxy={"name": original, "type": "ss"})
    rename_nodes([node], "smart", "DMIT-US", DEFAULT_TEMPLATE)
    assert node.name == original
    assert node.rule_name == original
    assert node.traffic == "500GB" and node.reset == "25D"

    from app.services.renamer import apply_alias
    apply_alias(node, "香港手动家宽")
    assert node.name == "香港手动家宽|500GB|25D"
