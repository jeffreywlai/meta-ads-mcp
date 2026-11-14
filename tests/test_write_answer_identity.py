"""Rounds thirty-two and thirty-three (R32 DD1 to DD3, R33 EE1): values in Meta's answer to a write that name the wrong thing.

A copy answer that names an object this call already knows, or whose own list of copied objects
contradicts the copy id, is not renamed; a batch status that
names another batch does not confirm this one; an uncertain delete is not logged as not deleted.
Ordinary answers are read exactly as before (controls at the end).
"""
import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from urllib.parse import parse_qs

import httpx
import pytest

from meta_ads_mcp.config import get_settings
from meta_ads_mcp.errors import MetaAdsError
from meta_ads_mcp.graph_api import GraphAPIClient
from meta_ads_mcp.tools import ads, campaigns, catalogs, creative_formats, creatives


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


# Round thirty-four (review R34, FF1): an id in Meta's answer to a create, copy or upload that cannot
# be the new object (a value that is not an id, or an object the call already knows) is never used in a
# follow-up write or read, nor shown as the new object. These run through the real GraphAPIClient over
# an HTTPX MockTransport. Controls for ordinary answers follow each group.


@asynccontextmanager
async def graph(monkeypatch, handler, *modules):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GraphAPIClient(get_settings())
        monkeypatch.setattr(GraphAPIClient, '_get_shared_client', lambda self, **kw: http)
        for module in modules:
            monkeypatch.setattr(module, 'get_graph_api_client', lambda: client)
        yield client


def _copy_tool(kind):
    return getattr(ads if kind == 'ad' else campaigns, f'duplicate_{kind}')


@pytest.mark.parametrize('kind', ['campaign', 'adset', 'ad'])
@pytest.mark.asyncio
async def test_ff1_copy_does_not_rename_an_original_named_with_padding(monkeypatch, kind):
    writes = []

    def handler(request):
        if request.method != 'GET':
            writes.append((request.url.path, parse_qs(request.content.decode())))
        if request.url.path.endswith('/copies'):
            return httpx.Response(200, json={f'copied_{kind}_id': '960002', 'ad_object_ids': [
                {'ad_object_type': 'ad', 'source_id': ' 960002 ', 'copied_id': '960003'}]})
        return httpx.Response(200, json={'success': True})

    async with graph(monkeypatch, handler, campaigns, ads):
        result = await _copy_tool(kind)(**{f'{kind}_id': '960001', 'new_name': 'QA copy'})
    assert len(writes) == 1, (writes, result)
    assert result['new_id'] is None and result['rename']['applied'] is False, result
    assert 'named an existing object' in result['warning'], result


@pytest.mark.parametrize('bad_id', [True, -12, 'not_an_id'])
@pytest.mark.parametrize('kind', ['campaign', 'adset', 'ad'])
@pytest.mark.asyncio
async def test_ff1_copy_does_not_rename_a_non_id(monkeypatch, kind, bad_id):
    writes = []

    def handler(request):
        if request.method != 'GET':
            writes.append(request.url.path)
        if request.url.path.endswith('/copies'):
            return httpx.Response(200, json={f'copied_{kind}_id': bad_id})
        return httpx.Response(200, json={'success': True})

    async with graph(monkeypatch, handler, campaigns, ads):
        result = await _copy_tool(kind)(**{f'{kind}_id': '960001', 'new_name': 'QA copy'})
    assert len(writes) == 1, writes
    assert result['new_id'] is None and result['new_ids'] == {f'{kind}_id': None}, result
    assert result['rename'] == {
        'requested': 'QA copy', 'applied': False, 'reason': "The copy's id is unknown, so it was not renamed.",
    }
    assert 'not an id' in result['warning'], result


