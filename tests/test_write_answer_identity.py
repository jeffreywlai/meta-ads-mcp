"""Round thirty-two (review R32, DD1 to DD3): values in Meta's answer to a write that name the wrong thing.

A copy answer that names an object this call already knows is not renamed; a batch status that
names another batch does not confirm this one; an uncertain delete is not logged as not deleted.
Ordinary answers are read exactly as before (controls at the end).
"""
import asyncio
from copy import deepcopy

import pytest

from meta_ads_mcp.errors import MetaAdsError
from meta_ads_mcp.tools import ads, campaigns, catalogs


class CopyNamesParent:
    def __init__(self, kind, parent):
        self.kind = kind
        self.parent = parent
        self.calls = []

    async def create_edge_object(self, parent_id, edge, *, data, files=None):
        self.calls.append(('copy', parent_id, edge, deepcopy(data)))
        return {f'copied_{self.kind}_id': self.parent}

    async def update_object(self, object_id, *, data):
        self.calls.append(('rename', object_id, deepcopy(data)))
        return {'success': True}


@pytest.mark.parametrize('kind,parent_key', [('adset', 'campaign_id'), ('ad', 'adset_id')])
def test_dd1_copy_never_renames_its_destination_parent(monkeypatch, kind, parent_key):
    parent = '920002'
    client = CopyNamesParent(kind, parent)
    module = ads if kind == 'ad' else campaigns
    monkeypatch.setattr(module, 'get_graph_api_client', lambda: client)
    result = None
    try:
        result = asyncio.run(getattr(module, f'duplicate_{kind}')(
            **{f'{kind}_id': '920001', parent_key: parent, 'new_name': 'QA duplicate'}))
    except MetaAdsError:
        pass
    assert not [c for c in client.calls if c[0] == 'rename' and c[1] == parent], result
    if result is not None:
        assert result.get('rename', {}).get('applied') is not True, result


@pytest.mark.parametrize('kind,child_type', [('campaign', 'ad_set'), ('adset', 'ad')])
def test_dd1_copy_never_renames_an_original_child(monkeypatch, kind, child_type):
    source, original_child, copied_child = '921001', '921002', '921003'

    class CopyNamesChild(CopyNamesParent):
        async def create_edge_object(self, parent_id, edge, *, data, files=None):
            self.calls.append(('copy', parent_id, edge, deepcopy(data)))
            return {f'copied_{kind}_id': original_child,
                    'ad_object_ids': [{'ad_object_type': child_type,
                                       'source_id': original_child, 'copied_id': copied_child}]}

    client = CopyNamesChild(kind, original_child)
    monkeypatch.setattr(campaigns, 'get_graph_api_client', lambda: client)
    result = None
    try:
        result = asyncio.run(getattr(campaigns, f'duplicate_{kind}')(
            **{f'{kind}_id': source, 'deep_copy': True, 'new_name': 'QA duplicate'}))
    except MetaAdsError:
        pass
    assert not [c for c in client.calls if c[0] == 'rename' and c[1] == original_child], result
    if result is not None:
        assert result.get('rename', {}).get('applied') is not True, result


class WrongBatchHandle:
    def __init__(self):
        self.calls = []

    async def list_objects(self, parent_id, edge, *, fields=None, params=None):
        self.calls.append(('read', parent_id, edge, deepcopy(params)))
        if edge == 'products':
            return {'data': [{'id': '930001', 'retailer_id': 'QA-SKU', 'name': 'Before'}]}
        assert edge == 'check_batch_request_status'
        return {'data': [{'handle': 'different-batch', 'status': 'finished',
                          'errors_total_count': 0, 'ids_of_invalid_requests': [], 'errors': []}]}

    async def create_edge_object(self, parent_id, edge, *, data, files=None):
        self.calls.append(('write', parent_id, edge, deepcopy(data)))
        return {'handles': ['requested-batch']}


async def no_pause(_seconds):
    return None


@pytest.mark.parametrize('status_only', [False, True], ids=['submit-and-poll', 'handle-only'])
def test_dd2_another_handle_cannot_verify_this_batch(monkeypatch, status_only):
    client = WrongBatchHandle()
    monkeypatch.setattr(catalogs, 'get_graph_api_client', lambda: client)
    monkeypatch.setattr(catalogs, '_pause', no_pause)
    kwargs = {'handle': 'requested-batch'} if status_only else {
        'items': [{'method': 'update', 'retailer_id': 'QA-SKU', 'name': 'After'}], 'wait_seconds': 2}
    result = asyncio.run(asyncio.wait_for(catalogs.batch_update_catalog_products(
        catalog_id='930000', **kwargs), timeout=3))
    assert result['ok'] is False, result
    if status_only:
        assert result['outcome'] != 'all_applied', result
    else:
        assert result['items'][0]['outcome'] != 'applied', result


