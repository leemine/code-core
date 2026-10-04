# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team table data access object."""

from typing import Optional

from sqlalchemy import delete, exists, select
from sqlalchemy.exc import IntegrityError

from openjiuwen.agent_teams.tools.database.engine import DbSessions, get_current_time
from openjiuwen.agent_teams.tools.models import Team, TeamMember
from openjiuwen.core.common.logging import team_logger
from openjiuwen.core.controller.schema.execution_origin import current_execution_origin

from .record_authority import MemberRecordDenied, MemberRecordWrites, record_values


class TeamDao:
    """Data access object for the team_info table."""

    def __init__(self, sessions: DbSessions, *, record_writes: MemberRecordWrites | None = None) -> None:
        """Initialize team DAO with the shared read/write session provider."""
        self._sessions = sessions
        self._record_writes = record_writes or MemberRecordWrites(self, None)

    async def create_team(
        self,
        team_name: str,
        display_name: str,
        leader_member_name: str,
        *,
        desc: Optional[str] = None,
        prompt: Optional[str] = None,
        dispatch_mode: str = "autonomous",
        enable_task_verification: bool = False,
    ) -> bool:
        """Create a new team.

        The optional columns are keyword-only: ``desc`` / ``prompt`` sit next to
        each other and ``dispatch_mode`` / ``enable_task_verification`` carry the
        effective per-instance capability choices made at ``build_team`` time
        (F_62, persisted so cold recovery restores them) — naming them at the
        call site is what keeps them from being swapped.
        """
        async with self._sessions.write() as session:
            try:
                ts = get_current_time()
                team = Team(
                    team_name=team_name,
                    display_name=display_name,
                    leader_member_name=leader_member_name,
                    desc=desc,
                    prompt=prompt,
                    dispatch_mode=dispatch_mode,
                    enable_task_verification=enable_task_verification,
                    created=ts,
                    updated_at=ts,
                )
                session.add(team)
                await session.commit()
                team_logger.info("Team %s created", team_name)
                return True
            except IntegrityError as e:
                await session.rollback()
                team_logger.error("Team %s already exists: %s", team_name, e)
                return False

    async def get_team(self, team_name: str) -> Optional[Team]:
        """Get team information by ID."""
        async with self._sessions.read() as session:
            result = await session.execute(select(Team).where(Team.team_name == team_name))
            return result.scalar_one_or_none()

    async def team_exists(self, team_name: str) -> bool:
        """Check whether a team row exists in the static table.

        Lighter than ``get_team`` when only existence is needed: selects the
        team name column instead of hydrating a full ORM object.
        """
        async with self._sessions.read() as session:
            result = await session.execute(
                select(Team.team_name).where(Team.team_name == team_name)
            )
            return result.scalar_one_or_none() is not None

    async def delete_team(self, team_name: str) -> bool:
        """Delete a team (cascade delete will remove related records).

        Returns:
            ``True`` if a row was deleted, ``False`` when the team did
            not exist. Callers that treat "missing" as success can map
            ``False`` themselves.
        """
        record_writes = self._record_writes
        authorizer = record_writes.authorizer
        original_origin = current_execution_origin()
        references = record_writes.references(self) if authorizer is not None else None

        def check_admission():
            if (self._record_writes is not record_writes or record_writes.authorizer is not authorizer
                    or (authorizer is not None and (current_execution_origin() is not original_origin
                        or record_writes.references(self) != references))):
                raise MemberRecordDenied("original team delete source changed")

        async with self._sessions.write() as session:
            check_admission()
            if authorizer is not None and not record_writes.transaction_matches(session):
                raise MemberRecordDenied("original team delete transaction database changed")
            result = await session.execute(select(Team).where(Team.team_name == team_name))
            team = result.scalar_one_or_none()
            if not team:
                team_logger.debug("Team %s not found for deletion", team_name)
                return False

            members = list((await session.execute(select(TeamMember).where(
                TeamMember.team_name == team_name,
            ))).scalars().all())
            transaction = session.get_transaction()
            check_admission()
            if authorizer is not None and not record_writes.transaction_matches(session):
                raise MemberRecordDenied("original team delete transaction database changed")
            bounds = []
            for member in members:
                check_admission()
                bound = record_writes.bind(self, "delete_team", team_name, member.member_name)
                if bound is None:
                    self._record_writes.require_legacy(member)
                else:
                    before = bound.authorize_row(member)
                    bound.check(before, record_values(member), session=session, transaction=transaction)
                    bounds.append((bound, before, record_values(member)))
            # Remove only the rows we checked. A concurrent replacement/new member
            # makes the final team DELETE fail instead of joining its cascade.
            for member in members:
                conditions = (self._record_writes.legacy_where(TeamMember)
                              if member.record_nonce is None else (
                                  TeamMember.record_nonce == member.record_nonce,
                                  TeamMember.record_revision == member.record_revision,
                                  TeamMember.record_source_id == member.record_source_id,
                                  TeamMember.record_digest == member.record_digest,
                              ))
                result = await session.execute(delete(TeamMember).where(
                    TeamMember.team_name == team_name,
                    TeamMember.member_name == member.member_name, *conditions,
                ))
                if result.rowcount != 1:
                    raise MemberRecordDenied("member changed before team deletion")
            for bound, before, proposed in bounds:
                bound.check(before, proposed, session=session, transaction=transaction)
            result = await session.execute(delete(Team).where(
                Team.team_name == team_name,
                ~exists().where(TeamMember.team_name == team_name),
            ))
            if result.rowcount != 1:
                raise MemberRecordDenied("team roster changed before deletion")
            await session.flush()
            check_admission()
            if authorizer is not None and not record_writes.transaction_matches(session):
                raise MemberRecordDenied("original team delete transaction database changed")
            for bound, before, proposed in bounds:
                bound.check(before, proposed, session=session, transaction=transaction)
            await session.commit()
            for bound, before, proposed in bounds:
                bound.committed(before, transaction, before, proposed)
            team_logger.info("Team %s deleted", team_name)
            return True

    async def get_team_updated_at(self, team_name: str) -> int:
        """Probe ``team_info.updated_at`` for change detection.

        Args:
            team_name: Team identifier.

        Returns:
            Last update timestamp (ms), or ``0`` when the row is
            missing or the column is null.
        """
        async with self._sessions.read() as session:
            result = await session.execute(select(Team.updated_at).where(Team.team_name == team_name))
            value = result.scalar_one_or_none()
            return int(value) if value is not None else 0
