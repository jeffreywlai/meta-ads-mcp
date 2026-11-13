"""Ad tool tests."""

from __future__ import annotations

import asyncio

import pytest

from meta_ads_mcp.tools import ads


class FakeAdsClient:
    """Fake ad client."""

    def __init__(self, *, currency: str = "USD") -> None:
        self.created_payload = None
        self.currency = currency

    async def create_edge_object(self, parent_id: str, edge: str, *, data, files=None):
        self.created_payload = {"parent_id": parent_id, "edge": edge, "data": data}
        return {"id": "ad_created", "payload": data}

    async def get_object(self, object_id: str, *, fields=None, params=None):
        if object_id == "act_123":
            assert fields == ["currency"]
            return {"id": object_id, "currency": self.currency}
        if object_id == "ad_123":
            return {
                "id": "ad_123",
                "name": "Ad 123",
                "account_id": "act_123",
                "creative": {"id": "crt_123"},
            }
        return {
            "id": "crt_123",
            "name": "Creative 123",
            "image_hash": "hash_123",
            "thumbnail_url": "https://example.com/thumb.png",
            "object_story_spec": {"link_data": {"picture": "https://example.com/story.png"}},
            "asset_feed_spec": {"images": [{"hash": "hash_456", "url": "https://example.com/feed.png"}]},
        }

    async def get_ad_images_by_hashes(self, account_id: str, *, hashes, fields=None):
        assert account_id == "act_123"
        assert hashes == ["hash_123", "hash_456"]
        return {
            "data": [
                {"hash": "hash_123", "url": "https://cdn.example.com/hash_123.png"},
                {"hash": "hash_456", "permalink_url": "https://cdn.example.com/hash_456.png"},
            ]
        }


def test_create_ad_wraps_creative_id(monkeypatch) -> None:
    client = FakeAdsClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    result = asyncio.run(
        ads.create_ad(
            account_id="123",
            name="New Ad",
            adset_id="adset_123",
            creative_id="crt_123",
            bid_amount=12.34,
        )
    )
    assert result["created"]["id"] == "ad_created"
    assert client.created_payload["parent_id"] == "act_123"
    assert client.created_payload["data"]["creative"] == {"creative_id": "crt_123"}
    assert client.created_payload["data"]["bid_amount"] == 1234


def test_create_ad_encodes_exact_decimal_bid(monkeypatch) -> None:
    client = FakeAdsClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    asyncio.run(
        ads.create_ad(
            account_id="123",
            name="New Ad",
            adset_id="adset_123",
            creative_id="crt_123",
            bid_amount=19.99,
        )
    )
    assert client.created_payload["data"]["bid_amount"] == 1999


def test_create_ad_encodes_zero_decimal_bid(monkeypatch) -> None:
    client = FakeAdsClient(currency="JPY")
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    asyncio.run(
        ads.create_ad(
            account_id="123",
            name="JPY Ad",
            adset_id="adset_123",
            creative_id="crt_123",
            bid_amount=1250.0,
        )
    )
    assert client.created_payload["data"]["bid_amount"] == 1250


@pytest.mark.parametrize("bid_amount", [0, -1, float("inf")])
def test_create_ad_rejects_invalid_bid_before_client_lookup(
    monkeypatch,
    bid_amount,
) -> None:
    monkeypatch.setattr(
        ads,
        "get_graph_api_client",
        lambda: (_ for _ in ()).throw(AssertionError("client should not be created")),
    )
    with pytest.raises(ads.ValidationError, match="bid_amount"):
        asyncio.run(
            ads.create_ad(
                account_id="123",
                name="Invalid Bid",
                adset_id="adset_123",
                creative_id="crt_123",
                bid_amount=bid_amount,
            )
        )


def test_create_ad_accepts_nested_graph_creative_shape(monkeypatch) -> None:
    client = FakeAdsClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    result = asyncio.run(
        ads.create_ad(
            account_id="123",
            name="Nested creative",
            adset_id="adset_123",
            creative={"creative_id": "crt_123"},
            validate_only=True,
        )
    )
    assert client.created_payload["data"]["creative"] == {"creative_id": "crt_123"}
    assert client.created_payload["data"]["execution_options"] == ["validate_only"]
    assert result["validation_only"] is True


