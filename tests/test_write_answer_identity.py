"""Rounds thirty-two and thirty-three (R32 DD1 to DD3, R33 EE1): values in Meta's answer to a write that name the wrong thing.

A copy answer that names an object this call already knows, or whose own list of copied objects
contradicts the copy id, is not renamed; a batch status that
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


# Round thirty-three (review R33, EE1): the answer's own list of copied objects contradicts the
# top-level copy id. Meta's copies references name the types campaign, ad_set, ad and unique_adcreative.
_OWN_TYPE = {'campaign': 'campaign', 'adset': 'ad_set', 'ad': 'ad'}
_CHILD_TYPE = {'campaign': 'ad_set', 'adset': 'ad', 'ad': 'unique_adcreative'}


async def _copy_with_answer(monkeypatch, kind, answer):
    client = _OrdinaryCopy(answer)
    monkeypatch.setattr(campaigns, 'get_graph_api_client', lambda: client)
    monkeypatch.setattr(ads, 'get_graph_api_client', lambda: client)
    if kind == 'campaign':
        result = await campaigns.duplicate_campaign(campaign_id='950001', deep_copy=True, new_name='QA copy')
    elif kind == 'adset':
        result = await campaigns.duplicate_adset(adset_id='950001', deep_copy=True, new_name='QA copy')
    else:
        result = await ads.duplicate_ad(ad_id='950001', new_name='QA copy')
    return client, result


@pytest.mark.parametrize('kind,child_kind', [('campaign', 'ad_set'), ('adset', 'ad'), ('ad', 'unique_adcreative')])
@pytest.mark.parametrize('mapping_case', ['wrong_type', 'different_copy_for_source'])
@pytest.mark.asyncio
async def test_ee1_copy_does_not_rename_an_explicitly_identified_different_object(monkeypatch, kind, child_kind, mapping_case):
    mapping = (
        {'ad_object_type': child_kind, 'source_id': '950004', 'copied_id': '950003'}
        if mapping_case == 'wrong_type' else
        {'ad_object_type': _OWN_TYPE[kind], 'source_id': '950001', 'copied_id': '950002'}
    )
    client, result = await _copy_with_answer(
        monkeypatch, kind, {f'copied_{kind}_id': '950003', 'ad_object_ids': [mapping]})
    assert not [c for c in client.calls if c[0] == 'rename'], result
    assert result['new_id'] is None and result['new_ids'] == {f'{kind}_id': None}, result
    assert result['rename']['applied'] is False, result
    assert 'disagrees with itself' in result['warning'], result


@pytest.mark.parametrize('kind', ['campaign', 'adset', 'ad'])
@pytest.mark.parametrize('rows', [
    [],
    None,
    ['not-a-row', 7],
    [{'ad_object_type': 'something_new', 'source_id': '950004', 'copied_id': '950003'}],
    [{'source_id': '950001', 'copied_id': '950002'}],
    [{'ad_object_type': None, 'source_id': '950004', 'copied_id': '950003'}],
    [{'ad_object_type': 'OWN', 'source_id': '950001', 'copied_id': '950003'}],
    [{'ad_object_type': 'OWN', 'source_id': 950001, 'copied_id': 950003}],
    [{'ad_object_type': ' OWN ', 'source_id': ' 950001 ', 'copied_id': ' 950003 '}],
    [{'ad_object_type': 'OWN', 'source_id': '950001'}],
    [{'ad_object_type': 'OWN', 'source_id': '950001', 'copied_id': None}],
    [{'ad_object_type': 'OWN', 'source_id': '950001', 'copied_id': 'pending'}],
    [{'ad_object_type': 'OWN', 'source_id': None, 'copied_id': '950002'}],
    [{'ad_object_type': 'CHILD', 'source_id': '950004', 'copied_id': '950005'}],
    [{'ad_object_type': 'CHILD', 'source_id': '950004'}],
], ids=[
    'empty', 'absent', 'unreadable-rows', 'unknown-type', 'no-type', 'null-type', 'own-row',
    'own-row-numbers', 'own-row-padded', 'own-row-no-copy', 'own-row-null-copy', 'own-row-unusual-copy',
    'own-row-no-source', 'child-row', 'child-row-no-copy',
])
@pytest.mark.asyncio
async def test_a_copy_list_that_does_not_contradict_the_copy_id_still_renames(monkeypatch, kind, rows):
    """Control: a missing mapping, an unreadable row or an unknown type is not a contradiction."""
    def fill(row):
        if not isinstance(row, dict) or not isinstance(row.get('ad_object_type'), str):
            return row
        named = row['ad_object_type'].replace('OWN', _OWN_TYPE[kind]).replace('CHILD', _CHILD_TYPE[kind])
        return {**row, 'ad_object_type': named}

    answer = {f'copied_{kind}_id': '950003'}
    if rows is not None:
        answer['ad_object_ids'] = [fill(row) for row in rows]
    client, result = await _copy_with_answer(monkeypatch, kind, answer)
    assert client.calls[-1] == ('rename', '950003', {'name': 'QA copy'}), result
    assert result['new_id'] == '950003' and result['rename']['applied'] is True, result
    assert 'warning' not in result, result


@pytest.mark.parametrize('top_level', ['950003', 950003], ids=['text', 'number'])
@pytest.mark.asyncio
async def test_copy_ids_in_another_form_are_compared_by_value(monkeypatch, top_level):
    """Control: the same copy id as a number is not a different copy; the same child id as a number still is."""
    ordinary = {'copied_campaign_id': top_level, 'ad_object_ids': [
        {'ad_object_type': 'campaign', 'source_id': 950001, 'copied_id': 950003},
        {'ad_object_type': 'ad_set', 'source_id': '950004', 'copied_id': '950005'},
    ]}
    client, result = await _copy_with_answer(monkeypatch, 'campaign', ordinary)
    assert client.calls[-1] == ('rename', '950003', {'name': 'QA copy'}), result
    assert result['rename']['applied'] is True and 'warning' not in result, result
    contradicted = {'copied_campaign_id': top_level, 'ad_object_ids': [
        {'ad_object_type': 'ad_set', 'source_id': '950004', 'copied_id': 950003},
    ]}
    client, result = await _copy_with_answer(monkeypatch, 'campaign', contradicted)
    assert not [c for c in client.calls if c[0] == 'rename'] and result['new_id'] is None, result


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
