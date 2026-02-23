"""Tests for ContactStore persistent contact management."""


import pytest


class TestContactStoreEmpty:
    """Tests for empty store behavior."""

    def test_empty_store_has_no_contacts(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(path=tmp_path / "contacts.json")
        assert store.list_contacts() == []

    def test_get_contact_returns_none_for_unknown_url(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(path=tmp_path / "contacts.json")
        assert store.get_contact("http://unknown:8222") is None

    def test_is_known_returns_false_for_empty_store(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(path=tmp_path / "contacts.json")
        assert store.is_known("http://unknown:8222") is False


class TestContactStoreAdd:
    """Tests for adding contacts."""

    @pytest.mark.asyncio
    async def test_add_contact_and_retrieve(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(path=tmp_path / "contacts.json")
        await store.add_contact("http://sarah:8222", "Sarah's Agent")
        contact = store.get_contact("http://sarah:8222")
        assert contact is not None
        assert contact["url"] == "http://sarah:8222"
        assert contact["name"] == "Sarah's Agent"
        assert contact["tier"] == "known"

    @pytest.mark.asyncio
    async def test_add_contact_sets_timestamps(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(path=tmp_path / "contacts.json")
        await store.add_contact("http://sarah:8222", "Sarah's Agent")
        contact = store.get_contact("http://sarah:8222")
        assert contact is not None
        assert "first_seen" in contact
        assert "last_seen" in contact
        assert contact["first_seen"] == contact["last_seen"]

    @pytest.mark.asyncio
    async def test_add_contact_is_known(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(path=tmp_path / "contacts.json")
        await store.add_contact("http://sarah:8222", "Sarah's Agent")
        assert store.is_known("http://sarah:8222") is True
        assert store.is_known("http://unknown:8222") is False


class TestContactStoreUpdateTier:
    """Tests for updating contact tiers."""

    @pytest.mark.asyncio
    async def test_update_tier_changes_tier(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(path=tmp_path / "contacts.json")
        await store.add_contact("http://sarah:8222", "Sarah's Agent", tier="known")
        await store.update_tier("http://sarah:8222", "trusted")
        contact = store.get_contact("http://sarah:8222")
        assert contact is not None
        assert contact["tier"] == "trusted"

    @pytest.mark.asyncio
    async def test_update_tier_noop_for_unknown(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(path=tmp_path / "contacts.json")
        # Should not raise
        await store.update_tier("http://unknown:8222", "trusted")
        assert store.list_contacts() == []


class TestContactStoreRemove:
    """Tests for removing contacts."""

    @pytest.mark.asyncio
    async def test_remove_contact(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(path=tmp_path / "contacts.json")
        await store.add_contact("http://sarah:8222", "Sarah's Agent")
        await store.remove_contact("http://sarah:8222")
        assert store.get_contact("http://sarah:8222") is None
        assert store.list_contacts() == []

    @pytest.mark.asyncio
    async def test_remove_contact_noop_for_unknown(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(path=tmp_path / "contacts.json")
        # Should not raise
        await store.remove_contact("http://unknown:8222")
        assert store.list_contacts() == []


class TestContactStoreList:
    """Tests for listing contacts."""

    @pytest.mark.asyncio
    async def test_list_contacts_returns_all(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(path=tmp_path / "contacts.json")
        await store.add_contact("http://alice:8222", "Alice")
        await store.add_contact("http://bob:8222", "Bob")
        contacts = store.list_contacts()
        assert len(contacts) == 2
        urls = {c["url"] for c in contacts}
        assert urls == {"http://alice:8222", "http://bob:8222"}


class TestContactStoreUpdateLastSeen:
    """Tests for updating last_seen timestamp."""

    @pytest.mark.asyncio
    async def test_update_last_seen(self, tmp_path):
        import asyncio

        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(path=tmp_path / "contacts.json")
        await store.add_contact("http://sarah:8222", "Sarah's Agent")
        contact_before = store.get_contact("http://sarah:8222")
        assert contact_before is not None
        first_seen_before = contact_before["first_seen"]
        last_seen_before = contact_before["last_seen"]

        # Small delay so timestamp differs
        await asyncio.sleep(0.05)
        await store.update_last_seen("http://sarah:8222")

        contact_after = store.get_contact("http://sarah:8222")
        assert contact_after is not None
        # first_seen should NOT change
        assert contact_after["first_seen"] == first_seen_before
        # last_seen SHOULD change
        assert contact_after["last_seen"] > last_seen_before

    @pytest.mark.asyncio
    async def test_update_last_seen_noop_for_unknown(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        store = ContactStore(path=tmp_path / "contacts.json")
        # Should not raise
        await store.update_last_seen("http://unknown:8222")


class TestContactStorePersistence:
    """Tests for file persistence."""

    @pytest.mark.asyncio
    async def test_data_survives_reload(self, tmp_path):
        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        path = tmp_path / "contacts.json"
        store1 = ContactStore(path=path)
        await store1.add_contact("http://sarah:8222", "Sarah's Agent", tier="trusted")

        # Create a new store from the same file
        store2 = ContactStore(path=path)
        contact = store2.get_contact("http://sarah:8222")
        assert contact is not None
        assert contact["name"] == "Sarah's Agent"
        assert contact["tier"] == "trusted"

    def test_corrupt_json_starts_empty(self, tmp_path, caplog):
        import logging

        from amplifier_module_hooks_a2a_server.contacts import ContactStore

        path = tmp_path / "contacts.json"
        path.write_text("{invalid json!!")

        with caplog.at_level(logging.WARNING):
            store = ContactStore(path=path)

        assert store.list_contacts() == []
        assert any(
            "corrupt" in r.message.lower() or "unreadable" in r.message.lower()
            for r in caplog.records
        )