def test_create_ad_rejects_conflicting_creative_inputs(monkeypatch) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: FakeAdsClient())
    with pytest.raises(ads.ValidationError, match="must match"):
        asyncio.run(
            ads.create_ad(
                account_id="123",
                name="Conflict",
                adset_id="adset_123",
                creative_id="crt_1",
                creative={"creative_id": "crt_2"},
            )
        )


def test_create_ad_rejects_ignored_nested_creative_fields(monkeypatch) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: FakeAdsClient())
    with pytest.raises(ads.ValidationError, match="unexpected fields"):
        asyncio.run(
            ads.create_ad(
                account_id="123",
                name="Unsupported creative body",
                adset_id="adset_123",
                creative={"creative_id": "crt_1", "name": "would be ignored"},
            )
        )


@pytest.mark.parametrize(
    "params, expected_field",
    [
        ({"bid_amount": 25}, "bid_amount"),
        ({"execution_options": ["validate_only"]}, "execution_options"),
    ],
)
def test_create_ad_params_cannot_supply_omitted_managed_fields(
    monkeypatch,
    params,
    expected_field,
) -> None:
    monkeypatch.setattr(
        ads,
        "get_graph_api_client",
        lambda: (_ for _ in ()).throw(AssertionError("client should not be created")),
    )
    with pytest.raises(ads.ValidationError, match=expected_field):
        asyncio.run(
            ads.create_ad(
                account_id="123",
                name="Ad",
                adset_id="adset_123",
                creative_id="crt_123",
                params=params,
            )
        )


def test_get_ad_image_resolves_candidates(monkeypatch) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: FakeAdsClient())
    result = asyncio.run(ads.get_ad_image(ad_id="ad_123"))
    assert result["item"]["creative_id"] == "crt_123"
    assert result["item"]["image_hashes"] == ["hash_123", "hash_456"]
    assert result["item"]["best_image_url"] == "https://example.com/thumb.png"
    assert result["summary"]["resolved_image_count"] == 2


def test_image_rules_preserve_crops_defaults_and_ambiguous_assets() -> None:
    creative = {"asset_feed_spec": {
        "images": [
            {"hash": "portrait1", "adlabels": [{"name": "story"}], "image_crops": {"9x16": [[0, 0], [9, 16]]}},
            {"hash": "portrait2", "adlabels": [{"name": "story"}]},
            {"hash": "square", "adlabels": [{"name": "default"}]},
        ],
        "asset_customization_rules": [
            {"image_label": {"name": "story"}, "is_default": False, "priority": 1,
             "customization_spec": {"instagram_positions": ["story", "stream", "future_position"], "age_min": 25}},
            {"image_label": {"name": "default"}, "is_default": True},
            {"image_label": {"name": "missing"}},
            {"video_label": {"name": "video"}},
        ],
    }}
    rules = ads._configured_image_rules(creative, [{"hash": "portrait1", "url": "https://example.com/portrait", "original_width": 900}])
    assert [image["hash"] for image in rules[0]["images"]] == ["portrait1", "portrait2"]
    assert rules[0]["images"][0]["url"] == "https://example.com/portrait"
    assert rules[0]["images"][0]["image_crops"] == {"9x16": [[0, 0], [9, 16]]}
    assert rules[0]["customization_spec"]["age_min"] == 25
    assert rules[0]["is_default"] is False
    assert [position["platform_position"] for position in rules[0]["configured_reporting_positions"]] == ["story", "feed", "future_position"]
    assert rules[1]["is_default"] is True
    assert rules[2]["images"] == []
    assert rules[2]["mapping_status"] == "unresolved_image_label"
    assert rules[2]["is_default"] is None  # Do not infer a default from absent rules.
    assert rules[3]["mapping_status"] == "no_image_label"


def test_image_rules_are_exposed_without_claiming_actual_delivery(monkeypatch) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: FakeAdsClient())
    result = asyncio.run(ads.get_ad_image(ad_id="ad_123"))
    assert result["item"]["configured_image_rules"] == []
    assert result["item"]["placement_delivery_verified"] is False
    assert "not actual delivery" in result["summary"]["placement_note"]