@pytest.mark.parametrize('copied', ['960009', 960009, ' 960009 '], ids=['text', 'number', 'padded'])
@pytest.mark.parametrize('kind', ['campaign', 'adset', 'ad'])
@pytest.mark.asyncio
async def test_an_ordinary_copy_id_in_any_form_is_still_renamed(monkeypatch, kind, copied):
    """Control: the copy's id as text, a number or text with spaces is read as the same id and renamed."""
    writes = []

    def handler(request):
        writes.append((request.url.path, parse_qs(request.content.decode())))
        if request.url.path.endswith('/copies'):
            return httpx.Response(200, json={f'copied_{kind}_id': copied, 'ad_object_ids': [
                {'ad_object_type': 'ad', 'source_id': '960004', 'copied_id': '960005'}]})
        return httpx.Response(200, json={'success': True})

    async with graph(monkeypatch, handler, campaigns, ads):
        result = await _copy_tool(kind)(**{f'{kind}_id': '960001', 'new_name': 'QA copy'})
    assert writes[-1][0].endswith('/960009') and writes[-1][1]['name'] == ['QA copy'], writes
    assert result['new_id'] == '960009' and result['rename']['applied'] is True and 'warning' not in result, result


@pytest.mark.parametrize('kind', ['campaign', 'adset', 'ad'])
@pytest.mark.asyncio
async def test_a_copy_answer_with_no_id_still_says_so(monkeypatch, kind):
    """Control: an answer with no copy id keeps its own warning and sends no rename, as before."""
    writes = []

    def handler(request):
        writes.append(request.url.path)
        return httpx.Response(200, json={})

    async with graph(monkeypatch, handler, campaigns, ads):
        result = await _copy_tool(kind)(**{f'{kind}_id': '960001', 'new_name': 'QA copy'})
    assert len(writes) == 1 and result['new_id'] is None, result
    assert result['warning'].startswith("Meta's response did not include the new copy's id"), result


_BOOST_STAGES = ['campaigns', 'adsets', 'adcreatives', 'ads']
_ONE_STEP = dict(account_id='961000', instagram_media_id='961010', instagram_account_id='961011',
                 page_id='961012', daily_budget=5, targeting={'geo_locations': {'countries': ['US']}})


@pytest.mark.parametrize('stage', _BOOST_STAGES)
@pytest.mark.parametrize('known_id', ['961000', '961010', '961011', '961012'])
@pytest.mark.asyncio
async def test_ff1_one_step_boost_does_not_reuse_an_input_object(monkeypatch, stage, known_id):
    writes = []

    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'id': 'act_961000', 'currency': 'USD'})
        edge = request.url.path.rsplit('/', 1)[-1]
        writes.append(edge)
        normal = {'campaigns': '961101', 'adsets': '961102', 'adcreatives': '961103', 'ads': '961104'}
        return httpx.Response(200, json={'id': known_id if edge == stage else normal[edge]})

    async with graph(monkeypatch, handler, creatives):
        with pytest.raises(MetaAdsError) as raised:
            await creatives.boost_instagram_post(**_ONE_STEP)
    assert len(writes) == _BOOST_STAGES.index(stage) + 1, writes
    message = str(raised.value)
    assert f'{known_id}, already known in this call' in message, message
    assert 'may have created it; look for it in the account before retrying' in message, message


@pytest.mark.parametrize('bad_id', [-12, 'not_an_id'])
@pytest.mark.asyncio
async def test_ff1_boost_does_not_build_on_an_invalid_campaign_id(monkeypatch, bad_id):
    writes = []

    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'currency': 'USD'})
        edge = request.url.path.rsplit('/', 1)[-1]
        writes.append(edge)
        return httpx.Response(200, json={'id': bad_id if edge == 'campaigns' else str(962100 + len(writes))})

    async with graph(monkeypatch, handler, creatives):
        with pytest.raises(MetaAdsError) as raised:
            await creatives.boost_instagram_post(
                account_id='962000', instagram_media_id='962010', instagram_account_id='962011',
                page_id='962012', daily_budget=5, targeting={'geo_locations': {'countries': ['US']}})
    assert writes == ['campaigns'], writes
    assert 'something that is not an id where' in str(raised.value)