@pytest.mark.parametrize('body', [
    {'success': True, 'error': {'code': 100, 'error_subcode': 33,
                             'message': 'Accesssing Deprecated Platform API Endpoint'}},
    {'error': {'code': 100, 'error_subcode': 33, 'is_transient': True,
               'message': 'Accesssing Deprecated Platform API Endpoint'}},
], ids=['contradictory-success', 'transient-refusal'])
def test_dd3_retired_rule_error_keeps_unknown_delete_outcome(monkeypatch, body, caplog):
    import httpx
    import json
    from fastmcp.exceptions import ToolError
    from meta_ads_mcp.config import Settings
    from meta_ads_mcp.coordinator import mcp_server
    from meta_ads_mcp.graph_api import GraphAPIClient
    from meta_ads_mcp.tools import catalog_feeds

    class Transport:
        async def request(self, *, method, url, **kwargs):
            value = {'id': '940001', 'rule_type': 'mapping_rule', 'params': {'map_from': 'title'}}
            if method == 'DELETE':
                value = body
            return httpx.Response(400 if method == 'DELETE' else 200, json=value,
                                  request=httpx.Request(method, url))

    transport = Transport()
    monkeypatch.setattr(GraphAPIClient, '_get_shared_client', lambda self, **kw: transport)
    client = GraphAPIClient(Settings(access_token='test-token', api_version='v26.0', default_account_id=None,
        app_id=None, app_secret=None, redirect_uri=None, log_level='INFO', host='127.0.0.1',
        port=8000, request_timeout=1, max_retries=0, read_only=False))
    monkeypatch.setattr(catalog_feeds, 'get_graph_api_client', lambda: client)
    with pytest.raises(ToolError) as raised:
        asyncio.run(asyncio.wait_for(mcp_server.call_tool(
            'delete_feed_rule', {'rule_id': '940001'}), timeout=3))
    public = json.loads(str(raised.value))['error']
    # The public MCP boundary already preserves uncertainty through the cause chain.
    assert public['mutation_outcome_unknown'] is True, public
    assert 'so the rule was not deleted' not in str(raised.value), public
    # The default ERROR log still prints the translated top-level failure as certain.
    assert 'so the rule was not deleted' not in caplog.text, caplog.text


class _OrdinaryCopy:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def create_edge_object(self, parent_id, edge, *, data, files=None):
        self.calls.append(("copy", parent_id, edge, deepcopy(data)))
        return deepcopy(self.answer)

    async def update_object(self, object_id, *, data):
        self.calls.append(("rename", object_id, deepcopy(data)))
        return {"success": True}


def test_an_ordinary_adset_copy_into_another_campaign_is_still_renamed(monkeypatch):
    client = _OrdinaryCopy({"copied_adset_id": "920009", "ad_object_ids": [
        {"ad_object_type": "ad_set", "source_id": "920001", "copied_id": "920009"},
    ]})
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    result = asyncio.run(campaigns.duplicate_adset(adset_id="920001", campaign_id="920002", new_name="QA duplicate"))
    assert client.calls[-1] == ("rename", "920009", {"name": "QA duplicate"})
    assert result["new_id"] == "920009" and result["rename"]["applied"] is True and "warning" not in result


def test_an_ordinary_deep_campaign_copy_is_still_renamed(monkeypatch):
    client = _OrdinaryCopy({"copied_campaign_id": "921009", "ad_object_ids": [
        {"ad_object_type": "campaign", "source_id": "921001", "copied_id": "921009"},
        {"ad_object_type": "ad_set", "source_id": "921002", "copied_id": "921003"},
    ]})
    monkeypatch.setattr(campaigns, "get_graph_api_client", lambda: client)
    result = asyncio.run(campaigns.duplicate_campaign(campaign_id="921001", deep_copy=True, new_name="QA duplicate"))
    assert client.calls[-1] == ("rename", "921009", {"name": "QA duplicate"})
    assert result["rename"]["applied"] is True


@pytest.mark.parametrize("row_handle", ["requested-batch", " requested-batch ", None, "", 7], ids=["same", "padded", "absent", "blank", "not-text"])
def test_a_status_entry_for_this_batch_or_with_no_handle_is_read_as_before(monkeypatch, row_handle):
    class Client(WrongBatchHandle):
        async def list_objects(self, parent_id, edge, *, fields=None, params=None):
            answer = await super().list_objects(parent_id, edge, fields=fields, params=params)
            if edge == "check_batch_request_status":
                if row_handle is None:
                    answer["data"][0].pop("handle")
                else:
                    answer["data"][0]["handle"] = row_handle
            return answer

    monkeypatch.setattr(catalogs, "get_graph_api_client", lambda: Client())
    result = asyncio.run(catalogs.batch_update_catalog_products(catalog_id="930000", handle="requested-batch"))
    assert result["ok"] is True and result["outcome"] == "all_applied"


def test_a_plain_retired_endpoint_refusal_still_says_the_rule_was_not_deleted(monkeypatch):
    from meta_ads_mcp.errors import MetaApiError, UnsupportedFeatureError
    from meta_ads_mcp.tools import catalog_feeds

    class Client:
        async def get_object(self, object_id, *, fields=None, params=None):
            return {"id": object_id, "rule_type": "mapping_rule"}

        async def delete_object(self, object_id):
            raise MetaApiError(message="Accesssing Deprecated Platform API Endpoint", status_code=400, code=100)

    monkeypatch.setattr(catalog_feeds, "get_graph_api_client", lambda: Client())
    with pytest.raises(UnsupportedFeatureError, match="was not deleted"):
        asyncio.run(catalog_feeds.delete_feed_rule(rule_id="940001"))