def test_bulk_image_inspection_reuses_creative_and_image_reads_for_95_ads(monkeypatch) -> None:
    calls = []

    class BulkClient(FakeAdsClient):
        async def get_object(self, object_id, *, fields=None, params=None):
            calls.append(object_id)
            if object_id.startswith("ad_"):
                return {"id": object_id, "name": object_id, "account_id": "act_123", "creative": {"id": "crt_123"}}
            return await super().get_object(object_id, fields=fields, params=params)

        async def get_ad_images_by_hashes(self, account_id, *, hashes, fields=None):
            calls.append((account_id, tuple(hashes)))
            return await super().get_ad_images_by_hashes(account_id, hashes=hashes, fields=fields)

    monkeypatch.setattr(ads, "get_graph_api_client", lambda: BulkClient())
    result = asyncio.run(ads.get_ad_image(ad_ids=[f"ad_{index}" for index in range(95)]))
    assert len(result["items"]) == 95
    assert result["summary"]["complete"] is True
    assert result["summary"]["unique_creative_count"] == 1
    assert len(calls) == 97  # 95 ad reads, one creative read, one image lookup.
    assert calls.count("crt_123") == 1


def test_bulk_image_cache_is_account_scoped_and_preserves_unresolved_hashes(monkeypatch) -> None:
    from meta_ads_mcp.coordinator import mcp_server
    accounts = []

    class AccountImagesClient:
        async def get_object(self, object_id, *, fields=None):
            if object_id.startswith("ad_"):
                return {"id": object_id, "account_id": "123" if object_id == "ad_1" else "456", "creative": {"id": "crt"}}
            return {"image_hash": "same_hash"}

        async def get_ad_images_by_hashes(self, account_id, *, hashes, fields=None):
            accounts.append(account_id)
            return {"data": [{"hash": "same_hash", "url": "https://example.com/123"}]} if account_id == "123" else {"data": []}

    monkeypatch.setattr(ads, "get_graph_api_client", lambda: AccountImagesClient())
    result = asyncio.run(mcp_server.call_tool("get_ad_image", {"ad_ids": "ad_1,ad_1,ad_2"})).structured_content
    assert accounts == ["123", "456"]
    assert result["summary"]["count"] == 2
    assert result["items"][0]["resolved_images"][0]["url"] == "https://example.com/123"
    assert result["items"][1]["unresolved_image_hashes"] == ["same_hash"]
    assert result["items"][1]["resolved_images"] == []


@pytest.mark.parametrize("arguments", [
    {}, {"ad_id": "ad_1", "ad_ids": ["ad_2"]}, {"ad_ids": []}, {"ad_id": " "},
    {"ad_ids": [f"ad_{index}" for index in range(101)]},
])
def test_bulk_image_inputs_are_bounded_before_client(monkeypatch, arguments) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(ads.ValidationError):
        asyncio.run(ads.get_ad_image(**arguments))