@pytest.mark.parametrize('stage', ['adcreatives', 'ads'])
@pytest.mark.parametrize('known_id', ['963000', '963010', '963011', '963012'])
@pytest.mark.asyncio
async def test_ff1_existing_boost_does_not_reuse_other_input_objects(monkeypatch, stage, known_id):
    writes = []

    def handler(request):
        edge = request.url.path.rsplit('/', 1)[-1]
        writes.append(edge)
        return httpx.Response(200, json={'id': known_id if edge == stage else '963101'})

    async with graph(monkeypatch, handler, creatives):
        with pytest.raises(MetaAdsError) as raised:
            await creatives.boost_instagram_post(
                account_id='963000', adset_id='963001', instagram_media_id='963010',
                instagram_account_id='963011', page_id='963012')
    assert len(writes) == (1 if stage == 'adcreatives' else 2), writes
    assert f'{known_id}, already known in this call' in str(raised.value), str(raised.value)


@pytest.mark.parametrize('form', ['text', 'number'])
@pytest.mark.asyncio
async def test_an_ordinary_one_step_boost_still_creates_all_four(monkeypatch, form):
    """Control: new ids as text or as numbers, none of them an input object, build the boost as before."""
    sent = []

    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'id': 'act_961000', 'currency': 'USD'})
        edge = request.url.path.rsplit('/', 1)[-1]
        sent.append((edge, parse_qs(request.content.decode())))
        new = {'campaigns': 961101, 'adsets': 961102, 'adcreatives': 961103, 'ads': 961104}[edge]
        return httpx.Response(200, json={'id': str(new) if form == 'text' else new})

    async with graph(monkeypatch, handler, creatives):
        result = await creatives.boost_instagram_post(**_ONE_STEP)
    assert [edge for edge, _ in sent] == _BOOST_STAGES
    assert sent[1][1]['campaign_id'] == ['961101'] and sent[3][1]['adset_id'] == ['961102'], sent
    assert result['ok'] is True and (result['campaign_id'], result['adset_id'], result['creative_id'], result['ad_id']) == (
        '961101', '961102', '961103', '961104'), result


@pytest.mark.asyncio
async def test_a_boost_create_with_no_id_keeps_its_message(monkeypatch):
    """Control: a create answer with no id stops the boost with the message it had before."""
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'currency': 'USD'})
        return httpx.Response(200, json={})

    async with graph(monkeypatch, handler, creatives):
        with pytest.raises(MetaAdsError, match='Meta returned no id'):
            await creatives.boost_instagram_post(**_ONE_STEP)


@pytest.mark.parametrize('form', ['text', 'number'])
@pytest.mark.asyncio
async def test_an_ordinary_existing_adset_boost_still_creates_the_ad(monkeypatch, form):
    """Control: in an existing ad set, new creative and ad ids as text or numbers are used as before."""
    sent = []

    def handler(request):
        edge = request.url.path.rsplit('/', 1)[-1]
        sent.append((edge, parse_qs(request.content.decode())))
        new = 963101 if edge == 'adcreatives' else 963102
        return httpx.Response(200, json={'id': str(new) if form == 'text' else new})

    async with graph(monkeypatch, handler, creatives):
        result = await creatives.boost_instagram_post(
            account_id='963000', adset_id='963001', instagram_media_id='963010',
            instagram_account_id='963011', page_id='963012')
    assert json.loads(sent[1][1]['creative'][0]) == {'creative_id': '963101'}, sent
    assert result['ok'] is True and result['creative_id'] == '963101' and result['ad_id'] == '963102', result


