"""Synthetic correct-behavior regressions for the two issue #17 re-review defects."""

import copy
import json

import pytest

from test_dossiers import FakeLLM, fact, message, run
from test_dossiers import plugin as plugin_fixture, source as source_fixture

plugin = plugin_fixture
source = source_fixture


@pytest.mark.parametrize("action,wording,explicit_id,accepted", [
    ("keep", "unrelated", False, True),
    ("remove", "unrelated", False, True),
    ("correct_remove", "unrelated", False, True),
    ("remove", "original", False, False),
    ("correct_remove", "original", False, False),
    ("correct_remove", "corrected", False, False),
    ("keep", "unrelated", True, False),
    ("remove", "unrelated", True, False),
])
def test_provisional_id_collision_rechecks_latest_owner_policy(
    plugin, source, action, wording, explicit_id, accepted
):
    d = plugin._dossier_support
    entry = source.add()
    assert run(d, FakeLLM({"facts": [], "retire": []}), entry) == {"refreshed": 1}
    evidence = "I will deliver the report on Friday."
    source.data[1].append(message(3, evidence, "inbound"))
    original = "Use short greetings."
    corrected = "Always spell out the project name in the greeting."
    generated = {
        "unrelated": "Contact promised to deliver the report on Friday.",
        "original": original,
        "corrected": corrected,
    }[wording]

    def feedback(kwargs, count):
        d.edit(entry, "note", section="preferences", text=original)
        if action == "correct_remove":
            d.edit(entry, "correct", fact_id="f1", text=corrected)
        if action != "keep":
            d.edit(entry, "remove", fact_id="f1")
        patch = fact(ref="m3", quote=evidence, text=generated, section="commitments")
        if explicit_id:
            # f1 did not exist in the model snapshot: an explicit patch must
            # never be treated as a new fact merely because it now collides.
            patch["id"] = "f1"
        return {"facts": [patch], "retire": ["f1"]}

    assert run(d, FakeLLM(callback=feedback), entry) == {"refreshed": 1}
    value = d.load(entry)
    expected = [("f1", original)] if action == "keep" else []
    if accepted:
        expected.append(("f2", generated))
    assert [(f["id"], f["text"]) for f in value["facts"]] == expected
    if accepted:
        added = value["facts"][-1]
        assert added["sources"][0] == {k: v for k, v in {
            **source.data[1][-1], "quote": evidence,
        }.items() if k != "text"}
        assert added["first_revision"] == added["last_revision"] == value["revision"]
    if action != "keep":
        assert value["removed"][0]["id"] == "f1"
        assert value["removed"][0]["text"] == (corrected if action == "correct_remove" else original)
        if action == "correct_remove":
            assert value["removed"][0]["previous_texts"] == [original]
    assert "m3" in value["cursor"]
    unchanged = FakeLLM()
    assert run(d, unchanged, entry) == {"unchanged": 1}
    assert not unchanged.calls


@pytest.mark.parametrize("correct_first", [False, True])
def test_concurrent_stable_removal_blocks_dissimilar_same_id_patch(plugin, source, correct_first):
    d = plugin._dossier_support
    entry = source.add()
    run(d, FakeLLM(), entry)
    source.data[1].append(message(3, "The delivery arrives on Friday.", "inbound"))

    def feedback(kwargs, count):
        if correct_first:
            d.edit(entry, "correct", fact_id="f1", text="The owner cancelled the meeting.")
        d.edit(entry, "remove", fact_id="f1")
        return {"facts": [fact(id="f1", ref="m3", quote="The delivery arrives on Friday.",
                               text="Contact expects a delivery on Friday.")], "retire": []}

    assert run(d, FakeLLM(callback=feedback), entry) == {"refreshed": 1}
    value = d.load(entry)
    assert not value["facts"]
    assert value["removed"][0]["id"] == "f1"


def review_candidate(d, source):
    entry = source.add()
    run(d, FakeLLM(), entry)
    d.edit(entry, "correct", fact_id="f1", text="Meeting cancelled; do not arrange it.")
    source.data[1].append(message(3, "Any news?"))
    run(d, FakeLLM({"facts": [fact(text="They plan to get together on Tue.")], "retire": []}), entry)
    candidate = d.load(entry)["facts"][-1]
    assert candidate["id"] == "f2" and candidate["status"] == "owner review"
    assert candidate["owner_review"] == ["f1"] and not d.protected(candidate)
    return entry