def test_get_ad_image_handles_ad_without_creative(monkeypatch) -> None:
    class NoCreativeClient(FakeAdsClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            if object_id == "ad_123":
                return {"id": "ad_123", "name": "Ad 123", "account_id": "act_123"}
            return await super().get_object(object_id, fields=fields, params=params)

    monkeypatch.setattr(ads, "get_graph_api_client", lambda: NoCreativeClient())
    result = asyncio.run(ads.get_ad_image(ad_id="ad_123"))
    assert result["item"]["creative_id"] is None
    assert result["summary"]["resolved_image_count"] == 0


def test_get_ad_image_skips_resolution_without_account_id(monkeypatch) -> None:
    class NoAccountClient(FakeAdsClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            payload = await super().get_object(object_id, fields=fields, params=params)
            if object_id == "ad_123":
                payload.pop("account_id", None)
            return payload

        async def get_ad_images_by_hashes(self, account_id: str, *, hashes, fields=None):
            raise AssertionError("should not resolve hashes without account_id")

    monkeypatch.setattr(ads, "get_graph_api_client", lambda: NoAccountClient())
    result = asyncio.run(ads.get_ad_image(ad_id="ad_123"))
    assert result["item"]["account_id"] is None
    assert result["summary"]["resolved_image_count"] == 0


def test_get_ad_image_handles_no_hashes(monkeypatch) -> None:
    class NoHashClient(FakeAdsClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            if object_id == "crt_123":
                return {"id": "crt_123", "name": "Creative 123", "thumbnail_url": "https://example.com/thumb.png"}
            return await super().get_object(object_id, fields=fields, params=params)

    monkeypatch.setattr(ads, "get_graph_api_client", lambda: NoHashClient())
    result = asyncio.run(ads.get_ad_image(ad_id="ad_123"))
    assert result["item"]["image_hashes"] == []
    assert result["item"]["best_image_url"] == "https://example.com/thumb.png"


def test_get_ad_image_deduplicates_duplicate_urls(monkeypatch) -> None:
    class DuplicateUrlClient(FakeAdsClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            if object_id == "crt_123":
                return {
                    "id": "crt_123",
                    "name": "Creative 123",
                    "image_hash": "hash_123",
                    "thumbnail_url": "https://example.com/dup.png",
                    "image_url": "https://example.com/dup.png",
                }
            return await super().get_object(object_id, fields=fields, params=params)

        async def get_ad_images_by_hashes(self, account_id: str, *, hashes, fields=None):
            return {"data": [{"hash": "hash_123", "url": "https://example.com/dup.png"}]}

    monkeypatch.setattr(ads, "get_graph_api_client", lambda: DuplicateUrlClient())
    result = asyncio.run(ads.get_ad_image(ad_id="ad_123"))
    assert len(result["item"]["image_candidates"]) == 1


def test_get_ad_image_supports_asset_feed_only_images(monkeypatch) -> None:
    class AssetFeedOnlyClient(FakeAdsClient):
        async def get_object(self, object_id: str, *, fields=None, params=None):
            if object_id == "crt_123":
                return {
                    "id": "crt_123",
                    "name": "Creative 123",
                    "asset_feed_spec": {"images": [{"hash": "hash_999", "url": "https://example.com/feed-only.png"}]},
                }
            return await super().get_object(object_id, fields=fields, params=params)

        async def get_ad_images_by_hashes(self, account_id: str, *, hashes, fields=None):
            assert hashes == ["hash_999"]
            return {"data": []}

    monkeypatch.setattr(ads, "get_graph_api_client", lambda: AssetFeedOnlyClient())
    result = asyncio.run(ads.get_ad_image(ad_id="ad_123"))
    assert result["item"]["best_image_url"] == "https://example.com/feed-only.png"


class FakeUpdateAdClient:
    """Fake client for update_ad that records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    async def get_object(self, object_id: str, *, fields=None, params=None):
        self.calls.append(("get", object_id, list(fields or [])))
        return {
            "id": object_id,
            "name": "Old ad",
            "status": "PAUSED",
            "creative": {"id": "crt_old"},
            "tracking_specs": [{"action.type": ["offsite_conversion"], "fb_pixel": ["1"]}],
        }

    async def update_object(self, object_id: str, *, data):
        self.calls.append(("update", object_id, data))
        return {"success": True}


def test_update_ad_reads_then_updates_with_before_after(monkeypatch) -> None:
    client = FakeUpdateAdClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)

    result = asyncio.run(ads.update_ad(ad_id="ad_1", name="New ad", creative_id="crt_new", status="ACTIVE"))

    assert [call[0] for call in client.calls] == ["get", "update"]
    assert {"name", "status", "creative{id}"} <= set(client.calls[0][2])
    assert client.calls[1][2] == {"name": "New ad", "creative": {"creative_id": "crt_new"}, "status": "ACTIVE"}
    assert result["action"] == "update_ad"
    assert result["target"] == {"ad_id": "ad_1"}
    assert result["previous"] == {"name": "Old ad", "creative_id": "crt_old", "status": "PAUSED"}
    assert result["current"] == {"name": "New ad", "creative_id": "crt_new", "status": "ACTIVE"}
    assert result["validation_only"] is False


def test_update_ad_validate_only_and_tracking_specs(monkeypatch) -> None:
    client = FakeUpdateAdClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    specs = [{"action.type": ["offsite_conversion"], "fb_pixel": ["2"]}]
    result = asyncio.run(ads.update_ad(ad_id="ad_1", tracking_specs=specs, validate_only=True))
    assert client.calls[1][2] == {"tracking_specs": specs, "execution_options": ["validate_only"]}
    assert result["validation_only"] is True
    assert result["previous"]["tracking_specs"][0]["fb_pixel"] == ["1"]


def test_update_ad_requires_a_change(monkeypatch) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(ads.ValidationError, match="At least one field"):
        asyncio.run(ads.update_ad(ad_id="ad_1"))
    with pytest.raises(ads.ValidationError, match="At least one field"):
        asyncio.run(ads.update_ad(ad_id="ad_1", validate_only=True, params={}))


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"ad_id": "", "name": "x"}, "ad_id is required"),
        ({"ad_id": "ad_1", "status": "DELETED"}, "status must be"),
        ({"ad_id": "ad_1", "creative_id": " "}, "creative_id"),
        ({"ad_id": "ad_1", "params": {"bid_amount": 100}}, "bid_amount"),
        ({"ad_id": "ad_1", "params": {"adset_id": "s2"}}, "adset_id"),
        ({"ad_id": "ad_1", "params": {"creative": {"creative_id": "c"}}}, "creative"),
    ],
)
def test_update_ad_validation_errors_make_no_call(monkeypatch, arguments, message) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(ads.ValidationError, match=message):
        asyncio.run(ads.update_ad(**arguments))


def test_update_ad_accepts_archived(monkeypatch) -> None:
    client = FakeUpdateAdClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    result = asyncio.run(ads.update_ad(ad_id="ad_1", status="ARCHIVED"))
    assert client.calls[1] == ("update", "ad_1", {"status": "ARCHIVED"})
    assert result["previous"] == {"status": "PAUSED"}
    assert result["current"] == {"status": "ARCHIVED"}
    assert "cannot be restored to active from this server" in ads.update_ad.__doc__


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"bid_info": {"ACTIONS": 150}}, "bid_info"),
    ],
)
def test_update_ad_rejects_minor_unit_money_in_params(monkeypatch, params, message) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(ads.ValidationError, match=message):
        asyncio.run(ads.update_ad(ad_id="ad_1", params=params))


def test_update_ad_names_a_changed_field_meta_did_not_return(monkeypatch) -> None:
    class SparseClient(FakeUpdateAdClient):
        async def get_object(self, object_id, *, fields=None, params=None):
            self.calls.append(("get", object_id, list(fields or [])))
            return {"id": object_id, "name": "Old ad"}

    client = SparseClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    result = asyncio.run(ads.update_ad(
        ad_id="ad_1", name="New ad", creative_id="crt_new",
        params={"conversion_domain": "example.com", "not_a_readable_field": 1},
    ))
    assert client.calls[0] == ("get", "ad_1", ["id", "name", "creative{id}", "conversion_domain"])
    assert result["previous"] == {"name": "Old ad"}
    assert result["previous_unavailable"] == {
        "creative_id": "Meta returned no value for it; it may not have been set.",
        "conversion_domain": "Meta returned no value for it; it may not have been set.",
        "not_a_readable_field": "Not a field this server reads back from an ad.",
    }
    assert result["current"]["creative_id"] == "crt_new"


class AdCopyClient:
    """Fake client for an ad's copies edge."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def create_edge_object(self, parent_id, edge, *, data, files=None):
        self.calls.append(("create", parent_id, edge, dict(data)))
        return {"copied_ad_id": "622"}

    async def update_object(self, object_id, *, data):
        self.calls.append(("update", object_id, dict(data)))
        return {"success": True}


def test_duplicate_ad_defaults_to_paused(monkeypatch) -> None:
    client = AdCopyClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    result = asyncio.run(ads.duplicate_ad(ad_id="611"))
    assert client.calls == [("create", "611", "copies", {"status_option": "PAUSED"})]
    assert result["action"] == "duplicate_ad"
    assert result["target"] == {"ad_id": "611"}
    assert result["new_id"] == "622"
    assert result["new_ids"] == {"ad_id": "622"}
    assert result["copied_objects"] == []


def test_duplicate_ad_into_another_ad_set_with_rename_rule_and_new_name(monkeypatch) -> None:
    client = AdCopyClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    result = asyncio.run(ads.duplicate_ad(
        ad_id="611", adset_id="700", status_option="ACTIVE",
        rename_strategy="NO_RENAME", new_name="Hero ad B",
    ))
    assert client.calls == [
        ("create", "611", "copies", {
            "status_option": "ACTIVE", "rename_options": {"rename_strategy": "NO_RENAME"}, "adset_id": "700",
        }),
        ("update", "622", {"name": "Hero ad B"}),
    ]
    assert result["rename"] == {"requested": "Hero ad B", "applied": True}


def test_duplicate_ad_dry_run_sends_nothing(monkeypatch) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    result = asyncio.run(ads.duplicate_ad(ad_id="611", adset_id="700", dry_run=True))
    assert result["dry_run"] is True
    assert result["sent"] is False
    assert result["requests"] == [
        {"method": "POST", "path": "611/copies", "data": {"status_option": "PAUSED", "adset_id": "700"}},
    ]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"ad_id": "611", "status_option": "ARCHIVED"}, "status_option"),
        ({"ad_id": "611", "rename_strategy": "x"}, "rename_strategy"),
        ({"ad_id": "611", "adset_id": ""}, "adset_id"),
        ({"ad_id": "611", "new_name": ""}, "new_name"),
    ],
)
def test_duplicate_ad_input_errors_make_no_call(monkeypatch, arguments, message) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(ads.ValidationError, match=message):
        asyncio.run(ads.duplicate_ad(**arguments))