def _enhancement_handler(writes, returned):
    def handler(request):
        node = request.url.path.rsplit('/', 1)[-1]
        if request.method == 'GET':
            if node == '965001':
                return httpx.Response(200, json={'id': node, 'account_id': '965000', 'creative': {'id': '965002'}})
            if 'object_story_spec{' in request.url.params.get('fields', ''):
                return httpx.Response(200, json={'id': '965002'})
            return httpx.Response(200, json={'id': '965002', 'account_id': '965000',
                'object_story_spec': {'page_id': '965010', 'link_data': {'link': 'https://example.com'}}})
        writes.append((node, parse_qs(request.content.decode())))
        return httpx.Response(200, json=returned if node == 'adcreatives' else {'success': True})
    return handler


@pytest.mark.parametrize('returned_id', ['965000', '965010', -12, 'not_an_id'])
@pytest.mark.asyncio
async def test_ff1_enhancement_does_not_attach_a_known_object_or_non_id_as_its_new_creative(monkeypatch, returned_id):
    writes = []
    async with graph(monkeypatch, _enhancement_handler(writes, {'id': returned_id}), creative_formats):
        with pytest.raises(MetaAdsError) as raised:
            await creative_formats.update_ad_creative_enhancements(ad_id='965001', features={'image_touchups': 'OPT_OUT'})
    assert len(writes) == 1, writes
    assert 'A replacement creative may have been created' in str(raised.value), str(raised.value)


@pytest.mark.parametrize('returned_id', ['965003', 965003, ' 965003 '], ids=['text', 'number', 'padded'])
@pytest.mark.asyncio
async def test_an_ordinary_enhancement_copy_is_still_attached(monkeypatch, returned_id):
    """Control: a new creative id as text, a number or padded text is attached as before."""
    writes = []
    async with graph(monkeypatch, _enhancement_handler(writes, {'id': returned_id}), creative_formats):
        result = await creative_formats.update_ad_creative_enhancements(ad_id='965001', features={'image_touchups': 'OPT_OUT'})
    assert writes[1][0] == '965001' and json.loads(writes[1][1]['creative'][0]) == {'creative_id': '965003'}, writes
    assert result['ok'] is True and result['current']['creative_id'] == '965003', result


_BUILDER = dict(account_id='968000', name='QA creative', page_id='968001', instagram_account_id='968002',
                image_hash='qa-hash', link_url='https://example.com')


@pytest.mark.parametrize('bad_id', [-12, 'not_an_id'])
@pytest.mark.asyncio
async def test_ff1_builder_does_not_show_a_non_id_as_its_new_creative(monkeypatch, bad_id):
    async with graph(monkeypatch, lambda request: httpx.Response(200, json={'id': bad_id}), creative_formats):
        result = await creative_formats.create_ad_creative_from_inputs(**_BUILDER)
    assert result['creative_id'] is None and result['created'] is None, result
    assert result['created_id_unresolved'] is True and result['unresolved_result'] == {'id': bad_id}, result
    assert 'may have been created' in result['warning'], result


@pytest.mark.parametrize('known_id', ['968000', '968001', '968002'])
@pytest.mark.asyncio
async def test_ff1_builder_does_not_show_an_input_object_as_its_new_creative(monkeypatch, known_id):
    async with graph(monkeypatch, lambda request: httpx.Response(200, json={'id': known_id}), creative_formats):
        result = await creative_formats.create_ad_creative_from_inputs(**_BUILDER)
    assert result['creative_id'] is None and result['created'] is None, result
    assert result['created_id_unresolved'] is True and 'may have been created' in result['warning'], result


@pytest.mark.parametrize('answer,shown', [
    ({'id': '968003'}, '968003'), ({'id': 968003}, '968003'), ({'id': ' 968003 '}, '968003'), ({}, None),
], ids=['text', 'number', 'padded', 'absent'])
@pytest.mark.asyncio
async def test_an_ordinary_builder_answer_shows_its_creative_as_before(monkeypatch, answer, shown):
    """Control: the new creative's id as text, a number or padded text is shown; no id shows none, as before."""
    async with graph(monkeypatch, lambda request: httpx.Response(200, json=answer), creative_formats):
        result = await creative_formats.create_ad_creative_from_inputs(**_BUILDER)
    assert result['ok'] is True and result['creative_id'] == shown and result['created'] == answer, result
    assert 'warning' not in result and 'created_id_unresolved' not in result, result


