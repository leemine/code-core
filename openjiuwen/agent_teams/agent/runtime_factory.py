# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host construction port; Team retains scheduling and member ownership."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from openjiuwen.agent_teams.external.member_runtime import ExternalHarnessMemberRuntime
    from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
    from openjiuwen.agent_teams.schema.team import TeamRuntimeContext
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard


TEAM_MEMBER_RUNTIME_FACTORY = "team.member_runtime_factory"


@dataclass(frozen=True, slots=True)
class TeamMemberRuntimeBuild:
    """One member's construction inputs, never serialized as a Spec."""

    spec: TeamAgentSpec
    context: TeamRuntimeContext
    card: AgentCard
    language: str
    team_mode: str
    team_backend: Any
    workspace_manager: Any
    model_allocator: Any
    messager: Any


@runtime_checkable
class TeamMemberRuntimeFactory(Protocol):
    """Build every LLM member using the explicitly selected Provider.

    Validation must allocate no resources. Construction returns an unstarted
    runtime; asynchronous allocation belongs in its start/context factory.
    """

    @property
    def provider_id(self) -> str: ...

    def validate_team_spec(self, spec: TeamAgentSpec) -> None: ...

    def build_member_runtime(self, request: TeamMemberRuntimeBuild) -> ExternalHarnessMemberRuntime: ...


@dataclass(frozen=True, slots=True)
class TeamReviewRuntimeBuild:
    """One scheduled review invocation, distinct from roster members/children.

    Tools and prompt come from the original scheduler. The host binds its
    selected Provider, permissions, single event consumer and root accounting.
    The optional output sink forwards projected chunks into the scheduler's
    owning Team stream, not the Session persistence stream. The host uses its
    direct Session stream only when no sink is supplied by a standalone owner.
    No runtime objects or invocation IDs are persisted as a Team Spec.
    """

    spec: TeamAgentSpec
    reviewer: str
    task_id: str
    review_round: int
    invocation_id: str
    system_prompt: str
    language: str
    tools: tuple[Any, ...]
    team_session: Any
    build_context: Any
    output_sink: Callable[[Any], Awaitable[None]] | None = None


@runtime_checkable
class TeamReviewRuntime(Protocol):
    """A one-shot execution; dispose confirms exit, including after cancellation.

    run_once must report unsuccessful/unknown execution by raising; it may
    not replay ambiguous inputs. dispose is retryable and retains ownership
    until native execution, its event consumer and transports have exited.
    """

    @property
    def provider_id(self) -> str: ...

    async def run_once(self, prompt: str) -> Any: ...

    async def dispose(self) -> None: ...


@runtime_checkable
class TeamReviewRuntimeFactory(Protocol):
    """Optional scheduled extension of the same selected member factory."""

    def build_review_runtime(self, request: TeamReviewRuntimeBuild) -> TeamReviewRuntime: ...


def require_member_runtime_factory(spec: TeamAgentSpec) -> TeamMemberRuntimeFactory | None:
    """Resolve only the host's existing carrier, with no fallback to Native."""
    if spec.execution_provider == "native":
        return None
    spec.materialize_build_context()
    extras = getattr(spec.build_context, "extras", {})
    factory = extras.get(TEAM_MEMBER_RUNTIME_FACTORY)
    if not isinstance(factory, TeamMemberRuntimeFactory):
        raise ValueError("External Team requires a host member runtime factory")
    if factory.provider_id != spec.execution_provider:
        raise ValueError("Team member runtime factory Provider does not match the Spec")
    from openjiuwen.harness_providers.construction import resolve_provider

    resolve_provider(spec.execution_provider)
    # These paths still construct Native/legacy executions outside the member
    # seam. Do not quietly mix them into an explicitly selected Provider Team.
    if (spec.tiny_agents or spec.enable_swarmflow or spec.enable_fork or spec.external_cli_agents
            or spec.enable_hitt or spec.enable_bridge):
        raise ValueError("External Team does not yet support TinyAgent, Swarmflow, fork, HITT, bridge or mixed CLI members")
    factory.validate_team_spec(spec)
    if spec.dispatch_mode == "scheduled" and not isinstance(factory, TeamReviewRuntimeFactory):
        raise ValueError("External scheduled Team requires a same-Provider review runtime factory")
    return factory