@pytest.mark.parametrize("concurrent", [False, True])
@pytest.mark.parametrize("change", ["edit", "prune", "metadata", "retire", "supported"])
def test_owner_review_keeps_source_validity_and_retirement_rules(plugin, source, concurrent, change):
    d = plugin._dossier_support
    entry = review_candidate(d, source)
    if change in {"edit", "supported"}:
        source.data[1][0]["text"] = "The flight leaves at noon."
    elif change == "prune":
        source.data[1].pop(0)
    elif change == "metadata":
        source.data[1][0]["deleted"] = True
    else:
        source.data[1].append(message(4, "A neutral update."))

    path = d.directory(entry) / "dossier.json"
    before = path.read_bytes()
    shown = d.inspect_dossier(entry, "f2")["fact"]
    assert shown["owner_review"] == ["f1"]
    assert shown["status"] == ("unsupported" if change == "prune" else
                               "owner review" if change == "retire" else "stale")
    assert path.read_bytes() == before
    if change == "edit":
        reconciled = copy.deepcopy(d.load(entry))
        d.reconcile(reconciled, source.data[1], refresh=True)
        assert reconciled["facts"][-1]["status"] == "stale"
        assert reconciled["facts"][-1]["owner_review"] == ["f1"]

    response = {"facts": [], "retire": ["f2"] if change == "retire" else []}
    if change == "supported":
        response["facts"] = [fact(id="f2", quote="The flight leaves at noon.", text="Flight departs at noon.")]

    def feedback(kwargs, count):
        if concurrent:
            d.edit(entry, "note", section="preferences", text="Use short greetings.")
        return response

    llm = FakeLLM(callback=feedback)
    assert run(d, llm, entry) == {"refreshed": 1}
    value = d.load(entry)
    by_id = {f["id"]: f for f in value["facts"]}
    assert by_id["f1"]["text"] == "Meeting cancelled; do not arrange it."
    assert d.protected(by_id["f1"])
    assert ("f2" in by_id) == (change in {"metadata", "supported"})
    if change == "metadata":
        assert by_id["f2"]["status"] == "owner review"
        assert by_id["f2"]["owner_review"] == ["f1"]
        assert by_id["f2"]["sources"][0]["deleted"] is True
    elif change == "supported":
        assert by_id["f2"]["text"] == "Flight departs at noon."
        assert by_id["f2"]["status"] == "active"
        assert by_id["f2"]["first_revision"] == 3
    else:
        changes = d.revision_log(entry, value)[-1]["changes"]
        expected_action = "retired" if change == "retire" else "invalidated"
        assert any(e["id"] == "f2" and e["action"] == expected_action for e in changes)
    if concurrent:
        assert by_id["f3"]["text"] == "Use short greetings."
    if change in {"edit", "supported"}:
        assert json.loads(llm.calls[0]["input"][0]["text"])["messages"][0]["text"] == "The flight leaves at noon."
    unchanged = FakeLLM()
    assert run(d, unchanged, entry) == {"unchanged": 1}
    assert not unchanged.calls


@pytest.mark.parametrize("protection", ["pin", "correct"])
@pytest.mark.parametrize("change", ["edit", "prune"])
def test_protected_review_candidate_survives_without_model_support(plugin, source, protection, change):
    d = plugin._dossier_support
    entry = review_candidate(d, source)
    d.edit(entry, protection, fact_id="f2", **({"text": "Owner wording."} if protection == "correct" else {}))
    expected = d.load(entry)["facts"][-1]["text"]
    if change == "edit":
        source.data[1][0]["text"] = "The flight leaves at noon."
    else:
        source.data[1].pop(0)
    assert run(d, FakeLLM({"facts": [], "retire": ["f1", "f2"]}), entry) == {"refreshed": 1}
    candidate = next(f for f in d.load(entry)["facts"] if f["id"] == "f2")
    assert candidate["text"] == expected and d.protected(candidate)
    assert candidate["status"] == ("stale" if change == "edit" else "source gone")
