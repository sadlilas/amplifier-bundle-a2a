"""Tests for Agent Card generation."""


class TestBuildAgentCard:
    def test_default_values(self):
        from amplifier_module_hooks_a2a_server.card import build_agent_card

        card = build_agent_card({})
        assert card["name"] == "Amplifier Agent"
        assert card["description"] == "An Amplifier-powered agent"
        assert card["version"] == "1.0"
        assert card["capabilities"] == {"streaming": False}
        assert card["skills"] == []

    def test_custom_name_and_description(self):
        from amplifier_module_hooks_a2a_server.card import build_agent_card

        card = build_agent_card(
            {"agent_name": "Alice", "agent_description": "Alice's helper"}
        )
        assert card["name"] == "Alice"
        assert card["description"] == "Alice's helper"

    def test_default_port_in_url(self):
        from amplifier_module_hooks_a2a_server.card import build_agent_card

        card = build_agent_card({})
        assert "8222" in card["url"]

    def test_custom_port_in_url(self):
        from amplifier_module_hooks_a2a_server.card import build_agent_card

        card = build_agent_card({"port": 9999})
        assert "9999" in card["url"]

    def test_explicit_base_url_overrides_host_port(self):
        from amplifier_module_hooks_a2a_server.card import build_agent_card

        card = build_agent_card({"base_url": "https://my-agent.example.com"})
        assert card["url"] == "https://my-agent.example.com"

    def test_supported_interfaces_structure(self):
        from amplifier_module_hooks_a2a_server.card import build_agent_card

        card = build_agent_card({"port": 8222})
        interfaces = card["supportedInterfaces"]
        assert len(interfaces) == 1
        assert interfaces[0]["protocolBinding"] == "HTTP+JSON"
        assert interfaces[0]["protocolVersion"] == "1.0"
        assert "8222" in interfaces[0]["url"]

    def test_skills_from_config(self):
        from amplifier_module_hooks_a2a_server.card import build_agent_card

        skills = [{"name": "code-review", "description": "Reviews code for quality"}]
        card = build_agent_card({"skills": skills})
        assert card["skills"] == skills
