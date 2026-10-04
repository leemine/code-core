# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Member table data access object."""

from typing import List, Optional

from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError

from openjiuwen.agent_teams.schema.status import (
    EXECUTION_TRANSITIONS,
    MEMBER_DEPARTED_STATUSES,
    MEMBER_TRANSITIONS,
    MEMBER_UNREACHABLE_STATUSES,
    ExecutionStatus,
    MemberMode,
    MemberStatus,
)
from openjiuwen.agent_teams.tools.database.engine import (
    DbSessions,
    get_current_time,
    retry_on_locked,
)
from openjiuwen.agent_teams.tools.member_options import (
    MemberWorktreeOptions,
    promote_member_fallback_model,
    set_member_worktree_options,
)
from openjiuwen.agent_teams.tools.models import TeamMember
from openjiuwen.core.common.logging import team_logger

from .record_authority import (
    MemberCommittedFacts,
    MemberRecordDenied,
    MemberRecordWrites,
    MemberWriteReceipt,
    member_record_stamp,
    record_values,
)

_DEPARTED_STATUS_VALUES: tuple[str, ...] = tuple(status.value for status in MEMBER_DEPARTED_STATUSES)
_UNREACHABLE_STATUS_VALUES: tuple[str, ...] = tuple(status.value for status in MEMBER_UNREACHABLE_STATUSES)

# The human-member family: avatar-backed (``human_agent``) and passive
# (``passive_human``). Spelled as literals to keep this module out of the
# ``schema.team`` import cycle, mirroring ``create_member``'s ``role``
# docstring convention. Every "human agent" probe below matches both — the
# two roles share the HITT lifecycle surfaces (sender validation, inbound
# callbacks, task locks); where they diverge, call ``is_passive_human``.
_HUMAN_MEMBER_ROLES: tuple[str, ...] = ("human_agent", "passive_human")


def _valid_predecessor_values(target, transitions) -> list[str]:
    """Return the status string values that may legally transition to ``target``.

    Inverts a ``{from: [to, ...]}`` transition table into "which from-states
    reach ``target``", so an update can be expressed as one CAS UPDATE with a
    ``status IN (...)`` guard instead of a SELECT + Python validation.
    """
    return [state.value for state, targets in transitions.items() if target in targets]