class RecordingAdsClient(FakeAdsClient):
    """Fake ad client that records every call in order."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.calls: list[tuple] = []

    async def get_object(self, object_id, *, fields=None, params=None):
        self.calls.append(("get", object_id, list(fields or [])))
        return await super().get_object(object_id, fields=fields, params=params)

    async def create_edge_object(self, parent_id, edge, *, data, files=None):
        self.calls.append(("create", parent_id, edge, dict(data)))
        return {"id": "ad_created"}


def test_create_ad_params_only_call_is_unchanged(monkeypatch) -> None:
    """A call that puts the new fields in params, as callers do today, sends exactly what it sent before."""
    client = RecordingAdsClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    params = {"conversion_domain": "example.com", "adlabels": [{"name": "Q4"}]}
    result = asyncio.run(ads.create_ad(
        account_id="123", name="Old style ad", adset_id="adset_123",
        creative={"id": "crt_123"}, params=dict(params),
    ))
    assert client.calls == [
        ("create", "act_123", "ads", {
            "name": "Old style ad",
            "adset_id": "adset_123",
            "creative": {"creative_id": "crt_123"},
            "status": "PAUSED",
            **params,
        }),
    ]
    assert list(client.calls[0][3]) == ["name", "adset_id", "creative", "status", *params]
    assert result == {
        "ok": True, "action": "create_ad", "target": {"account_id": "act_123", "adset_id": "adset_123"},
        "validation_only": False, "created": {"id": "ad_created"},
    }


AD_BASE = {"account_id": "123", "name": "Ad", "adset_id": "adset_123"}
INLINE_SPEC = {
    "name": "Inline creative",
    "object_story_spec": {
        "page_id": "111",
        "link_data": {"link": "https://example.com", "message": "Hello", "image_hash": "abc"},
    },
}


@pytest.mark.parametrize(
    ("named", "params_value"),
    [
        ({"conversion_domain": "example.com"}, "example.org"),
        ({"adlabels": [{"name": "Q4"}]}, [{"name": "Q4"}]),
    ],
)
def test_create_ad_named_parameter_and_same_params_key_together_raise(monkeypatch, named, params_value) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    (key, _value), = named.items()
    with pytest.raises(ads.ValidationError, match=rf"{key} was given both as a named parameter and inside params"):
        asyncio.run(ads.create_ad(**AD_BASE, creative_id="crt_123", **named, params={key: params_value}))


def test_create_ad_named_parameters_are_sent(monkeypatch) -> None:
    client = RecordingAdsClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    asyncio.run(ads.create_ad(
        **AD_BASE, creative_id="crt_123", conversion_domain="shop.example.co.uk",
        adlabels=[{"name": "Q4"}], validate_only=True, params={"ad_schedule_start_time": "1795000000"},
    ))
    assert client.calls == [
        ("create", "act_123", "ads", {
            "name": "Ad",
            "adset_id": "adset_123",
            "creative": {"creative_id": "crt_123"},
            "status": "PAUSED",
            "conversion_domain": "shop.example.co.uk",
            "adlabels": [{"name": "Q4"}],
            "execution_options": ["validate_only"],
            "ad_schedule_start_time": "1795000000",
        }),
    ]


def test_create_ad_sends_inline_creative_spec(monkeypatch) -> None:
    client = RecordingAdsClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    result = asyncio.run(ads.create_ad(**AD_BASE, creative_spec=INLINE_SPEC))
    assert client.calls == [
        ("create", "act_123", "ads", {
            "name": "Ad", "adset_id": "adset_123", "creative": INLINE_SPEC, "status": "PAUSED",
        }),
    ]
    assert result["created"] == {"id": "ad_created"}


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"creative_spec": INLINE_SPEC, "creative_id": "crt_1"}, "exactly one of creative_id, creative or creative_spec"),
        ({"creative_spec": INLINE_SPEC, "creative": {"id": "crt_1"}}, "exactly one of creative_id, creative or creative_spec"),
        ({"creative_spec": {}}, "creative_spec must not be empty"),
        ({"creative_spec": {"creative_id": "crt_1"}}, "use creative_id"),
        ({"creative_spec": {"id": "crt_1", "name": "x"}}, "use creative_id"),
        ({"creative_id": "crt_1", "conversion_domain": "https://example.com/path"}, "domain only"),
        ({"creative_id": "crt_1", "conversion_domain": " "}, "conversion_domain"),
        ({"creative_id": "crt_1", "adlabels": [{}]}, "adlabels"),
        ({"creative_id": "crt_1", "params": {"creative": INLINE_SPEC}}, "creative"),
    ],
)
def test_create_ad_new_input_errors_make_no_call(monkeypatch, arguments, message) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(ads.ValidationError, match=message):
        asyncio.run(ads.create_ad(**AD_BASE, **arguments))


def test_create_ad_without_any_creative_keeps_its_error(monkeypatch) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(ads.ValidationError, match="creative_id"):
        asyncio.run(ads.create_ad(**AD_BASE))


def test_create_ad_old_style_creative_id_call_is_unchanged(monkeypatch) -> None:
    client = RecordingAdsClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    result = asyncio.run(ads.create_ad(
        **AD_BASE, creative_id="crt_123", bid_amount=12.34,
        tracking_specs=[{"action.type": ["offsite_conversion"], "fb_pixel": ["1"]}],
    ))
    assert client.calls == [
        ("get", "act_123", ["currency"]),
        ("create", "act_123", "ads", {
            "name": "Ad", "adset_id": "adset_123", "creative": {"creative_id": "crt_123"},
            "status": "PAUSED", "bid_amount": 1234,
            "tracking_specs": [{"action.type": ["offsite_conversion"], "fb_pixel": ["1"]}],
        }),
    ]
    assert result == {
        "ok": True, "action": "create_ad", "target": {"account_id": "act_123", "adset_id": "adset_123"},
        "validation_only": False, "created": {"id": "ad_created"},
    }


# Round three: named ad schedule times and labels on updates.


def test_create_ad_params_only_schedule_call_is_unchanged(monkeypatch) -> None:
    """Ad schedule times inside params are sent exactly as before they had named parameters."""
    client = RecordingAdsClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    params = {"ad_schedule_start_time": "2026-12-01T00:00:00", "ad_schedule_end_time": "1796000000"}
    result = asyncio.run(ads.create_ad(**AD_BASE, creative_id="crt_123", params=dict(params)))
    assert client.calls == [
        ("create", "act_123", "ads", {
            "name": "Ad", "adset_id": "adset_123", "creative": {"creative_id": "crt_123"},
            "status": "PAUSED", **params,
        }),
    ]
    assert result["created"] == {"id": "ad_created"}


def test_update_ad_params_only_adlabels_call_is_unchanged(monkeypatch) -> None:
    client = FakeUpdateAdClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    asyncio.run(ads.update_ad(ad_id="ad_1", params={"adlabels": [{"name": "Q4"}]}))
    assert client.calls == [
        ("get", "ad_1", ["id", "adlabels"]),
        ("update", "ad_1", {"adlabels": [{"name": "Q4"}]}),
    ]


def test_create_ad_named_schedule_times_are_sent(monkeypatch) -> None:
    client = RecordingAdsClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    asyncio.run(ads.create_ad(
        **AD_BASE, creative_spec=INLINE_SPEC,
        ad_schedule_start_time="2026-12-01T09:00:00-05:00", ad_schedule_end_time="1797000000",
    ))
    assert client.calls == [
        ("create", "act_123", "ads", {
            "name": "Ad", "adset_id": "adset_123", "creative": INLINE_SPEC, "status": "PAUSED",
            "ad_schedule_start_time": "2026-12-01T09:00:00-05:00", "ad_schedule_end_time": "1797000000",
        }),
    ]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"ad_schedule_start_time": "2026-12-01T09:00:00"}, "UTC offset"),
        ({"ad_schedule_end_time": "next week"}, "ad_schedule_end_time"),
        ({"ad_schedule_end_time": "0"}, "positive"),
        ({"ad_schedule_start_time": "2026-12-02T00:00:00Z", "ad_schedule_end_time": "2026-12-01T00:00:00Z"},
         "ad_schedule_end_time must be after ad_schedule_start_time"),
        ({"ad_schedule_start_time": "1796000000", "params": {"ad_schedule_start_time": "1796000000"}},
         "ad_schedule_start_time was given both"),
        ({"ad_schedule_end_time": "1796000000", "params": {"ad_schedule_end_time": "1796000000"}},
         "ad_schedule_end_time was given both"),
    ],
)
def test_create_ad_schedule_time_errors_make_no_call(monkeypatch, arguments, message) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(ads.ValidationError, match=message):
        asyncio.run(ads.create_ad(**AD_BASE, creative_id="crt_123", **arguments))


def test_update_ad_named_adlabels_reads_first_and_returns_previous(monkeypatch) -> None:
    class LabelledClient(FakeUpdateAdClient):
        async def get_object(self, object_id, *, fields=None, params=None):
            stored = await super().get_object(object_id, fields=fields, params=params)
            return {**stored, "adlabels": {"data": [{"id": 9, "name": "Old"}], "paging": {"next": "more"}}}

    client = LabelledClient()
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: client)
    result = asyncio.run(ads.update_ad(ad_id="ad_1", adlabels=[{"name": "New"}, {"id": "9"}]))
    assert client.calls == [
        ("get", "ad_1", ["id", "adlabels"]),
        ("update", "ad_1", {"adlabels": [{"name": "New"}, {"id": "9"}]}),
    ]
    assert result["previous"] == {"adlabels": [{"id": "9", "name": "Old"}]}
    assert result["current"] == {"adlabels": [{"name": "New"}, {"id": "9"}]}
    assert "first page only" in result["previous_incomplete"]["adlabels"]
    assert "replaces the ad's whole label set" in ads.update_ad.__doc__


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"adlabels": [{"name": "A"}], "params": {"adlabels": [{"name": "A"}]}}, "adlabels was given both"),
        ({"adlabels": ["Q4"]}, "adlabels"),
        ({"adlabels": []}, "adlabels must not be empty"),
    ],
)
def test_update_ad_adlabels_errors_make_no_call(monkeypatch, arguments, message) -> None:
    monkeypatch.setattr(ads, "get_graph_api_client", lambda: pytest.fail("No client should be created"))
    with pytest.raises(ads.ValidationError, match=message):
        asyncio.run(ads.update_ad(ad_id="ad_1", **arguments))
