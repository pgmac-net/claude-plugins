"""Approval requests: who can answer, expiry, single use, and the record before the answer."""

from __future__ import annotations

import asyncio

import pytest

from fakes import FakeChat, settle
from sre_gateway.approvals import ApprovalBroker, Decision
from sre_gateway.chat import Click, Place

APPROVER = "U" + "0APPROVE1"
RESPONDER = "U" + "0RESPOND1"
TOKEN = "xoxb-" + "123456789012-abcdefghijkl"


def broker(chat, records, ttl=60, fail=False):
    def record(decision: Decision) -> None:
        if fail:
            raise OSError("disk full")
        records.append(decision)

    return ApprovalBroker(
        chat, approvers=frozenset({APPROVER}), ttl_seconds=ttl, record=record, scrub=lambda t: t.replace(TOKEN, "[R]")
    )


async def ask(b, command="kubectl rollout restart deploy/web", incident="incidents-7"):
    return await b.ask(
        incident=incident,
        place=Place("C1"),
        tool="Bash",
        tool_input={"command": command},
        tool_use_id="toolu_1",
        statement_link="https://chat.example.com/x",
    )


async def pending_request(chat, b, **kw):
    task = asyncio.create_task(ask(b, **kw))
    await settle()
    return task, chat.approvals[-1][1]


async def test_approver_click_allows_and_is_recorded_first():
    chat, records = FakeChat(), []
    b = broker(chat, records)
    task, view = await pending_request(chat, b)
    await b.on_click(Click(view.request_id, APPROVER, "approve"))
    answer = await task
    assert answer.allow
    assert records[0].outcome == "approved"
    assert records[0].approver == APPROVER
    assert records[0].tool_use_id == "toolu_1"
    assert records[0].link.startswith("https://chat.example.com/")
    assert "Approved" in chat.closed[view.request_id]


async def test_non_approver_cannot_approve_and_request_stays_open():
    chat, records = FakeChat(), []
    b = broker(chat, records)
    task, view = await pending_request(chat, b)
    await b.on_click(Click(view.request_id, RESPONDER, "approve"))
    await settle()
    assert not task.done()
    assert records == []
    assert any("Only the approvers" in text for _user, text in chat.told)
    await b.on_click(Click(view.request_id, APPROVER, "deny", "not now"))
    answer = await task
    assert not answer.allow
    assert "not now" in answer.message
    assert records[0].outcome == "denied"
    assert records[0].reason == "not now"


async def test_expired_request_is_refused_recorded_and_closed():
    chat, records = FakeChat(), []
    b = broker(chat, records, ttl=0.05)
    task, view = await pending_request(chat, b)
    answer = await task
    assert not answer.allow
    assert "expired" in answer.message
    assert records[0].outcome == "expired"
    assert records[0].approver is None
    assert "Expired" in chat.closed[view.request_id]


async def test_click_after_expiry_does_nothing():
    chat, records = FakeChat(), []
    b = broker(chat, records, ttl=0.05)
    task, view = await pending_request(chat, b)
    await task
    await b.on_click(Click(view.request_id, APPROVER, "approve"))
    assert [r.outcome for r in records] == ["expired"]


async def test_request_is_single_use():
    chat, records = FakeChat(), []
    b = broker(chat, records)
    task, view = await pending_request(chat, b)
    await b.on_click(Click(view.request_id, APPROVER, "deny"))
    await b.on_click(Click(view.request_id, APPROVER, "approve"))
    answer = await task
    assert not answer.allow
    assert [r.outcome for r in records] == ["denied"]


async def test_no_approval_before_the_incident_exists():
    chat, records = FakeChat(), []
    answer = await ask(broker(chat, records), incident=None)
    assert not answer.allow
    assert "incident record first" in answer.message
    assert chat.approvals == []


async def test_failed_record_refuses_the_call():
    chat, records = FakeChat(), []
    b = broker(chat, records, fail=True)
    task, view = await pending_request(chat, b)
    await b.on_click(Click(view.request_id, APPROVER, "approve"))
    answer = await task
    assert not answer.allow
    assert "could not be recorded" in answer.message


async def test_request_text_is_scrubbed():
    chat, records = FakeChat(), []
    b = broker(chat, records)
    task, view = await pending_request(chat, b, command=f"curl -H 'x: {TOKEN}' https://x")
    assert TOKEN not in view.summary
    await b.on_click(Click(view.request_id, APPROVER, "deny", f"leaked {TOKEN}"))
    answer = await task
    assert TOKEN not in answer.message
    assert TOKEN not in chat.closed[view.request_id]


@pytest.mark.parametrize("action", ["approve", "deny"])
async def test_unknown_request_click_is_ignored(action):
    chat, records = FakeChat(), []
    await broker(chat, records).on_click(Click("nope", APPROVER, action))
    assert records == []


async def test_a_call_too_long_to_show_in_full_is_refused_unposted():
    chat, records = FakeChat(), []
    answer = await ask(broker(chat, records), command="echo " + "x" * 2000)
    assert not answer.allow
    assert "too long" in answer.message
    assert chat.approvals == []


async def test_description_is_kept_apart_from_what_runs():
    chat, records = FakeChat(), []
    b = broker(chat, records)
    task = asyncio.create_task(
        b.ask(
            incident="incidents-7",
            place=Place("C1"),
            tool="Bash",
            tool_input={"command": "touch x", "description": "only reads things"},
            tool_use_id="toolu_1",
            statement_link=None,
        )
    )
    await settle()
    view = chat.approvals[-1][1]
    assert view.summary == "touch x"
    assert view.description == "only reads things"
    await b.on_click(Click(view.request_id, APPROVER, "deny"))
    await task
