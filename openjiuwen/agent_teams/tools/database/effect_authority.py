"""Live admission before TeamBackend's existing synchronous workspace effects.

This is a host SPI, not record authority and not a path supplied by a tool.
The original member DAO must still independently authorize and CAS its write.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Callable

from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin, current_execution_origin

from .record_authority import (
    MemberRecordDenied,
    MemberWriteCommittedButUnconfirmed,
    MemberWriteReceipt,
    _LiveOnly,
    _sync,
)


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class MemberEffectOperation(_LiveOnly):
    database: object
    backend: object
    kind: str
    team_name: str
    member_name: str
    session_id: str | None
    values: tuple
    workspace_intent: tuple


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class MemberEffectPermit(_LiveOnly):
    operation: MemberEffectOperation
    origin: ExecutionOrigin
    entity: object
    actor: object
    source_id: str
    check_current: Callable[[], None] = field(repr=False)
    on_committed: Callable[[MemberWriteReceipt], None] = field(repr=False)


class BoundMemberEffect:
    """One original spawn operation; no independent registry or lifecycle."""

    def __init__(self, database, backend, operation, current_facts):
        self.database, self.backend, self.operation = database, backend, operation
        self._current_facts = current_facts
        self._busy = False
        self._invalid = False
        self._delivered = False
        self.permit = None
        self.origin = current_execution_origin()
        self.guard = database._member_record_writes
        self.authorizer = self.guard.authorizer
        self.callback = self.authorizer.bind_for_effect
        if type(self.origin) is not ExecutionOrigin or not callable(self.callback):
            raise MemberRecordDenied("member workspace effects require original host admission")
        self._facts = self._snapshot()
        self.origin._check_current()
        self._fixed()
        permit = self.callback(operation, self.origin)
        if inspect.iscoroutine(permit):
            permit.close()
        self._fixed()
        if (
            type(permit) is not MemberEffectPermit
            or permit.operation is not operation
            or permit.origin is not self.origin
            or permit.entity is None
            or permit.actor is None
            or type(permit.source_id) is not str
            or not permit.source_id
            or len(permit.source_id) > 512
            or not callable(permit.check_current)
            or not callable(permit.on_committed)
        ):
            raise MemberRecordDenied("invalid member workspace effect permit")
        self.permit = permit
        self._facts = self._snapshot()
        self.check()

    def _snapshot(self):
        op, p = self.operation, self.permit
        return (
            self.guard.references(self.database.member),
            id(self.backend),
            id(self.backend.db),
            id(self.database._member_record_writes),
            id(self.authorizer),
            id(self.authorizer.bind_for_effect),
            id(self.origin),
            id(self.origin.host_value),
            id(self.origin._checker),
            id(op),
            id(op.database),
            id(op.backend),
            op.kind,
            op.team_name,
            op.member_name,
            op.session_id,
            op.values,
            op.workspace_intent,
            self._current_facts(),
            None
            if p is None
            else (
                id(p),
                id(p.operation),
                id(p.origin),
                id(p.entity),
                id(p.actor),
                p.source_id,
                id(p.check_current),
                id(p.on_committed),
            ),
        )

    def _fixed(self):
        if (
            self._invalid
            or current_execution_origin() is not self.origin
            or self._snapshot() != self._facts
            or self.backend.db is not self.database
        ):
            raise MemberRecordDenied("original member effect source changed")

    def check(self):
        if self._busy:
            self._invalid = True
            raise MemberRecordDenied("recursive member effect admission")
        self._busy = True
        try:
            self._fixed()
            self.origin._check_current()
            self._fixed()
            _sync(self.permit.check_current)
            self._fixed()
        finally:
            self._busy = False

    def committed(self, receipt):
        if type(receipt) is not MemberWriteReceipt:
            raise MemberRecordDenied("member create did not return its original receipt")
        try:
            if self._delivered or self._busy:
                self._invalid = True
                raise MemberRecordDenied("member effect receipt cannot be replayed")
            self.check()
            receipt.check_current()
            self._fixed()
            op = receipt.operation
            if (
                op.database is not self.database
                or op.kind != "create"
                or op.team_name != self.operation.team_name
                or op.member_name != self.operation.member_name
                or receipt._permit.origin is not self.origin
                or receipt._permit.entity is not self.permit.entity
                or receipt._permit.actor is not self.permit.actor
                or receipt._permit.source_id != self.permit.source_id
            ):
                raise MemberRecordDenied("member receipt does not belong to original effect")
            self._delivered = True
            self._busy = True
            try:
                _sync(self.permit.on_committed, receipt)
            finally:
                self._busy = False
            self.check()
            receipt.check_current()
            self._fixed()
        except BaseException as exc:
            raise MemberWriteCommittedButUnconfirmed(receipt) from exc


async def require_legacy_team(database, team_name):
    """Reject existing governed/partial rows before entering old FS helpers."""
    from sqlalchemy import select

    from openjiuwen.agent_teams.tools.models import TeamMember

    guard, sessions = database._member_record_writes, database._sessions
    if guard.authorizer is not None:
        raise MemberRecordDenied("governed Team side effect requires explicit host support")
    references = guard.references(database.member)

    def fixed(session):
        if (
            database._member_record_writes is not guard
            or database._sessions is not sessions
            or guard.authorizer is not None
            or guard.references(database.member) != references
            or not guard.transaction_matches(session)
        ):
            raise MemberRecordDenied("original Team storage changed")

    # Decisions preceding filesystem effects must read the original writer.
    # A replica or retargeted read factory cannot attest absence of protected
    # rows in the database that the later DAO mutation actually addresses.
    async with sessions.write() as session:
        fixed(session)
        members = (await session.execute(select(TeamMember).where(TeamMember.team_name == team_name))).scalars().all()
        fixed(session)
        for member in members:
            guard.require_legacy(member)
