"""Env-gated live integration tests for real Meta account scenarios."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from meta_ads_mcp.config import reload_settings
from meta_ads_mcp.errors import RateLimitError
from meta_ads_mcp.graph_api import get_graph_api_client, normalize_account_id
from meta_ads_mcp.tools import (
    ads,
    audiences,
    auth_tools,
    campaigns,
    creatives,
    diagnostics,
    discovery,
    execution,
    insights,
    recommendations,
    research,
    targeting,
)


pytestmark = pytest.mark.skipif(
    os.getenv("META_RUN_LIVE_TESTS") != "1",
    reason="Set META_RUN_LIVE_TESTS=1 and the required META_LIVE_* env vars to run live Meta tests.",
)

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc`\x00\x00\x00\x02\x00\x01\xe2!\xbc3"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.skip(f"{name} is not configured for live integration tests.")
    return value


def _optional_env(name: str) -> str | None:
    return os.getenv(name) or None


def _csv_env(name: str) -> list[str]:
    value = _env(name)
    return [part.strip() for part in value.split(",") if part.strip()]


def _run(awaitable):
    try:
        return asyncio.run(awaitable)
    except RateLimitError as exc:
        pytest.skip(f"Meta rate-limited this live test: {exc}")


def _use_token(monkeypatch: pytest.MonkeyPatch, env_name: str, *, default_account_id: str | None = None) -> None:
    monkeypatch.setenv("META_ACCESS_TOKEN", _env(env_name))
    if default_account_id is not None:
        monkeypatch.setenv("META_DEFAULT_ACCOUNT_ID", default_account_id)
    reload_settings()


async def _discover_active_context(
    account_id: str,
    *,
    include_adsets: bool = False,
    include_ads: bool = False,
    include_creatives: bool = False,
) -> dict[str, str]:
    context: dict[str, str] = {"account_id": account_id}

    campaigns_payload = await discovery.list_campaigns(account_id=account_id, limit=5)
    if campaigns_payload["items"]:
        context["campaign_id"] = campaigns_payload["items"][0]["id"]

    if include_adsets:
        adsets_payload = (
            await discovery.list_adsets(campaign_id=context["campaign_id"], limit=5)
            if "campaign_id" in context
            else await discovery.list_adsets(account_id=account_id, limit=5)
        )
        if adsets_payload["items"]:
            context["adset_id"] = adsets_payload["items"][0]["id"]

    if include_ads:
        ads_payload = await discovery.list_ads(account_id=account_id, limit=5)
        if ads_payload["items"]:
            context["ad_id"] = ads_payload["items"][0]["id"]

    if include_creatives:
        creatives_payload = await creatives.list_creatives(account_id=account_id, limit=5)
        if creatives_payload["items"]:
            context["creative_id"] = creatives_payload["items"][0]["id"]

    return context


def _extract_image_hash(upload_result: dict[str, Any]) -> str | None:
    uploaded = upload_result.get("uploaded") or {}
    images = uploaded.get("images") or {}
    for image in images.values():
        if isinstance(image, dict) and image.get("hash"):
            return str(image["hash"])
    return None


def test_live_read_only_account_with_no_spend_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_READ")
    account_id = _env("META_LIVE_NO_SPEND_ACCOUNT_ID")
    result = _run(diagnostics.get_account_optimization_snapshot(account_id=account_id))
    assert result["scope"]["object_id"] == normalize_account_id(account_id)


def test_live_active_spend_account_insights(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_READ")
    account_id = _env("META_LIVE_ACTIVE_ACCOUNT_ID")
    result = _run(insights.get_entity_insights(level="account", object_id=account_id, date_preset="last_7d"))
    assert result["summary"]["count"] >= 0


def test_live_active_spend_account_insights_accept_since_until_without_date_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_READ")
    account_id = _env("META_LIVE_ACTIVE_ACCOUNT_ID")
    result = _run(
        insights.get_entity_insights(
            level="account",
            object_id=account_id,
            since="2026-03-01",
            until="2026-03-07",
        )
    )
    assert result["summary"]["count"] >= 0


def test_live_invalid_date_preset_returns_useful_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_READ")
    account_id = _env("META_LIVE_ACTIVE_ACCOUNT_ID")
    with pytest.raises(Exception) as excinfo:
        _run(
            insights.get_entity_insights(
                level="account",
                object_id=account_id,
                date_preset="__not_a_real_preset__",
            )
        )
    assert str(excinfo.value)


def test_live_ads_read_token_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_READ")
    info = _run(auth_tools.validate_token())
    assert "ads_read" in (info.get("scopes") or [])


def test_live_ads_management_token_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_WRITE")
    info = _run(auth_tools.validate_token())
    assert "ads_management" in (info.get("scopes") or [])


def test_live_account_pages_and_instagram_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_READ")
    account_id = _env("META_LIVE_ACTIVE_ACCOUNT_ID")
    pages = _run(discovery.get_account_pages(account_id=account_id))
    instagram_accounts = _run(discovery.list_instagram_accounts(account_id=account_id))
    assert pages["summary"]["count"] >= 0
    assert instagram_accounts["summary"]["count"] >= 0


def test_live_recommendations_unsupported_account(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_READ")
    account_id = _env("META_LIVE_UNSUPPORTED_RECOMMENDATIONS_ACCOUNT_ID")
    result = _run(recommendations.get_recommendations(account_id=account_id))
    assert "supported" in result


def test_live_active_account_pagination_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_READ")
    account_id = _env("META_LIVE_ACTIVE_ACCOUNT_ID")
    context = _run(_discover_active_context(account_id))

    campaigns_page_1 = _run(discovery.list_campaigns(account_id=account_id, limit=1))
    assert campaigns_page_1["summary"]["count"] <= 1
    if campaigns_page_1["paging"]["after"]:
        campaigns_page_2 = _run(
            discovery.list_campaigns(account_id=account_id, limit=1, after=campaigns_page_1["paging"]["after"])
        )
        assert campaigns_page_2["summary"]["count"] >= 0

    adsets_page_1 = _run(discovery.list_adsets(account_id=account_id, limit=1))
    assert adsets_page_1["summary"]["count"] <= 1
    if adsets_page_1["paging"]["after"]:
        adsets_page_2 = _run(
            discovery.list_adsets(account_id=account_id, limit=1, after=adsets_page_1["paging"]["after"])
        )
        assert adsets_page_2["summary"]["count"] >= 0

    ads_page_1 = _run(discovery.list_ads(campaign_id=context["campaign_id"], limit=1))
    assert ads_page_1["summary"]["count"] <= 1
    if ads_page_1["paging"]["after"]:
        ads_page_2 = _run(
            discovery.list_ads(campaign_id=context["campaign_id"], limit=1, after=ads_page_1["paging"]["after"])
        )
        assert ads_page_2["summary"]["count"] >= 0

    creatives_page_1 = _run(creatives.list_creatives(account_id=account_id, limit=1))
    assert creatives_page_1["summary"]["count"] <= 1
    if creatives_page_1["paging"]["after"]:
        creatives_page_2 = _run(
            creatives.list_creatives(account_id=account_id, limit=1, after=creatives_page_1["paging"]["after"])
        )
        assert creatives_page_2["summary"]["count"] >= 0


def test_live_async_reporting_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_READ")
    account_id = _env("META_LIVE_ACTIVE_ACCOUNT_ID")
    context = _run(_discover_active_context(account_id))
    created = _run(
        insights.create_async_insights_report(
            level="campaign",
            object_id=context["campaign_id"],
            fields=["campaign_id", "campaign_name", "spend", "impressions", "clicks"],
            date_preset="last_7d",
        )
    )
    report_run_id = created["report_run_id"]
    last_result: dict[str, Any] | None = None
    last_error: Exception | None = None
    for _ in range(6):
        try:
            last_result = _run(
                insights.get_async_insights_report(
                    report_run_id=report_run_id,
                    fields=created["requested_fields"],
                )
            )
            last_error = None
            if last_result.get("status", {}).get("async_status") in {"Job Completed", "COMPLETED"}:
                break
        except Exception as exc:  # pragma: no cover - live race handling
            last_error = exc
        _run(asyncio.sleep(1))
    if last_result is None and last_error is not None:
        raise last_error
    assert last_result is not None
    assert "status" in last_result
    if last_result["status"].get("async_status") in {"Job Completed", "COMPLETED"}:
        assert "rows" in last_result


def test_live_targeting_and_opportunity_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_READ")
    account_id = _env("META_LIVE_ACTIVE_ACCOUNT_ID")
    context = _run(_discover_active_context(account_id))

    categories = _run(targeting.get_targeting_categories(category_class="life_events", account_id=account_id, limit=10))
    behaviors = _run(targeting.search_behaviors(query="Engaged", account_id=account_id, limit=10))
    demographics = _run(targeting.search_demographics(query="Parents", account_id=account_id, limit=10))
    audience = _run(
        targeting.estimate_audience_size(
            account_id=account_id,
            targeting_spec={"geo_locations": {"countries": ["US"]}, "age_min": 25, "age_max": 54},
        )
    )
    all_recs = _run(recommendations.get_recommendations(account_id=account_id, campaign_id=context["campaign_id"]))
    budget_recs = _run(recommendations.get_budget_opportunities(account_id=account_id, campaign_id=context["campaign_id"]))

    assert categories["summary"]["count"] >= 0
    assert behaviors["summary"]["count"] >= 0
    assert demographics["summary"]["count"] >= 0
    assert audience["summary"]["count"] == 1
    assert "supported" in all_recs
    if all_recs["supported"]:
        total = all_recs["summary"]["count"]
        assert budget_recs["summary"]["filtered_from_total"] == total


def test_live_creative_inspection_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_READ")
    account_id = _env("META_LIVE_ACTIVE_ACCOUNT_ID")
    context = _run(_discover_active_context(account_id, include_ads=True, include_creatives=True))
    creative_list = _run(creatives.list_creatives(account_id=account_id, limit=5))
    ad_image = _run(ads.get_ad_image(ad_id=context["ad_id"]))
    preview = _run(creatives.preview_ad(ad_id=context["ad_id"]))
    assert creative_list["summary"]["count"] >= 0
    assert ad_image["summary"]["count"] == 1
    assert preview["summary"]["count"] == 1


def test_live_gated_reach_frequency_predictions(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_WRITE")
    account_id = _env("META_LIVE_ACTIVE_ACCOUNT_ID")
    result = _run(targeting.get_reach_frequency_predictions(account_id=account_id, limit=5))
    assert result["summary"]["count"] >= 0


def test_live_ads_archive_with_authorized_app(monkeypatch: pytest.MonkeyPatch) -> None:
    search_terms = _optional_env("META_LIVE_ADS_LIBRARY_SEARCH_TERMS")
    countries = _optional_env("META_LIVE_ADS_LIBRARY_COUNTRIES")
    if not search_terms or not countries:
        pytest.skip("META_LIVE_ADS_LIBRARY_SEARCH_TERMS and META_LIVE_ADS_LIBRARY_COUNTRIES are required.")
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_READ")
    result = _run(
        research.search_ads_archive(
            search_terms=search_terms,
            ad_reached_countries=[part.strip() for part in countries.split(",") if part.strip()],
            ad_type=os.getenv("META_LIVE_ADS_LIBRARY_AD_TYPE", "ALL"),
            limit=5,
        )
    )
    assert result["summary"]["count"] >= 0


def test_live_campaign_write_workflow_reversible(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_WRITE")
    account_id = _env("META_LIVE_WRITE_ACCOUNT_ID")
    created_id: str | None = None
    try:
        created = _run(
            campaigns.create_campaign(
                account_id=account_id,
                name="Codex Live Campaign",
                objective="OUTCOME_SALES",
                status="PAUSED",
                daily_budget=10.0,
                bid_strategy="LOWEST_COST_WITHOUT_CAP",
            )
        )
        created_id = str((created.get("created") or {}).get("id"))
        assert created_id

        updated = _run(
            campaigns.update_campaign(
                campaign_id=created_id,
                name="Codex Live Campaign Updated",
                daily_budget=11.0,
            )
        )
        assert updated["current"]["name"] == "Codex Live Campaign Updated"
        _run(execution.set_campaign_status(campaign_id=created_id, status="PAUSED"))
        _run(execution.update_campaign_budget(campaign_id=created_id, daily_budget=12.0))
        _run(
            execution.update_campaign_bid_strategy(
                campaign_id=created_id,
                bid_strategy="LOWEST_COST_WITHOUT_CAP",
            )
        )
    finally:
        if created_id:
            _run(campaigns.delete_campaign(campaign_id=created_id))


def test_live_adset_execution_reversible(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_WRITE")
    adset_id = _env("META_LIVE_WRITE_ADSET_ID")
    client = get_graph_api_client()
    previous = _run(
        client.get_object(
            adset_id,
            fields=["id", "status", "effective_status", "bid_amount", "bid_strategy", "currency"],
        )
    )
    previous_status = previous.get("status") or previous.get("effective_status") or "PAUSED"
    previous_bid_amount = previous.get("bid_amount")
    previous_bid_strategy = previous.get("bid_strategy")
    currency = previous.get("currency")

    try:
        next_status = "ACTIVE" if previous_status == "PAUSED" else "PAUSED"
        _run(execution.set_adset_status(adset_id=adset_id, status=next_status))
        if previous_bid_amount is not None:
            human_bid = float(previous_bid_amount if currency and currency.upper() == "JPY" else float(previous_bid_amount) / 100.0)
            _run(execution.update_adset_bid_amount(adset_id=adset_id, bid_amount=max(human_bid + 1.0, 1.0)))
        if previous_bid_strategy:
            _run(
                execution.update_adset_bid_strategy(
                    adset_id=adset_id,
                    bid_strategy=str(previous_bid_strategy),
                    bid_amount=(
                        float(previous_bid_amount if currency and currency.upper() == "JPY" else float(previous_bid_amount) / 100.0)
                        if previous_bid_amount is not None
                        else None
                    ),
                )
            )
    finally:
        restore_payload: dict[str, Any] = {"status": previous_status}
        if previous_bid_amount is not None:
            restore_payload["bid_amount"] = previous_bid_amount
        if previous_bid_strategy:
            restore_payload["bid_strategy"] = previous_bid_strategy
        _run(client.update_object(adset_id, data=restore_payload))


def test_live_audience_workflow_reversible(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_WRITE")
    account_id = _env("META_LIVE_WRITE_ACCOUNT_ID")
    audience_id: str | None = None
    lookalike_id: str | None = None
    try:
        created = _run(
            audiences.create_custom_audience(
                account_id=account_id,
                name="Codex Live Audience",
                customer_file_source="USER_PROVIDED_ONLY",
                description="temporary codex audience",
            )
        )
        audience_id = str((created.get("created") or {}).get("id"))
        assert audience_id
        updated = _run(
            audiences.update_custom_audience(
                audience_id=audience_id,
                name="Codex Live Audience Updated",
                description="updated temporary codex audience",
            )
        )
        assert updated["current"]["name"] == "Codex Live Audience Updated"

        origin_audience_id = _optional_env("META_LIVE_LOOKALIKE_SEED_AUDIENCE_ID")
        if origin_audience_id:
            lookalike = _run(
                audiences.create_lookalike_audience(
                    account_id=account_id,
                    name="Codex Live Lookalike",
                    origin_audience_id=origin_audience_id,
                    country="US",
                    ratio=0.01,
                )
            )
            lookalike_id = str((lookalike.get("created") or {}).get("id"))
    finally:
        if lookalike_id:
            _run(audiences.delete_audience(audience_id=lookalike_id))
        if audience_id:
            _run(audiences.delete_audience(audience_id=audience_id))


def test_live_creative_workflow_reversible(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_WRITE")
    account_id = _env("META_LIVE_WRITE_ACCOUNT_ID")
    page_id = _env("META_LIVE_PAGE_ID")
    image_path = tmp_path / "codex-live.png"
    image_path.write_bytes(_TINY_PNG)

    creative_id: str | None = None
    upload = _run(creatives.upload_creative_asset(account_id=account_id, file_path=str(image_path)))
    image_hash = _extract_image_hash(upload)
    if not image_hash:
        pytest.skip("Meta did not return an image hash for the uploaded live asset.")

    try:
        created = _run(
            creatives.create_ad_creative(
                account_id=account_id,
                name="Codex Live Creative",
                object_story_spec={
                    "page_id": page_id,
                    "link_data": {
                        "link": os.getenv("META_LIVE_CREATIVE_LINK", "https://example.com"),
                        "message": "Codex live creative test",
                        "image_hash": image_hash,
                    },
                },
            )
        )
        creative_id = str((created.get("created") or {}).get("id"))
        assert creative_id
        preview = _run(
            creatives.preview_ad(
                account_id=account_id,
                creative_id=creative_id,
            )
        )
        assert preview["summary"]["count"] == 1
    finally:
        if creative_id:
            _run(creatives.delete_creative(creative_id=creative_id))


def test_live_create_ad_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.getenv("META_LIVE_ALLOW_AD_CREATE") != "1":
        pytest.skip("Set META_LIVE_ALLOW_AD_CREATE=1 to run residual ad-creation testing.")
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_WRITE")
    account_id = _env("META_LIVE_WRITE_ACCOUNT_ID")
    adset_id = _env("META_LIVE_CREATE_AD_ADSET_ID")
    creative_id = _env("META_LIVE_CREATE_AD_CREATIVE_ID")
    result = _run(
        ads.create_ad(
            account_id=account_id,
            name="Codex Live Ad",
            adset_id=adset_id,
            creative_id=creative_id,
            status="PAUSED",
        )
    )
    assert result["ok"] is True


def test_live_refresh_to_long_lived_token(monkeypatch: pytest.MonkeyPatch) -> None:
    short_lived_token = _optional_env("META_LIVE_SHORT_LIVED_TOKEN")
    if not short_lived_token:
        pytest.skip("META_LIVE_SHORT_LIVED_TOKEN is required.")
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_READ")
    result = _run(
        auth_tools.refresh_to_long_lived_token(
            access_token=short_lived_token,
            app_id=_env("META_LIVE_APP_ID"),
            app_secret=_env("META_LIVE_APP_SECRET"),
        )
    )
    assert "access_token" in result


def test_live_exchange_code_for_token(monkeypatch: pytest.MonkeyPatch) -> None:
    oauth_code = _optional_env("META_LIVE_OAUTH_CODE")
    if not oauth_code:
        pytest.skip("META_LIVE_OAUTH_CODE is required.")
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_READ")
    result = _run(
        auth_tools.exchange_code_for_token(
            code=oauth_code,
            redirect_uri=_env("META_LIVE_REDIRECT_URI"),
            app_id=_env("META_LIVE_APP_ID"),
            app_secret=_env("META_LIVE_APP_SECRET"),
        )
    )
    assert "access_token" in result


def test_live_generate_system_user_token(monkeypatch: pytest.MonkeyPatch) -> None:
    system_user_id = _optional_env("META_LIVE_SYSTEM_USER_ID")
    if not system_user_id:
        pytest.skip("META_LIVE_SYSTEM_USER_ID is required.")
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_WRITE")
    result = _run(
        auth_tools.generate_system_user_token(
            system_user_id=system_user_id,
            scope=_csv_env("META_LIVE_SYSTEM_USER_SCOPES"),
            business_app=_env("META_LIVE_APP_ID"),
        )
    )
    assert result["system_user_id"] == system_user_id


def test_live_zero_decimal_campaign_budget_reversible(monkeypatch: pytest.MonkeyPatch) -> None:
    campaign_id = _optional_env("META_LIVE_ZERO_DECIMAL_CAMPAIGN_ID")
    if not campaign_id:
        pytest.skip("META_LIVE_ZERO_DECIMAL_CAMPAIGN_ID is required.")
    _use_token(monkeypatch, "META_LIVE_ACCESS_TOKEN_WRITE")
    client = get_graph_api_client()
    previous = _run(client.get_object(campaign_id, fields=["daily_budget", "currency"]))
    previous_budget = previous.get("daily_budget")
    if previous_budget is None:
        pytest.skip("The zero-decimal live campaign does not expose daily_budget.")
    try:
        _run(execution.update_campaign_budget(campaign_id=campaign_id, daily_budget=float(previous_budget) + 1.0))
    finally:
        _run(client.update_object(campaign_id, data={"daily_budget": previous_budget}))