async def _create_with_schedule(monkeypatch, tool, created, reads):
    def handler(request):
        if request.method == 'POST':
            return httpx.Response(200, json=created)
        reads.append(request.url.path)
        return httpx.Response(200, json={'id': created.get('id'),
            'start_time': '2027-01-01T00:00:00+0000', 'end_time': '2027-01-08T00:00:00+0000'})

    async with graph(monkeypatch, handler, campaigns):
        if tool == 'campaign':
            return await campaigns.create_campaign(account_id='966000', name='QA campaign',
                objective='OUTCOME_TRAFFIC', start_time='2027-01-01T00:00:00Z')
        return await campaigns.create_ad_set(account_id='966000', campaign_id='966001', name='QA set',
            billing_event='IMPRESSIONS', optimization_goal='LINK_CLICKS',
            targeting={'geo_locations': {'countries': ['US']}}, start_time='2027-01-01T00:00:00')


@pytest.mark.parametrize('tool', ['campaign', 'adset'])
@pytest.mark.asyncio
async def test_ff1_create_schedule_cannot_be_read_back_from_a_known_parent(monkeypatch, tool):
    reads = []
    known = '966000' if tool == 'campaign' else '966001'
    result = await _create_with_schedule(monkeypatch, tool, {'id': known}, reads)
    assert reads == [] and 'stored_schedule' not in result, (reads, result)
    assert any('could not be read back' in warning for warning in result['warnings']), result


@pytest.mark.parametrize('tool', ['campaign', 'adset'])
@pytest.mark.parametrize('bad_id', [True, -12, 'not_an_id', 1.5])
@pytest.mark.asyncio
async def test_ff1_create_schedule_does_not_verify_a_non_id(monkeypatch, tool, bad_id):
    reads = []
    result = await _create_with_schedule(monkeypatch, tool, {'id': bad_id}, reads)
    assert reads == [] and 'stored_schedule' not in result, (reads, result)
    assert any('could not be read back' in warning for warning in result['warnings']), result


@pytest.mark.parametrize('tool', ['campaign', 'adset'])
@pytest.mark.parametrize('new_id', ['966002', 966002, ' 966002 '], ids=['text', 'number', 'padded'])
@pytest.mark.asyncio
async def test_an_ordinary_new_id_still_reads_back_the_stored_schedule(monkeypatch, tool, new_id):
    """Control: the new object's id as text, a number or padded text is read back as before."""
    reads = []
    result = await _create_with_schedule(monkeypatch, tool, {'id': new_id}, reads)
    assert [path.rsplit('/', 1)[-1] for path in reads] == ['966002'], reads
    assert result['stored_schedule']['start_time'] == '2027-01-01T00:00:00+0000', result


@pytest.mark.parametrize('tool', ['campaign', 'adset'])
@pytest.mark.asyncio
async def test_a_create_with_no_id_still_says_the_schedule_could_not_be_read_back(monkeypatch, tool):
    """Control: no id in the answer reads nothing back and keeps the read-back-unavailable warning."""
    reads = []
    result = await _create_with_schedule(monkeypatch, tool, {}, reads)
    assert reads == [] and 'stored_schedule' not in result and result['ok'] is True, result
    assert any('could not be read back' in warning for warning in result['warnings']), result


def _video_handler(created, status_id, reads=None):
    def handler(request):
        if request.method == 'POST':
            return httpx.Response(200, json=created)
        if reads is not None:
            reads.append(request.url.path)
        return httpx.Response(200, json={'id': status_id, 'status': {'video_status': 'ready'}})
    return handler


