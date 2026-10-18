"""Correct-behavior regressions for the independent issue #17 review.

All data is synthetic. Host integration executes installed host methods read-only;
CI without Hermes skips only those integration cases. Never call real providers.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import json
import logging
import os
import sys
import types
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_dossiers import (  # noqa: F401 - fixtures shared with acceptance suite
    NOW, FakeLLM, cli, event, fact, message, run, seed_records,
)

from test_dossiers import plugin as plugin_fixture, source as source_fixture

plugin = plugin_fixture
source = source_fixture

HOST = Path('/usr/local/lib/hermes-agent')


def host_method(relative, cls, method, namespace):
    path = HOST / relative
    if not path.exists():
        pytest.skip('installed Hermes integration source unavailable')
    tree = ast.parse(path.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    function = next(n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == method)
    unit = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), function], type_ignores=[])
    ast.fix_missing_locations(unit)
    exec(compile(unit, str(path), 'exec'), namespace)
    return namespace[method]


@pytest.mark.parametrize('response_text', ['They plan to get together on Tue.', 'Meeting proposed for Tuesday!'])
def test_removed_ids_and_wording_cannot_return(plugin, source, response_text):
    d = plugin._dossier_support
    entry = source.add()
    run(d, FakeLLM(), entry)
    d.edit(entry, 'remove', fact_id='f1')
    source.data[1].append(message(3, 'Any news?'))
    assert run(d, FakeLLM({'facts': [fact(id='f1', text=response_text)], 'retire': []}), entry) == {'refreshed': 1}
    value = d.load(entry)
    assert not value['facts'] and value['next_fact'] == 2
    assert value['removed'][0]['id'] == 'f1'


def test_correction_blocks_old_echo_marks_ambiguous_claim_and_keeps_new_evidence(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    run(d, FakeLLM(), entry)
    d.edit(entry, 'correct', fact_id='f1', text='Meeting cancelled; do not arrange it.')
    source.data[1].append(message(3, 'Any news?'))
    assert run(d, FakeLLM(), entry) == {'refreshed': 1}
    assert [f['text'] for f in d.load(entry)['facts']] == ['Meeting cancelled; do not arrange it.']
    source.data[1].append(message(4, 'A neutral update'))
    run(d, FakeLLM({'facts': [fact(text='They plan to get together on Tue.')], 'retire': []}), entry)
    ambiguous = d.load(entry)['facts']
    assert len(ambiguous) == 2
    assert ambiguous[1]['section'] == 'contradictions' and ambiguous[1]['status'] == 'owner review'
    assert ambiguous[1]['owner_review'] == ['f1']
    # Fresh evidence is not swallowed just because it conflicts with a correction.
    source.data[1].append(message(5, "Let's meet Tuesday again.", 'inbound'))
    fresh = fact(ref='m5', quote="Let's meet Tuesday again.")
    run(d, FakeLLM({'facts': [fresh], 'retire': []}), entry)
    facts = d.load(entry)['facts']
    assert facts[0]['text'] == 'Meeting cancelled; do not arrange it.'
    assert len(facts) == 3 and facts[-1]['section'] == 'contradictions'
    assert facts[-1]['sources'][0]['ref'] == 'm5'


def test_correct_then_remove_protects_original_wording_under_new_id(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    run(d, FakeLLM(), entry)
    d.edit(entry, 'correct', fact_id='f1', text='Meeting cancelled; do not arrange it.')
    d.edit(entry, 'remove', fact_id='f1')
    source.data[1].append(message(3, 'Any news?'))
    run(d, FakeLLM(), entry)
    assert not d.load(entry)['facts']
    # Tombstones stay authoritative even if the old source has since changed.
    source.data[1][0]['text'] = "Let's meet Tuesday again."
    run(d, FakeLLM({'facts': [fact(quote="Let's meet Tuesday again.")], 'retire': []}), entry)
    assert not d.load(entry)['facts']


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['remove', 'correct', 'correct_remove'])
async def test_concurrent_feedback_rebase_rechecks_full_local_policy(plugin, source, action):
    d = plugin._dossier_support
    entry = source.add()
    await d.run_cycle(FakeLLM(), entry=entry, manual=True, now=NOW)
    source.data[1].append(message(3, 'Any news?'))

    def feedback(kwargs, count):
        if action.startswith('correct'):
            d.edit(entry, 'correct', fact_id='f1', text='Cancelled by owner.')
        if action in {'remove', 'correct_remove'}:
            d.edit(entry, 'remove', fact_id='f1')
        return {'facts': [fact(id='f1', text='They plan to get together on Tue.'), fact()], 'retire': []}

    assert await d.run_cycle(FakeLLM(callback=feedback), entry=entry, manual=True, now=NOW) == {'refreshed': 1}
    facts = d.load(entry)['facts']
    assert [f['text'] for f in facts] == (['Cancelled by owner.'] if action == 'correct' else [])


@pytest.mark.parametrize('left,right', [
    ('Alice says the agreed budget is EUR 100.', 'Owner says the agreed budget is EUR 200.'),
    ('Alice agreed to attend the Tuesday meeting.', 'Alice did not agree to attend the Tuesday meeting.'),
    ('Alice promised to book the meeting room.', 'Bob promised to book the meeting room.'),
])
def test_different_claims_and_evidence_never_fuzzy_merge(plugin, source, left, right):
    d = plugin._dossier_support
    entry = source.add(received=left, sent=right)
    response = {'facts': [fact(text=left, quote=left, section='contradictions'),
                          fact(ref='m2', text=right, quote=right, section='contradictions')], 'retire': []}
    run(d, FakeLLM(response), entry)
    facts = d.load(entry)['facts']
    assert [(f['id'], f['text']) for f in facts] == [('f1', left), ('f2', right)]
    source.data[1].append(message(3, 'new'))
    run(d, FakeLLM(response), entry)
    assert [f['id'] for f in d.load(entry)['facts']] == ['f1', 'f2']
    # Exact text with different evidence must also survive separately.
    source.data[1].append(message(4, left))
    run(d, FakeLLM({'facts': [fact(ref='m4', text=left, quote=left, section='contradictions')], 'retire': []}), entry)
    assert len(d.load(entry)['facts']) == 3


def test_explicit_id_update_preserves_identity(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    run(d, FakeLLM(), entry)
    source.data[1][0]['text'] = 'Changed decision'
    run(d, FakeLLM({'facts': [fact(id='f1', text='Changed decision', quote='Changed decision')], 'retire': []}), entry)
    value = d.load(entry)
    assert value['facts'][0]['id'] == 'f1' and value['facts'][0]['first_revision'] == 1
    assert value['facts'][0]['last_revision'] == 2 and value['next_fact'] == 2


@pytest.mark.parametrize('all_targets', [False, True])
@pytest.mark.parametrize('crash_target', ['changes.jsonl', 'dossier.json'])
@pytest.mark.parametrize('later_success', [False, True])
def test_real_atomic_write_process_exit_then_forget_cleans_private_residue(plugin, source, all_targets, crash_target, later_success):
    d = plugin._dossier_support
    entry = source.add()
    run(d, FakeLLM(), entry)
    canonical_before = copy.deepcopy(source.data)
    pid = os.fork()
    if pid == 0:
        real_replace = d.os.replace
        def crash_replace(src, dst):
            if Path(dst).name == crash_target:
                os._exit(77)
            real_replace(src, dst)
        d.os.replace = crash_replace
        d.edit(entry, 'correct', fact_id='f1', text='Crash-only private owner correction')
        os._exit(78)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 77
    leftovers = list(d.directory(entry).glob('.dossier-*'))
    assert len(leftovers) == 1 and 'Crash-only private owner correction' in leftovers[0].read_text()
    if later_success:
        d.edit(entry, 'correct', fact_id='f1', text='Later successful correction')
        assert leftovers[0].exists()  # success does not imply old crash residue is absent
    if all_targets:
        result = d.forget_all()
        assert result['forgotten'] == 1 and result['failed'] == 0
    else:
        d.forget(entry)
    assert not d.directory(entry).exists() and source.data == canonical_before


def test_forget_all_continues_on_failure_and_refuses_symlinks(plugin, source, capsys):
    d = plugin._dossier_support
    a, b, c = source.add(), source.add(2), source.add(3)
    for entry in (a, b, c):
        run(d, FakeLLM(), entry)
    outside = d.root().parent / 'outside'
    outside.mkdir()
    secret = outside / 'dossier.json'
    secret.write_text('outside must survive')
    # Simulate both a symlinked target and an internal temp symlink.
    d.forget(a)
    d.directory(a).symlink_to(outside, target_is_directory=True)
    (d.directory(b) / '.dossier-symlink').symlink_to(secret)
    assert cli(plugin, 'forget', '--all') == 1
    result = json.loads(capsys.readouterr().out.splitlines()[0])
    assert result['failed'] == 2 and result['forgotten'] == 1
    assert set(result['failed_targets']) == {d.target_key(a), d.target_key(b)}
    assert secret.read_text() == 'outside must survive'
    assert d.directory(a).is_symlink() and not d.directory(c).exists()


@pytest.mark.parametrize('protection', ['pin', 'correct'])
@pytest.mark.parametrize('restored_text', ["Let's meet Tuesday.", 'A later edit of the same ID'])
def test_retention_then_real_message_edited_restores_protected_reference(plugin, protection, restored_text):
    h, d = plugin._history_support, plugin._dossier_support
    old = NOW - timedelta(days=60)
    seed_records(plugin, event(plugin, 1, "Let's meet Tuesday.", observed=old, date=old),
                 event(plugin, 2, 'Sure, Tuesday works!', 'outbound', date=NOW-timedelta(hours=1)))
    h.rebuild_contact_catalog()
    entry = h._resolve_chat_entry(business_connection_id=None, chat_id='1')
    run(d, FakeLLM(), entry)
    d.edit(entry, protection, fact_id='f1', **({'text': 'Owner corrected wording'} if protection == 'correct' else {}))
    expected = d.load(entry)['facts'][0]['text']
    partitions = h._iter_history_files(h._entry_chat_dir(entry))
    assert len(partitions) == 2
    partitions[0].unlink()
    h.rebuild_contact_catalog()
    run(d, FakeLLM({'facts': [], 'retire': []}), entry)
    assert d.load(entry)['facts'][0]['status'] == 'source gone'
    seed_records(plugin, event(plugin, 1, restored_text, kind='message.edited', observed=NOW, date=old))
    h.rebuild_contact_catalog()
    run(d, FakeLLM({'facts': [], 'retire': []}), entry)
    shown = d.inspect_dossier(entry, 'f1')
    assert shown['fact']['text'] == expected
    assert shown['fact']['status'] == ('active' if restored_text == "Let's meet Tuesday." else 'stale')
    assert not shown['fact']['missing_sources']
    assert shown['fact']['sources'][0]['ref'] == 'm1'
    assert shown['canonical_sources'][0]['text'] == restored_text


def large_feedback(d, entry, count=4000):
    value = d.load(entry)
    value['removed'] = [{'id': f'f{i+1000}', 'text': f'{i} ' + 'x'*790, 'section': 'preferences'} for i in range(count)]
    value['cursor'].update({f'm{10000+i}': 'old' for i in range(count)})
    with d.lock(d.root() / ('.' + d.target_key(entry) + '.lock')):
        d.save(entry, value, [], 'synthetic long-lived feedback')


def test_whole_request_bound_thousands_of_removals_pruning_preview_and_progression(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    run(d, FakeLLM(), entry)
    large_feedback(d, entry)
    source.data[1].extend(message(i, f'Evidence {i} ' + 'y'*1900) for i in range(3, 65))
    expected = d.encode(d.preview(entry, now=NOW))
    llm = FakeLLM({'facts': [], 'retire': []})
    assert run(d, llm, entry) == {'refreshed': 1}
    assert llm.calls[0]['input'][0]['text'] == expected and len(llm.calls) > 1
    seen = []
    for call in llm.calls:
        data = json.loads(call['input'][0]['text'])
        assert d.request_size(data) <= d.MAX_REQUEST_BYTES
        assert len(d.encode(call).encode()) <= d.MAX_REQUEST_BYTES
        assert len(data['do_not_readd']) < 100 and len(data['removed_sources']) < 4000
        seen.extend(m['ref'] for m in data['messages'])
    assert seen == sorted(f'm{i}' for i in range(3, 65))
    assert len(d.load(entry)['removed']) == 4000
    assert run(d, llm, entry) == {'unchanged': 1}
    # An omitted oldest tombstone still blocks a patch locally, even with new evidence.
    value = d.load(entry)
    raw = {'facts': [fact(text=value['removed'][0]['text'])], 'retire': []}
    assert not d.validate(raw, value, {m['ref']: m for m in source.data[1]}, value['revision']+1)


@pytest.mark.parametrize('text', ['🧑'*800, '\\'*800, '"'*800])
def test_maximum_legal_owner_facts_fit_without_dropping_protection(plugin, source, text):
    d = plugin._dossier_support
    entry = source.add()
    value = d.empty(entry)
    value['facts'] = [{
        'id': f'f{i}', 'kind': 'owner', 'section': 'preferences', 'text': text,
        'owner_edited': True, 'status': 'active',
        'sources': [{'ref': f'm{j}'} for j in range(20)],
    } for i in range(100)]
    value['removed'] = [{'id': f'f{1000+i}', 'text': 'x'*800, 'section': 'preferences'} for i in range(2000)]
    data = d.payload(entry, value, [message(1, '🧑'*4000)], [f'm{i}' for i in range(2000)])
    assert len(data['facts']) == len(data['owner_read_only']) == 100
    assert all(f['text'] == text for f in data['facts'])
    assert d.request_size(data) <= d.MAX_REQUEST_BYTES


@pytest.mark.asyncio
async def test_long_lived_payload_concurrent_rebase_enforces_unprojected_tombstone(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    await d.run_cycle(FakeLLM(), entry=entry, manual=True, now=NOW)
    large_feedback(d, entry)
    source.data[1].append(message(3, 'Any update?'))
    def feedback(kwargs, count):
        data = json.loads(kwargs['input'][0]['text'])
        assert d.request_size(data) <= d.MAX_REQUEST_BYTES
        d.edit(entry, 'remove', fact_id='f1')
        return {'facts': [fact(id='f1', text='They plan to get together on Tue.')], 'retire': []}
    assert await d.run_cycle(FakeLLM(callback=feedback), entry=entry, manual=True, now=NOW) == {'refreshed': 1}
    assert not d.load(entry)['facts']


def test_oversized_protected_context_fails_without_call_or_consuming_cursor(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    # Corrupt/noncanonical auxiliary identifiers cannot force oversized calls.
    entry['current_profile']['first_name'] = 'x'*2_000_000
    data = d.payload(entry, d.empty(entry), source.data[1], [])
    assert d.request_size(data) < d.MAX_REQUEST_BYTES
    value = d.empty(entry)
    value['facts'] = [{'id': 'f1', 'kind': 'owner', 'section': 'preferences', 'text': 'x'*d.MAX_REQUEST_BYTES,
                       'status': 'active', 'sources': []}]
    with pytest.raises(ValueError, match='request budget'):
        d.payload(entry, value, source.data[1], [])
    run(d, FakeLLM(), entry)
    value['revision'] = d.load(entry)['revision']
    value['last_refresh'] = NOW.isoformat()
    value['cursor'] = d.versions(source.data[1])
    with d.lock(d.root() / ('.' + d.target_key(entry) + '.lock')):
        d.save(entry, value, [], 'synthetic corrupt protected context')
    before = d.load(entry)
    source.data[1].append(message(3, 'Pending new source'))
    llm = FakeLLM()
    assert run(d, llm, entry) == {'failed': 1}
    assert not llm.calls and d.load(entry)['cursor'] == before['cursor']
    assert d.load(entry)['revision'] == before['revision']


def test_actual_attribution_unknown_and_mixed_are_distinct_from_requested(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    class AttributedLLM:
        def __init__(self):
            self.calls = 0
        async def acomplete_structured(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(parsed={'facts': [], 'retire': []}, provider='actual-provider', model=f'actual-{self.calls}')
    source.data[1].extend(message(i, f'Synthetic {i}') for i in range(3, 105))
    assert run(d, AttributedLLM(), entry) == {'refreshed': 1}
    value = d.inspect_dossier(entry)
    assert value['model'] == {'provider': 'mixed', 'model': 'mixed'}
    assert [m['model'] for m in value['model_calls']] == ['actual-1', 'actual-2', 'actual-3']
    assert value['requested_model'] == {'provider': 'gemini', 'model': 'gemini-3.8-flash'}
    assert 'actual-2' in d.render(value)
    source.data[1].append(message(105, 'New'))
    assert run(d, FakeLLM(), entry) == {'refreshed': 1}
    assert d.load(entry)['model'] == {'provider': None, 'model': None}
    assert d.load(entry)['requested_model']['model'] == 'gemini-3.8-flash'


@pytest.mark.asyncio
@pytest.mark.parametrize('await_cancellation', [True, False])
async def test_actual_host_unload_reload_restarts_worker_when_native_handler_is_deduped(plugin, monkeypatch, await_cancellation):
    """Run host register/wire/spawn/unload methods; never re-call skipped factories."""
    namespace = {'asyncio': asyncio, 'logger': logging.getLogger('synthetic-host')}
    methods = {name: host_method('hermes_cli/plugins.py', 'PluginContext', name, namespace)
               for name in ('spawn_task', 'register_platform_handler', 'register_telegram_handler')}
    wire = host_method('gateway/platforms/base.py', 'BasePlatformAdapter', '_wire_plugin_handlers', namespace)
    for name in ('_unload_scoped', '_dispose_registrations', '_forget_registrations'):
        methods[name] = host_method('hermes_cli/plugins_ledger.py', 'PluginLedgerMixin', name, namespace)
    # Use the real host registration's dispose/cancellation behavior as well.
    tree = ast.parse((HOST / 'hermes_cli/plugins_ledger.py').read_text())
    registration = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PluginRegistration')
    namespace.update(dataclass=dataclass, field=field, __name__=__name__)
    unit = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), registration], type_ignores=[])
    ast.fix_missing_locations(unit)
    exec(compile(unit, 'host_registration', 'exec'), namespace)
    registration_class = namespace['PluginRegistration']
    manager = SimpleNamespace(_platform_handler_factories={}, _registration_order=[], _ownership_ledger={},
                              _memory_hook_registrations={}, _plugins={'synthetic': object()})
    for name in ('_unload_scoped', '_dispose_registrations', '_forget_registrations'):
        setattr(manager, name, types.MethodType(methods[name], manager))
    manager._resolve_plugin_key = lambda p: p
    manager._unload_target_keys = lambda p: {p}
    manager.get_platform_handler_factories = lambda platform: list(manager._platform_handler_factories.get(platform, []))
    stub = types.ModuleType('hermes_cli.plugins')
    stub.get_plugin_manager = lambda: manager
    stub._PLUGINS_DEBUG = False
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugins', stub)
    ctx = SimpleNamespace(plugin_id='synthetic', manifest=SimpleNamespace(name='synthetic'), _manager=manager,
                          llm=FakeLLM(), register_hook=lambda *a: None, register_cli_command=lambda **k: None,
                          register_skill=lambda *a: None)
    def track(kind, name, release):
        handle = registration_class(kind, name, release, plugin_key='synthetic')
        manager._registration_order.append(handle)
        manager._ownership_ledger.setdefault('synthetic', []).append(handle)
        return handle
    ctx._track = track
    for name in ('spawn_task', 'register_platform_handler', 'register_telegram_handler'):
        setattr(ctx, name, types.MethodType(methods[name], ctx))
    monkeypatch.setattr(plugin._history_support, 'run_startup_maintenance',
                        lambda: SimpleNamespace(cap_exceeded=False, warnings=[]))
    plugin.register(ctx)
    assert not plugin._dossier_support._workers  # even with an asyncio loop, CLI is safe
    adapter = SimpleNamespace(platform=SimpleNamespace(value='telegram'), name='Synthetic',
                              _plugin_handler_native=None, _plugin_handlers_wired=None)
    handlers = []
    app = SimpleNamespace(running=True, add_handler=lambda *a, **k: handlers.append(a[0]))
    wire(adapter, app)
    d = plugin._dossier_support
    first = d._workers[str(d.root())]
    assert len(handlers) == 1
    await asyncio.sleep(0)
    assert manager._unload_scoped('synthetic')
    if await_cancellation:
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not d._workers
    # Re-register as host reload does; same app and handler qualname remain wired.
    plugin.register(ctx)
    wire(adapter, app)
    second = d._workers[str(d.root())]
    assert second is not first and not second.done() and len(handlers) == 1
    if not await_cancellation:
        with pytest.raises(asyncio.CancelledError):
            await first
        assert d._workers[str(d.root())] is second
    assert not ctx.llm.calls  # still delayed, no model/provider side effects
    manager._unload_scoped('synthetic')
    with pytest.raises(asyncio.CancelledError):
        await second
    assert not d._workers


@pytest.fixture
def host_llm(monkeypatch):
    path = HOST / 'agent/plugin_llm.py'
    if not path.exists():
        pytest.skip('installed Hermes structured facade unavailable')
    spec = importlib.util.spec_from_file_location('synthetic_review_host_plugin_llm', path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    # The installed host is read-only, including bytecode caches.
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def test_real_host_complete_structured_schema_validation_and_attribution(plugin, source, host_llm, caplog):
    # jsonschema must be present; this test must never silently take the host's
    # optional-validation skip path in the isolated review environment.
    import jsonschema
    assert jsonschema.Draft202012Validator
    d = plugin._dossier_support
    entry = source.add()
    output = {'facts': [fact()], 'retire': []}
    calls = []
    def provider(**kwargs):
        calls.append(kwargs)
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(output)))])
        return 'observed-provider', 'observed-model', response
    async def async_provider(**kwargs):
        return provider(**kwargs)
    policy = host_llm._TrustPolicy(plugin_id='synthetic', allow_provider_override=True, allow_model_override=True)
    llm = host_llm.make_plugin_llm_for_test(plugin_id='synthetic', policy=policy,
                                          sync_caller=provider, async_caller=async_provider)
    preview = d.preview(entry, now=NOW)
    kwargs = d.request(preview)
    valid = llm.complete_structured(**kwargs)
    assert valid.parsed == output and valid.provider == 'observed-provider' and valid.model == 'observed-model'
    assert len(d.encode(calls[0]).encode()) <= d.request_size(preview) <= d.MAX_REQUEST_BYTES
    assert run(d, llm, entry) == {'refreshed': 1}
    assert d.load(entry)['model'] == {'provider': 'observed-provider', 'model': 'observed-model'}
    output = {'facts': [{'section': 'shared_facts', 'text': 'PRIVATE_INVALID_MODEL_OUTPUT'}], 'retire': []}
    with pytest.raises(ValueError, match='did not match schema'):
        llm.complete_structured(**kwargs)
    source.data[1].append(message(3, 'New synthetic data'))
    before = d.load(entry)
    assert run(d, llm, entry) == {'failed': 1}
    assert d.load(entry)['cursor'] == before['cursor']
    assert 'PRIVATE_INVALID_MODEL_OUTPUT' not in caplog.text
    output = {'facts': [], 'retire': []}
    kwargs['json_schema'] = {'type': 'not-a-real-json-schema-type'}
    with pytest.raises(jsonschema.SchemaError):
        llm.complete_structured(**kwargs)


def test_forget_preserves_real_canonical_archive_bytes(plugin):
    h, d = plugin._history_support, plugin._dossier_support
    seed_records(plugin, event(plugin, 1, "Let's meet Tuesday."),
                 event(plugin, 2, 'Sure, Tuesday works!', 'outbound'))
    h.rebuild_contact_catalog()
    entry = h._resolve_chat_entry(business_connection_id=None, chat_id='1')
    before = {str(p.relative_to(h.history_root())): p.read_bytes()
              for p in h.history_root().rglob('*') if p.is_file()}
    run(d, FakeLLM(), entry)
    (d.directory(entry) / '.dossier-abandoned').write_text('synthetic private residue')
    assert d.forget_all()['failed'] == 0
    after = {str(p.relative_to(h.history_root())): p.read_bytes()
             for p in h.history_root().rglob('*') if p.is_file()}
    assert after == before and not d.directory(entry).exists()


def test_max_protected_state_long_feedback_multichunk_concurrent_correction(plugin, source):
    d = plugin._dossier_support
    entry = source.add()
    run(d, FakeLLM(), entry)
    value = d.load(entry)
    value['facts'] = [{
        'id': f'f{i}', 'kind': 'owner', 'section': 'preferences', 'text': '🧑'*800,
        'owner_edited': True, 'status': 'active', 'sources': [], 'first_revision': 1, 'last_revision': 1,
    } for i in range(1, 101)]
    value['next_fact'] = 101
    with d.lock(d.root() / ('.' + d.target_key(entry) + '.lock')):
        d.save(entry, value, [], 'synthetic maximum protected state')
    large_feedback(d, entry)
    source.data[1].extend(message(i, '🧑'*4000) for i in range(3, 59))
    expected = d.encode(d.preview(entry, now=NOW))
    def concurrent(kwargs, count):
        data = json.loads(kwargs['input'][0]['text'])
        assert len(data['facts']) == len(data['owner_read_only']) == 100
        assert all(f['text'] == '🧑'*800 for f in data['facts'])
        assert d.serialized_request_size(kwargs) <= d.MAX_REQUEST_BYTES
        if count == 2:
            d.edit(entry, 'correct', fact_id='f100', text='Concurrent protected correction')
        return {'facts': [], 'retire': []}
    llm = FakeLLM(callback=concurrent)
    assert run(d, llm, entry) == {'refreshed': 1}
    assert len(llm.calls) > 2 and llm.calls[0]['input'][0]['text'] == expected
    value = d.load(entry)
    assert len(value['facts']) == 100 and value['facts'][-1]['text'] == 'Concurrent protected correction'
    assert len(value['removed']) == 4000 and len(value['cursor']) == 58
    assert run(d, llm, entry) == {'unchanged': 1}


def test_correction_does_not_swallow_different_quoted_evidence_in_same_message(plugin, source):
    d = plugin._dossier_support
    first = 'Alice says the agreed budget is EUR 100.'
    second = 'Owner says the agreed budget is EUR 200.'
    entry = source.add(received=first + ' ' + second)
    run(d, FakeLLM({'facts': [fact(text=first, quote=first)], 'retire': []}), entry)
    d.edit(entry, 'correct', fact_id='f1', text='The finalized budget is EUR 200.')
    source.data[1].append(message(3, 'Any news?'))
    run(d, FakeLLM({'facts': [fact(text=second, quote=second)], 'retire': []}), entry)
    facts = d.load(entry)['facts']
    assert len(facts) == 2 and facts[0]['text'] == 'The finalized budget is EUR 200.'
    assert facts[1]['text'] == second and facts[1]['sources'][0]['quote'] == second
    assert facts[1]['section'] == 'contradictions' and facts[1]['status'] == 'active'
