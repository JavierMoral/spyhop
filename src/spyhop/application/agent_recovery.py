"""Recovers the agent of a run: a lost agent starts again, and a stopped
agent is told to continue. Each one counts as a restart; after
agents.max_restarts, the run waits for a person."""

import logging
from datetime import timedelta

from spyhop.application.context_builder import PLAN_FILE
from spyhop.application.phase_runner import PhaseRunner
from spyhop.application.run_alerts import RunAlerts
from spyhop.config.settings import AgentSettings
from spyhop.domain.enums import RunStatus, WaitingReason
from spyhop.domain.exceptions import OrcaError
from spyhop.domain.models import AgentSession, AutomationRun, ResolvedWorkflow, Worktree
from spyhop.domain.protocols import TerminalHost
from spyhop.infrastructure.persistence.repositories import RunRepository, utc_now

_log = logging.getLogger(__name__)

# Cycles without the run's agent in `worktree ps`, and the time since the
# first one, before the agent counts as lost. Orca lists a new agent a few
# seconds after the launch, and a restart of Orca briefly lists none. The time
# is necessary because a wake of the service starts a cycle at once, so the
# hooks of a new agent can start several cycles in a few seconds.

MISSING_CYCLES = 3

MISSING_AFTER = timedelta(seconds=90)

CONTINUE_PROMPT = (
    "Continúa con la tarea de CLAUDE-TASK.md. Si ya has terminado, escribe CLAUDE-PLAN.md o "
    "CLAUDE-RESULT.md y declara el plan o el fin con el comando spyhop declare de tu /goal."
)


class AgentRecovery:
    def __init__(
        self,
        runs: RunRepository,
        terminal_host: TerminalHost,
        agents: AgentSettings,
        phases: PhaseRunner,
        alerts: RunAlerts,
        missing_after: timedelta = MISSING_AFTER,
    ) -> None:
        self._runs = runs
        self._terminal_host = terminal_host
        self._agents = agents
        self._phases = phases
        self._alerts = alerts
        self._missing_after = missing_after
        # The missing time counts from the start at the earliest: while the
        # service was stopped, nothing looked for the agent.
        self._started_at = utc_now()

    async def on_missing(
        self,
        run: AutomationRun,
        worktree: Worktree,
        workflow: ResolvedWorkflow,
        session: AgentSession | None,
    ) -> None:
        missing = self._runs.note_missing(run.id)

        assert missing.missing_since is not None

        if (
            missing.missing_cycles < MISSING_CYCLES
            or utc_now() - max(missing.missing_since, self._started_at) < self._missing_after
        ):
            _log.debug("%s: agent not listed by Orca (%d)", run.issue_key, missing.missing_cycles)

            return

        self._runs.clear_missing(run.id)

        if (
            run.status is RunStatus.RUNNING
            and run.restarts < self._agents.max_restarts
            and workflow.phases[run.phase] in ("plan", "implement")
        ):
            self._runs.count_restart(run.id)

            _log.info("%s: agent lost; started again", run.issue_key)

            await self._alerts.tell(
                run, "el agente ya no estaba en Orca; Spyhop lo arranca otra vez"
            )
            await self._phases.enter(run, worktree, workflow, run.phase, session)

            return

        await self._alerts.wait(
            run, WaitingReason.UNKNOWN_STATE, f"no agent running in {worktree.path}"
        )

    async def prompt_stopped(self, run: AutomationRun, session: AgentSession | None) -> bool:
        """Tells an agent that stopped without declaring the plan or the end
        to continue. Returns False when the run must wait for a person."""

        if (
            run.status is not RunStatus.RUNNING
            or run.restarts >= self._agents.max_restarts
            or session is None
            or session.agent_handle is None
        ):
            return False

        try:
            await self._terminal_host.send_prompt(session.agent_handle, CONTINUE_PROMPT)
        except OrcaError as error:
            _log.warning("%s: stopped agent not prompted: %s", run.issue_key, error)

            return False

        self._runs.count_restart(run.id)

        # The stopped state is now older than the run's last change.

        self._runs.mark_resumed(run.id)

        _log.info("%s: agent stopped without a declaration; told to continue", run.issue_key)

        await self._alerts.tell(
            run, "el agente se paró sin declarar el plan ni el fin; Spyhop le pide seguir"
        )

        return True

    async def on_plan_agent_closed(self, run: AutomationRun, worktree: Worktree) -> None:
        """Tells the person once that only an agent that they start can now
        revise the plan."""

        if run.plan_agent_closed_told:
            return

        self._runs.set_plan_agent_closed_told(run.id, True)

        _log.info("%s: plan agent closed while the plan waits for its review", run.issue_key)

        await self._alerts.tell(
            run,
            f"el agente del plan está cerrado; aprueba el plan con `spyhop approve "
            f"{run.issue_key}`, o abre un agente en {worktree.path} para revisar {PLAN_FILE}",
        )
