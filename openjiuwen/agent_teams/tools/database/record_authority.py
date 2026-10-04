"""Live host authority for writes to the original member table.

Stamps are comparison data. Only the injected host callback authorizes a writer.
No source, actor, permit or committed receipt can be reconstructed from JSON.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import secrets
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from openjiuwen.core.controller.schema.execution_origin import (
    ExecutionOrigin,
    current_execution_origin,
)


class MemberRecordDenied(RuntimeError):
    """The original member write/source could not be verified."""


class MemberWriteCommittedButUnconfirmed(MemberRecordDenied):
    """The transaction committed, but its source cannot authorize a current ACK.

    Retain the original receipt for reconciliation; never replay the mutation
    automatically or treat this receipt as permission under a replacement owner.
    """

    def __init__(self, receipt):
        super().__init__("member transaction committed; current acknowledgement unconfirmed")
        self.receipt = receipt


class _LiveOnly:
    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        raise TypeError("member authority cannot be serialized")


def _sync(callback, *args):
    result = callback(*args)
    if inspect.iscoroutine(result):
        result.close()
    if result is not None:
        raise MemberRecordDenied("member authority must synchronously return None")


def _text(value):
    if type(value) is not str or not value or len(value) > 512:
        raise MemberRecordDenied("invalid member source identifier")
    return value


_FIELDS = (
    "team_name",
    "member_name",
    "display_name",
    "desc",
    "agent_card",
    "status",
    "execution_status",
    "mode",
    "role",
    "prompt",
    "options",
    "updated_at",
)
_STAMP_FIELDS = ("record_nonce", "record_source_id", "record_revision", "record_digest")


def record_values(member):
    """An immutable exact snapshot, including fields not shown by the monitor."""
    return tuple((name, getattr(member, name)) for name in _FIELDS)


def _stamp_facts(stamp):
    if stamp is None:
        return None
    if type(stamp) is not MemberRecordStamp:
        raise MemberRecordDenied("invalid expected member stamp")
    return (stamp.nonce, stamp.source_id, stamp.revision, stamp.digest)


def _digest(values, nonce, source, revision):
    encoded = json.dumps(
        [values, nonce, source, revision], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class MemberRecordStamp:
    nonce: str
    source_id: str
    revision: int
    digest: str


def member_record_stamp(member) -> MemberRecordStamp | None:
    """Validate persisted provenance; never create a signature during reading."""
    values = tuple(getattr(member, name, None) for name in _STAMP_FIELDS)
    if all(value is None for value in values):
        return None
    nonce, source, revision, digest = values
    if (
        type(nonce) is not str
        or len(nonce) != 64
        or any(c not in "0123456789abcdef" for c in nonce)
        or type(revision) is not int
        or not 1 <= revision < 2**63
        or type(digest) is not str
        or len(digest) != 64
    ):
        raise MemberRecordDenied("invalid member record stamp")
    _text(source)
    if digest != _digest(record_values(member), nonce, source, revision):
        raise MemberRecordDenied("member record content changed outside its transaction")
    return MemberRecordStamp(nonce, source, revision, digest)


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class MemberWriteOperation(_LiveOnly):
    database: object
    kind: str
    team_name: str
    member_name: str
    changes: tuple[tuple[str, object], ...]
    _dao: object = field(repr=False)
    _sessions: object = field(repr=False)


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class MemberWritePermit(_LiveOnly):
    """Issued by a host after checking its actual actor/entity, never by a client.

    ``expected_stamp`` must come from the original saved committed receipt, not
    from adopting the current database row. The callback revalidates original
    entity/actor references and this exact operation on every transaction check.
    """

    operation: MemberWriteOperation
    origin: ExecutionOrigin
    entity: object
    actor: object
    source_id: str
    expected_stamp: MemberRecordStamp | None
    check_current: Callable[[MemberRecordStamp | None, tuple], None] = field(repr=False)


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class MemberRecordAuthorizer(_LiveOnly):
    bind_for_write: Callable[[MemberWriteOperation, ExecutionOrigin], MemberWritePermit]
    bind_for_effect: Callable | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, eq=False, repr=False, init=False)
class MemberWriteReceipt(_LiveOnly):
    """Original successful transaction, for retention in the existing member slot."""

    operation: MemberWriteOperation
    stamp: MemberRecordStamp
    _permit: MemberWritePermit
    _transaction: object
    _source_check: Callable[[], None]
    _issued_facts: tuple

    def __init__(self):
        raise TypeError("receipts originate only from a committed member transaction")

    @classmethod
    def _committed(cls, operation, stamp, permit, transaction, source_check):
        result = object.__new__(cls)
        for key, value in (
            ("operation", operation),
            ("stamp", stamp),
            ("_permit", permit),
            ("_transaction", transaction),
            ("_source_check", source_check),
        ):
            object.__setattr__(result, key, value)
        object.__setattr__(result, "_issued_facts", result._facts())
        return result

    def _facts(self):
        return (
            id(self.operation),
            _stamp_facts(self.stamp),
            id(self._permit),
            id(self._transaction),
            id(self._source_check),
        )

    def check_current(self) -> None:
        """Recheck original live source before host retention/current delivery.

        A committed-but-unconfirmed receipt still fails this check after its
        source expires; its transaction fact alone is never new authorization.
        """
        if self._facts() != self._issued_facts:
            raise MemberRecordDenied("original committed receipt changed")
        _sync(self._source_check)
        if self._facts() != self._issued_facts:
            raise MemberRecordDenied("original committed receipt changed")


class _BoundWrite:
    def __init__(self, guard, operation, origin, permit):
        self.guard, self.operation, self.origin, self.permit = guard, operation, origin, permit
        self._facts = self._snapshot()
        self._checking = False
        self._invalid = False

    def _snapshot(self):
        p, op = self.permit, self.operation
        return (
            self.guard.references(op._dao),
            id(op._dao),
            id(op._sessions),
            id(self.guard.authorizer),
            id(self.guard.authorizer.bind_for_write),
            id(self.guard.database),
            id(getattr(self.guard.database, "_sessions", None)),
            id(self.origin.host_value),
            id(self.origin._checker),
            id(op),
            id(op.database),
            op.kind,
            op.team_name,
            op.member_name,
            op.changes,
            id(p),
            id(p.operation),
            id(p.origin),
            id(p.entity),
            id(p.actor),
            p.source_id,
            _stamp_facts(p.expected_stamp),
            id(p.check_current),
        )

    def check(self, before, proposed, *, session=None, transaction=None):
        before_facts = _stamp_facts(before)

        def fixed():
            if (
                self._invalid
                or self._snapshot() != self._facts
                or _stamp_facts(before) != before_facts
                or current_execution_origin() is not self.origin
                or getattr(self.guard.database, "_member_record_writes", self.guard) is not self.guard
                or (
                    session is not None
                    and (
                        session.get_transaction() is not transaction
                        or not self.guard.transaction_matches(session)
                    )
                )
            ):
                raise MemberRecordDenied("original member write changed")

        if self._checking:
            self._invalid = True
            raise MemberRecordDenied("recursive member authority")
        self._checking = True
        try:
            fixed()
            self.origin._check_current()
            fixed()
            _sync(self.permit.check_current, before, proposed)
            fixed()
        finally:
            self._checking = False

    def authorize_row(self, member):
        stamp = member_record_stamp(member) if member is not None else None
        p = self.permit
        if self.operation.kind == "create":
            if member is not None or p.expected_stamp is not None:
                raise MemberRecordDenied("member create cannot adopt a record")
        elif stamp is None or p.expected_stamp != stamp or p.source_id != stamp.source_id:
            raise MemberRecordDenied("original member record no longer matches")
        return stamp

    def stamp(self, member, before):
        nonce = secrets.token_hex(32) if before is None else before.nonce
        revision = 1 if before is None else before.revision + 1
        if revision >= 2**63:
            raise MemberRecordDenied("member revision exhausted")
        member.record_nonce = nonce
        member.record_source_id = self.permit.source_id
        member.record_revision = revision
        member.record_digest = _digest(record_values(member), nonce, self.permit.source_id, revision)
        return member_record_stamp(member)

    def committed(self, stamp, transaction, before, proposed):
        """Keep the confirmed transaction fact even if source changes during commit."""
        receipt = MemberWriteReceipt._committed(
            self.operation, stamp, self.permit, transaction, lambda: self.check(before, proposed)
        )
        try:
            receipt.check_current()
        except BaseException as exc:
            raise MemberWriteCommittedButUnconfirmed(receipt) from exc
        return receipt


class MemberRecordWrites:
    """A per-DB helper; no registry, queue or independent lock."""

    def __init__(self, database, authorizer):
        if authorizer is not None and type(authorizer) is not MemberRecordAuthorizer:
            raise TypeError("member authorizer must be a live MemberRecordAuthorizer")
        self.database, self.authorizer = database, authorizer

    def transaction_matches(self, session):
        """Check the actual ORM sinks, not only AsyncSession's default bind."""
        from openjiuwen.agent_teams.tools.models import Team, TeamMember

        engine = self.database.engine
        if (type(session) is not AsyncSession or type(session.sync_session) is not Session
                or session.bind is not engine or session.sync_session.bind is not engine.sync_engine):
            return False
        return all(
            session.sync_session.get_bind(mapper=model) is engine.sync_engine
            and session.sync_session.get_bind(clause=model.__table__) is engine.sync_engine
            for model in (Team, TeamMember)
        )

    def references(self, dao):
        """Pure checks of the actual DAO and DB before any host callback."""
        database = self.database
        sessions = getattr(database, "_sessions", None)
        factory = getattr(database, "session_local", None)
        # The original TeamDatabase uses one standard writer engine. Mapper
        # binds/custom Session classes could route writes elsewhere while the
        # factory object and its default bind remain unchanged.
        if (type(factory) is not async_sessionmaker or factory.class_ is not AsyncSession
                or type(factory.kw) is not dict
                or set(factory.kw) != {"bind", "autoflush", "expire_on_commit"}
                or factory.kw["autoflush"] is not False
                or factory.kw["expire_on_commit"] is not False):
            raise MemberRecordDenied("governed member requires the original single-engine session factory")
        if (
            sessions is None
            or dao._sessions is not sessions
            or dao._record_writes is not self
            or getattr(database, "_member_record_writes", None) is not self
            or sessions._write_session_local is not database.session_local
            or sessions._write_session_local.kw.get("bind") is not database.engine
        ):
            raise MemberRecordDenied("original member DAO/database mismatch")
        return (
            id(self),
            id(database),
            id(sessions),
            id(dao),
            id(self.authorizer),
            id(self.authorizer.bind_for_write),
            id(database.engine),
            id(database.session_local),
            id(sessions._write_session_local),
            id(sessions._write_lock),
        )

    def bind(self, dao, kind, team_name, member_name, changes=()):
        if self.authorizer is None:
            return None
        references = self.references(dao)
        origin = current_execution_origin()
        if not isinstance(origin, ExecutionOrigin):
            raise MemberRecordDenied("member write requires original execution source")
        if any(type(key) is not str or (value is not None and type(value) not in (str, int)) for key, value in changes):
            raise MemberRecordDenied("member operation must contain immutable scalar fields")
        operation = MemberWriteOperation(
            self.database, kind, _text(team_name), _text(member_name), tuple(changes), dao, dao._sessions
        )
        facts = (
            id(operation.database),
            operation.kind,
            operation.team_name,
            operation.member_name,
            operation.changes,
            id(operation._dao),
            id(operation._sessions),
        )
        authorizer, callback = self.authorizer, self.authorizer.bind_for_write
        source_facts = (id(origin.host_value), id(origin._checker))
        origin._check_current()
        if (
            references != self.references(dao)
            or source_facts != (id(origin.host_value), id(origin._checker))
            or current_execution_origin() is not origin
            or self.authorizer is not authorizer
            or authorizer.bind_for_write is not callback
        ):
            raise MemberRecordDenied("original member admission source changed")
        permit = callback(operation, origin)
        if inspect.iscoroutine(permit):
            permit.close()
        if (
            references != self.references(dao)
            or type(permit) is not MemberWritePermit
            or source_facts != (id(origin.host_value), id(origin._checker))
            or current_execution_origin() is not origin
            or permit.operation is not operation
            or permit.origin is not origin
            or permit.entity is None
            or permit.actor is None
            or self.authorizer is not authorizer
            or authorizer.bind_for_write is not callback
            or facts
            != (
                id(operation.database),
                operation.kind,
                operation.team_name,
                operation.member_name,
                operation.changes,
                id(operation._dao),
                id(operation._sessions),
            )
        ):
            raise MemberRecordDenied("invalid member write permit")
        _text(permit.source_id)
        bound = _BoundWrite(self, operation, origin, permit)
        bound.check(permit.expected_stamp, operation.changes)
        return bound

    @staticmethod
    def require_legacy(member):
        if any(getattr(member, name, None) is not None for name in _STAMP_FIELDS):
            raise MemberRecordDenied("governed member requires its original host authority")

    @staticmethod
    def legacy_where(model):
        return tuple(getattr(model, name).is_(None) for name in _STAMP_FIELDS)

    @staticmethod
    def receipt_flag(value):
        if type(value) is not bool:
            raise TypeError("return_receipt must be bool")