@pytest.mark.parametrize('id_key', ['id', 'video_id'])
@pytest.mark.asyncio
async def test_ff1_video_upload_cannot_verify_the_account_as_its_new_video(monkeypatch, id_key):
    reads = []
    async with graph(monkeypatch, _video_handler({id_key: '967000'}, '967000', reads), creatives):
        result = await creatives.upload_ad_video(account_id='967000', video_url='https://example.com/qa.mp4')
    assert reads == [] and result['video_id'] is None and result['processing_status'] is None, result
    assert 'not a usable video id' in result['status_note'], result


@pytest.mark.parametrize('created', [
    {'id': '967001'}, {'id': 967001}, {'id': ' 967001 '}, {'video_id': '967001'},
], ids=['text', 'number', 'padded', 'video_id-key'])
@pytest.mark.asyncio
async def test_an_ordinary_video_upload_still_reads_its_status(monkeypatch, created):
    """Control: the new video's id as text, a number, padded text or under video_id is read as before."""
    async with graph(monkeypatch, _video_handler(created, '967001'), creatives):
        result = await creatives.upload_ad_video(account_id='967000', video_url='https://example.com/qa.mp4')
    assert result['video_id'] == '967001' and result['processing_status'] == 'ready', result


@pytest.mark.asyncio
async def test_a_video_upload_with_no_id_still_says_so(monkeypatch):
    """Control: no id in the answer keeps the note it had before."""
    async with graph(monkeypatch, _video_handler({}, '967001'), creatives):
        result = await creatives.upload_ad_video(account_id='967000', video_url='https://example.com/qa.mp4')
    assert result['video_id'] is None and result['status_note'].startswith('Meta returned no video id'), result


def _chunked_handler(phases, *, session_id, video_id):
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'id': request.url.path.rsplit('/', 1)[-1], 'status': {'video_status': 'ready'}})
        phase = 'transfer' if b'video_file_chunk' in request.content else parse_qs(request.content.decode())['upload_phase'][0]
        phases.append(phase)
        if phase == 'start':
            return httpx.Response(200, json={'upload_session_id': session_id, 'video_id': video_id,
                'start_offset': '0', 'end_offset': '8'})
        if phase == 'transfer':
            return httpx.Response(200, json={'start_offset': '8', 'end_offset': '8'})
        return httpx.Response(200, json={'success': True})
    return handler


async def _chunked_upload(monkeypatch, tmp_path, handler):
    video = tmp_path / 'qa.mp4'
    video.write_bytes(b'qa-video')
    monkeypatch.setattr(creatives, 'MAX_DIRECT_VIDEO_BYTES', 1)
    async with graph(monkeypatch, handler, creatives):
        return await asyncio.wait_for(creatives.upload_ad_video(account_id='967000', file_path=str(video)), timeout=3)


@pytest.mark.asyncio
async def test_ff1_chunked_upload_does_not_verify_the_account_as_its_new_video(monkeypatch, tmp_path):
    phases = []
    result = await _chunked_upload(monkeypatch, tmp_path, _chunked_handler(phases, session_id='967100', video_id='967000'))
    assert phases == ['start', 'transfer', 'finish'], phases
    assert result['video_id'] is None and result['processing_status'] is None, result
    assert 'not a usable video id' in result['status_note'], result


@pytest.mark.parametrize('session_id', ['967100', '967000', '967001', 'sess-abc'],
                         ids=['ordinary', 'same-as-account', 'same-as-video', 'not-digits'])
@pytest.mark.asyncio
async def test_an_ordinary_chunked_upload_reads_its_video_and_never_compares_the_session(monkeypatch, tmp_path, session_id):
    """Control: the upload session id is not a Graph object id and is compared with nothing."""
    phases = []
    result = await _chunked_upload(monkeypatch, tmp_path, _chunked_handler(phases, session_id=session_id, video_id='967001'))
    assert phases == ['start', 'transfer', 'finish'], phases
    assert result['video_id'] == '967001' and result['processing_status'] == 'ready', result
    assert result['upload']['upload_session_id'] == session_id, result