class MemberDao:
    """Data access object for the team_member table."""

    def __init__(self, sessions: DbSessions, *, record_writes: MemberRecordWrites | None = None) -> None:
        """Initialize member DAO with the shared read/write session provider."""
        self._sessions = sessions
        self._record_writes = record_writes or MemberRecordWrites(self, None)

    async def _write_guarded(self, bound, changes, *, valid_from=None, return_receipt=False):
        async with self._sessions.write() as session:
            op = bound.operation
            bound.check(bound.permit.expected_stamp, op.changes,
                        session=session, transaction=session.get_transaction())
            row = (await session.execute(select(TeamMember).where(
                TeamMember.member_name == op.member_name,
                TeamMember.team_name == op.team_name,
            ))).scalar_one_or_none()
            transaction = session.get_transaction()
            before = bound.authorize_row(row)
            bound.check(before, op.changes, session=session, transaction=transaction)
            if valid_from is not None and getattr(row, valid_from[0]) not in valid_from[1]:
                return False
            values = changes(row) if callable(changes) else changes
            if values is None:
                return False
            candidate = TeamMember(**dict(record_values(row)))
            for key, value in values.items():
                setattr(candidate, key, value)
            proposed = record_values(candidate)
            bound.check(before, proposed, session=session, transaction=transaction)
            stamp = bound.stamp(candidate, before)
            result = await session.execute(update(TeamMember).where(
                TeamMember.member_name == op.member_name,
                TeamMember.team_name == op.team_name,
                TeamMember.record_nonce == before.nonce,
                TeamMember.record_revision == before.revision,
                TeamMember.record_digest == before.digest,
                TeamMember.record_source_id == before.source_id,
            ).values(**dict(proposed), record_nonce=stamp.nonce,
                     record_source_id=stamp.source_id, record_revision=stamp.revision,
                     record_digest=stamp.digest))
            if result.rowcount != 1:
                return False
            await session.flush()
            bound.check(before, proposed, session=session, transaction=transaction)
            await session.commit()
            receipt = bound.committed(stamp, transaction, before, proposed)
            return receipt if return_receipt else True

    async def create_member(
        self,
        member_name: str,
        team_name: str,
        display_name: str,
        agent_card: str,
        status: str,
        *,
        role: str = "teammate",
        desc: Optional[str] = None,
        execution_status: Optional[str] = None,
        mode: str = MemberMode.BUILD_MODE.value,
        prompt: Optional[str] = None,
        options: Optional[str] = None,
        return_receipt: bool = False,
    ) -> bool | MemberWriteReceipt:
        """Create a new team member.

        Args:
            role: ``TeamRole`` enum value (``leader`` / ``teammate`` /
                ``human_agent``). Persisted so cold-recovery can rebuild
                the right runtime profile (tools / rails / prompt
                sections) without depending on the leader's in-memory
                roster. Defaults to ``"teammate"`` (the literal value
                of ``TeamRole.TEAMMATE``; spelled as a literal to keep
                this module out of the ``schema.team`` import cycle)
                because that matches the overwhelmingly common spawn
                path. HITT callers must pass
                ``role=TeamRole.HUMAN_AGENT.value`` explicitly.
            options: JSON object for extensible member configuration.
                Current shape: ``{"model_ref": {...},
                "fallback_model_ref": {...}, "cli_agent": "...",
                "worktree": {...}, "permissions_override": {...}}``.
        """
        self._record_writes.receipt_flag(return_receipt)
        changes = (("display_name", display_name), ("agent_card", agent_card), ("status", status),
                   ("role", role), ("desc", desc), ("execution_status", execution_status),
                   ("mode", mode), ("prompt", prompt), ("options", options))
        bound = self._record_writes.bind(self, "create", team_name, member_name, changes)
        async with self._sessions.write() as session:
            try:
                member = TeamMember(
                    member_name=member_name,
                    team_name=team_name,
                    display_name=display_name,
                    agent_card=agent_card,
                    status=status,
                    role=role,
                    desc=desc,
                    execution_status=execution_status,
                    mode=mode,
                    prompt=prompt,
                    options=options,
                    updated_at=get_current_time(),
                )
                if bound is not None:
                    bound.check(None, record_values(member), session=session, transaction=session.get_transaction())
                    existing = (await session.execute(select(TeamMember).where(
                        TeamMember.member_name == member_name, TeamMember.team_name == team_name,
                    ))).scalar_one_or_none()
                    bound.authorize_row(existing)
                    bound.check(None, record_values(member), session=session, transaction=session.get_transaction())
                    stamp = bound.stamp(member, None)
                session.add(member)
                await session.flush()
                transaction = session.get_transaction()
                if bound is not None:
                    bound.check(None, record_values(member), session=session, transaction=transaction)
                await session.commit()
                if bound is not None:
                    receipt = bound.committed(stamp, transaction, None, record_values(member))
                    if return_receipt:
                        return receipt
                team_logger.info("Member %s created", member_name)
                return True
            except IntegrityError:
                await session.rollback()
                team_logger.error(
                    "Failed to create member %s in team %s (duplicate name or missing team)",
                    member_name, team_name,
                )
                return False

    async def is_human_agent(self, team_name: str, member_name: str) -> bool:
        """Return True if ``member_name`` is a human member (avatar or passive).

        Single-row probe (index-friendly) for the common case of
        checking one member's role without scanning the full roster.

        Role only — a member that has already left the team still answers
        True. Guards that must not fire for a departed member want
        :meth:`is_live_human_agent` instead. Callers that need to tell the
        avatar and passive flavors apart follow up with
        :meth:`is_passive_human`.
        """
        async with self._sessions.read() as session:
            stmt = select(TeamMember.member_name).where(
                TeamMember.team_name == team_name,
                TeamMember.member_name == member_name,
                TeamMember.role.in_(_HUMAN_MEMBER_ROLES),
            )
            return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def is_passive_human(self, team_name: str, member_name: str) -> bool:
        """Return True if ``member_name`` is a passive human member.

        Companion probe to :meth:`is_human_agent` for the branches where the
        two human flavors diverge (tool-call passthrough eligibility,
        avatar-driving refusal, role lookup for rendering): only the
        passive role has no avatar, so only it may act through the
        passthrough executor.
        """
        async with self._sessions.read() as session:
            stmt = select(TeamMember.member_name).where(
                TeamMember.team_name == team_name,
                TeamMember.member_name == member_name,
                TeamMember.role == "passive_human",
            )
            return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def _is_human_agent_excluding(
        self,
        team_name: str,
        member_name: str,
        excluded: tuple[str, ...],
    ) -> bool:
        """Single-row human-member probe with a status exclusion applied."""
        async with self._sessions.read() as session:
            stmt = select(TeamMember.member_name).where(
                TeamMember.team_name == team_name,
                TeamMember.member_name == member_name,
                TeamMember.role.in_(_HUMAN_MEMBER_ROLES),
                TeamMember.status.notin_(excluded),
            )
            return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def _list_human_agents_excluding(self, team_name: str, excluded: tuple[str, ...]) -> list[str]:
        """Human-member roster with a status exclusion applied."""
        async with self._sessions.read() as session:
            stmt = select(TeamMember.member_name).where(
                TeamMember.team_name == team_name,
                TeamMember.role.in_(_HUMAN_MEMBER_ROLES),
                TeamMember.status.notin_(excluded),
            )
            return list((await session.execute(stmt)).scalars().all())

    async def is_live_human_agent(self, team_name: str, member_name: str) -> bool:
        """Return True if ``member_name`` is a human member still on the team.

        Covers both flavors (avatar and passive). Excludes
        ``MEMBER_DEPARTED_STATUSES``. The HITT task lock keys on this
        rather than on the bare role: the lock exists to stop the leader from
        stealing work out from under a live human, and a human the leader has
        already released is no longer there to do it.
        """
        return await self._is_human_agent_excluding(team_name, member_name, _DEPARTED_STATUS_VALUES)

    async def is_reachable_human_agent(self, team_name: str, member_name: str) -> bool:
        """Return True if ``member_name`` is a human member still reachable.

        Covers both flavors (avatar and passive). Excludes only
        ``MEMBER_UNREACHABLE_STATUSES`` — a member that merely has shutdown
        *requested* is still reachable, and must be, or the notice that
        it was removed would never reach its controller. Message delivery
        keys on this; work guards key on the stricter
        :meth:`is_live_human_agent`.
        """
        return await self._is_human_agent_excluding(team_name, member_name, _UNREACHABLE_STATUS_VALUES)

    async def list_human_agent_names(self, team_name: str) -> list[str]:
        """Return member names whose ``role`` is a human member (avatar or passive).

        Used by ``TeamBackend.human_agent_names()`` to enumerate all human
        members on the team. Role only — members on their way out or
        already gone are included; see :meth:`list_live_human_agent_names`
        and :meth:`list_reachable_human_agent_names`.
        """
        async with self._sessions.read() as session:
            stmt = select(TeamMember.member_name).where(
                TeamMember.team_name == team_name,
                TeamMember.role.in_(_HUMAN_MEMBER_ROLES),
            )
            return list((await session.execute(stmt)).scalars().all())

    async def list_live_human_agent_names(self, team_name: str) -> list[str]:
        """Return human member names (avatar or passive) that have not left the team.

        Batch counterpart of :meth:`is_live_human_agent`, used by the cancel-all
        path to skip the tasks held by humans still on the team while cancelling
        a departed human's leftovers like anyone else's.
        """
        return await self._list_human_agents_excluding(team_name, _DEPARTED_STATUS_VALUES)

    async def list_reachable_human_agent_names(self, team_name: str) -> list[str]:
        """Return human member names (avatar or passive) that can still be delivered to.

        Batch counterpart of :meth:`is_reachable_human_agent`, used to fan a
        broadcast out to human controllers.
        """
        return await self._list_human_agents_excluding(team_name, _UNREACHABLE_STATUS_VALUES)

    async def get_member_status(self, team_name: str, member_name: str) -> Optional[str]:
        """Return one member's ``status``, or None when it does not exist.

        Narrow projection: the coordination layer consults a member's status on
        every mailbox drain to decide whether its harness may be fed, and pulling
        the whole row (serialized card, private prompt, options JSON) for one
        column would be pure waste on that path.
        """
        async with self._sessions.read() as session:
            stmt = select(TeamMember.status).where(
                TeamMember.team_name == team_name,
                TeamMember.member_name == member_name,
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    async def member_exists(self, member_name: str, team_name: str) -> bool:
        """Check whether a member row exists, without loading it.

        An ``EXISTS`` probe for callers that only need presence (e.g.
        recipient validation) instead of ``get_member``, which loads the
        full row (``agent_card`` / ``prompt`` / ``options``).

        Args:
            member_name: Member identifier.
            team_name: Team identifier.

        Returns:
            True when a matching member row exists.
        """
        async with self._sessions.read() as session:
            stmt = select(
                exists().where(
                    TeamMember.member_name == member_name,
                    TeamMember.team_name == team_name,
                )
            )
            return bool((await session.execute(stmt)).scalar())

    async def read_committed_member(self, receipt: MemberWriteReceipt) -> MemberCommittedFacts:
        """Compare an original transaction receipt with its current writer row.

        This is a pure fact query, never current read permission. The caller
        must separately authorize its parent/entity and final delivery. It
        neither restores the old ExecutionOrigin nor calls its authorizer.
        The returned original immutable facts may contain private content and
        must not be emitted wholesale. No fence extends beyond this query.
        """
        if type(receipt) is not MemberWriteReceipt:
            raise MemberRecordDenied("original member receipt required")
        facts = receipt.committed_facts()
        guard, sessions = self._record_writes, self._sessions
        references = facts._database_references
        database = facts.database

        def check(session=None):
            receipt.check_integrity()
            if (self._record_writes is not guard or self._sessions is not sessions
                    or guard.database is not database or database.member is not self
                    or facts.dao is not self or facts.sessions is not sessions
                    or not references or facts.kind == "delete" or guard.references(self) != references
                    or (session is not None and not guard.transaction_matches(session))):
                raise MemberRecordDenied("original member database changed")

        check()
        # The original writer session is intentional: a read replica or a
        # replaceable read factory cannot attest to this committed database.
        async with sessions.write() as session:
            check(session)
            row = (await session.execute(select(TeamMember).where(
                TeamMember.member_name == facts.member_name,
                TeamMember.team_name == facts.team_name,
            ))).scalar_one_or_none()
            check(session)
            if (row is None or member_record_stamp(row) != facts.stamp
                    or record_values(row) != facts.record):
                raise MemberRecordDenied("member receipt does not match current record")
        check()
        return facts

    async def get_member(self, member_name: str, team_name: str) -> Optional[TeamMember]:
        """Get member information by ID."""
        async with self._sessions.read() as session:
            result = await session.execute(
                select(TeamMember).where(
                    TeamMember.member_name == member_name,
                    TeamMember.team_name == team_name,
                )
            )
            return result.scalar_one_or_none()

    async def get_team_members(self, team_name: str, status: str | None = None) -> List[TeamMember]:
        """Get members for a team, optionally filtered by status.

        Args:
            team_name: Team identifier.
            status: If provided, only return members with this status.
        """
        async with self._sessions.read() as session:
            stmt = select(TeamMember).where(TeamMember.team_name == team_name)
            if status is not None:
                stmt = stmt.where(TeamMember.status == status)
            return (await session.execute(stmt)).scalars().all()

    async def get_member_roster(self, team_name: str) -> List[tuple[str, str, str]]:
        """Get a projected roster of ``(member_name, display_name, status)``.

        A column projection instead of ``select(TeamMember)`` so the roster
        view never loads the heavy columns (``agent_card`` / ``prompt`` /
        ``options``) it does not render.

        Args:
            team_name: Team identifier.

        Returns:
            One ``(member_name, display_name, status)`` tuple per member.
        """
        async with self._sessions.read() as session:
            stmt = select(
                TeamMember.member_name,
                TeamMember.display_name,
                TeamMember.status,
            ).where(TeamMember.team_name == team_name)
            rows = (await session.execute(stmt)).all()
            return [(row.member_name, row.display_name, row.status) for row in rows]

    async def get_members_max_updated_at(self, team_name: str) -> int:
        """Probe MAX(``team_member.updated_at``) for the team.

        Args:
            team_name: Team identifier.

        Returns:
            Largest member update timestamp (ms), or ``0`` when no
            members exist or all rows have null ``updated_at``.
        """
        async with self._sessions.read() as session:
            result = await session.execute(
                select(func.max(TeamMember.updated_at)).where(TeamMember.team_name == team_name)
            )
            value = result.scalar_one_or_none()
            return int(value) if value is not None else 0

    async def update_member_status(
        self,
        member_name: str,
        team_name: str,
        status: str,
        *, return_receipt: bool = False,
    ) -> bool | MemberWriteReceipt:
        """Update member status via a single guarded CAS UPDATE.

        The transition validation lives in the ``WHERE status IN (valid
        predecessors)`` clause, so the happy path is one UPDATE — no SELECT
        held inside the write lock. Only the rare rowcount=0 (failure) path
        reads back the row to log whether the member was missing or the
        transition was illegal.
        """
        valid_from = _valid_predecessor_values(MemberStatus(status), MEMBER_TRANSITIONS)
        self._record_writes.receipt_flag(return_receipt)
        bound = self._record_writes.bind(self, "status", team_name, member_name, (("status", status),))
        if bound is not None:
            return await self._write_guarded(bound, {"status": status},
                                             valid_from=("status", valid_from), return_receipt=return_receipt)

        async def _op() -> bool:
            async with self._sessions.write() as session:
                result = await session.execute(
                    update(TeamMember)
                    .where(
                        TeamMember.member_name == member_name,
                        TeamMember.team_name == team_name,
                        *self._record_writes.legacy_where(TeamMember),
                        TeamMember.status.in_(valid_from),
                    )
                    .values(status=status)
                )
                if result.rowcount == 1:
                    await session.commit()
                    team_logger.debug("Member %s status updated to %s", member_name, status)
                    return True

                await self._log_member_update_rejection(
                    session, member_name, team_name, TeamMember.status, status
                )
                return False

        # Status writes sit on the hot path of every kernel.start / spawn
        # transition; a transient SQLite file-lock wait or a temporarily
        # exhausted write pool must retry, not crash the member.
        return await retry_on_locked(
            _op,
            on_locked_result=False,
            label=f"update_member_status {member_name}",
        )

    async def _log_member_update_rejection(
        self,
        session,
        member_name: str,
        team_name: str,
        column,
        target: str,
    ) -> None:
        """Log the reason a guarded member update matched no row (failure path).

        Reads the PK (to distinguish a missing member from a legal-but-rejected
        transition) plus the current value of ``column`` (which may itself be
        NULL, e.g. ``execution_status``) for the invalid-transition message.
        """
        existing = await session.execute(
            select(TeamMember.member_name, column).where(
                TeamMember.member_name == member_name,
                TeamMember.team_name == team_name,
            )
        )
        row = existing.first()
        if row is None:
            team_logger.error("Member %s not found in team %s", member_name, team_name)
        else:
            team_logger.error(
                "Invalid state transition for member %s: %s -> %s",
                member_name,
                row[1],
                target,
            )

    async def try_transition_member_status(
        self,
        member_name: str,
        team_name: str,
        from_status: MemberStatus,
        to_status: MemberStatus,
        *, return_receipt: bool = False,
    ) -> bool | MemberWriteReceipt:
        """Atomically transition member status from from_status to to_status.

        Uses a single UPDATE with WHERE status = from_status so only
        one concurrent caller can succeed (rowcount=1). The database
        transaction ensures atomicity; if the WHERE clause no longer
        matches, rowcount=0 and the method returns False.

        Args:
            member_name: The member whose status to transition.
            team_name: The team the member belongs to.
            from_status: The expected current status (must match).
            to_status: The target status.

        Returns:
            True if the transition succeeded, False otherwise.
        """
        self._record_writes.receipt_flag(return_receipt)
        bound = self._record_writes.bind(self, "transition_status", team_name, member_name,
                                        (("from_status", from_status.value), ("status", to_status.value)))
        if bound is not None:
            return await self._write_guarded(bound, {"status": to_status.value},
                valid_from=("status", [from_status.value]), return_receipt=return_receipt)
        async with self._sessions.write() as session:
            result = await session.execute(
                update(TeamMember)
                .where(
                    TeamMember.member_name == member_name,
                    TeamMember.team_name == team_name,
                    *self._record_writes.legacy_where(TeamMember),
                    TeamMember.status == from_status.value,
                )
                .values(status=to_status.value)
            )
            await session.commit()
            transitioned = result.rowcount == 1
            if not transitioned:
                team_logger.debug(
                    "CAS %s -> %s for member %s failed (rowcount=%s)",
                    from_status.value,
                    to_status.value,
                    member_name,
                    result.rowcount,
                )
            return transitioned

    async def update_member_execution_status(
        self,
        member_name: str,
        team_name: str,
        execution_status: str,
        *, return_receipt: bool = False,
    ) -> bool | MemberWriteReceipt:
        """Update member execution status via a single guarded CAS UPDATE.

        Mirror of ``update_member_status``: the transition validation is the
        ``WHERE execution_status IN (valid predecessors)`` guard, so the happy
        path is one UPDATE with no in-lock SELECT; only rowcount=0 reads back
        to log the precise rejection reason.
        """
        valid_from = _valid_predecessor_values(ExecutionStatus(execution_status), EXECUTION_TRANSITIONS)
        self._record_writes.receipt_flag(return_receipt)
        bound = self._record_writes.bind(self,
            "execution_status", team_name, member_name, (("execution_status", execution_status),))
        if bound is not None:
            return await self._write_guarded(bound, {"execution_status": execution_status},
                                             valid_from=("execution_status", valid_from), return_receipt=return_receipt)
        async with self._sessions.write() as session:
            result = await session.execute(
                update(TeamMember)
                .where(
                    TeamMember.member_name == member_name,
                    TeamMember.team_name == team_name,
                        *self._record_writes.legacy_where(TeamMember),
                    TeamMember.execution_status.in_(valid_from),
                )
                .values(execution_status=execution_status)
            )
            if result.rowcount == 1:
                await session.commit()
                team_logger.debug("Member %s execution status updated to %s", member_name, execution_status)
                return True

            await self._log_member_update_rejection(
                session, member_name, team_name, TeamMember.execution_status, execution_status
            )
            return False

    async def reset_member_execution_status(
        self,
        member_name: str,
        team_name: str,
        execution_status: str,
        *, return_receipt: bool = False,
    ) -> bool | MemberWriteReceipt:
        """Reset member execution status without predecessor checks.

        This is intentionally NOT a normal state-machine transition; it is
        used only during recovery/restart when the previous execution context
        has been cleaned up and the member is about to begin a brand-new task
        lifecycle. Skipping the predecessor guard allows RUNNING/STARTING/etc.
        to be forced back to IDLE, eliminating illegal-transition noise like
        ``RUNNING -> STARTING`` on restart (issue #4318).
        """
        self._record_writes.receipt_flag(return_receipt)
        bound = self._record_writes.bind(self,
            "reset_execution_status", team_name, member_name, (("execution_status", execution_status),))
        if bound is not None:
            return await self._write_guarded(bound, {"execution_status": execution_status},
                                             valid_from=None, return_receipt=return_receipt)
        async with self._sessions.write() as session:
            result = await session.execute(
                update(TeamMember)
                .where(
                    TeamMember.member_name == member_name,
                    TeamMember.team_name == team_name,
                        *self._record_writes.legacy_where(TeamMember),
                )
                .values(execution_status=execution_status)
            )
            if result.rowcount == 1:
                await session.commit()
                team_logger.debug(
                    "Member %s execution status reset to %s", member_name, execution_status
                )
                return True

            team_logger.warning(
                "Failed to reset execution status for member %s: rowcount=%s",
                member_name,
                result.rowcount,
            )
            return False

    async def update_member_worktree(
        self,
        member_name: str,
        team_name: str,
        worktree: MemberWorktreeOptions | None = None,
        *,
        isolation: Optional[str] = None,
        worktree_path: Optional[str] = None,
        return_receipt: bool = False,
    ) -> bool | MemberWriteReceipt:
        """Update worktree isolation metadata for a member."""
        self._record_writes.receipt_flag(return_receipt)
        frozen_worktree = worktree.model_dump_json() if worktree is not None else None
        bound = self._record_writes.bind(self, "worktree", team_name, member_name,
            (("worktree", frozen_worktree), ("isolation", isolation), ("worktree_path", worktree_path)))
        if bound is not None:
            def changes(row):
                original = MemberWorktreeOptions.model_validate_json(frozen_worktree) if frozen_worktree else None
                return {"options": set_member_worktree_options(
                    row.options, original, isolation=isolation, worktree_path=worktree_path)}
            return await self._write_guarded(bound, changes, return_receipt=return_receipt)
        async with self._sessions.write() as session:
            result = await session.execute(
                select(TeamMember).where(
                    TeamMember.member_name == member_name,
                    TeamMember.team_name == team_name,
                )
            )
            member = result.scalar_one_or_none()
            if not member:
                team_logger.error("Member %s not found in team %s", member_name, team_name)
                return False
            self._record_writes.require_legacy(member)
            member.options = set_member_worktree_options(
                member.options,
                worktree,
                isolation=isolation,
                worktree_path=worktree_path,
            )
            await session.commit()
            return True

    async def promote_member_fallback_model(
        self,
        member_name: str,
        team_name: str,
        *, return_receipt: bool = False,
    ) -> bool | MemberWriteReceipt:
        """Promote the persisted fallback model to the active model reference."""
        self._record_writes.receipt_flag(return_receipt)
        bound = self._record_writes.bind(self, "promote_fallback", team_name, member_name)
        if bound is not None:
            def changes(row):
                promoted = promote_member_fallback_model(row.options)
                return None if promoted == row.options else {"options": promoted}
            return await self._write_guarded(bound, changes, return_receipt=return_receipt)
        async with self._sessions.write() as session:
            result = await session.execute(
                select(TeamMember).where(
                    TeamMember.member_name == member_name,
                    TeamMember.team_name == team_name,
                )
            )
            member = result.scalar_one_or_none()
            if member is None:
                team_logger.error("Member %s not found in team %s", member_name, team_name)
                return False
            self._record_writes.require_legacy(member)
            promoted = promote_member_fallback_model(member.options)
            if promoted == member.options:
                return False
            member.options = promoted
            await session.commit()
            return True
