class SpyhopError(Exception):
    """Base class for errors Spyhop reports to the user."""


class ConfigurationError(SpyhopError):
    pass


class JiraError(SpyhopError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)

        self.status_code = status_code


class JiraAuthenticationError(JiraError):
    pass


class JiraRateLimitedError(JiraError):
    def __init__(self, retry_after: float | None) -> None:
        super().__init__("JIRA rate limit reached", status_code=429)

        self.retry_after = retry_after


class OrcaError(SpyhopError):
    pass


class OrcaUnavailableError(OrcaError):
    """The CLI could not reach a running Orca; try again later."""


class OrcaUncertainError(OrcaError):
    """The command may or may not have taken effect (timeout, crash, garbled
    answer). A launch in this state is retried with the same operation id."""


class OrcaCommandError(OrcaError):
    """Orca answered and refused the command."""

    def __init__(self, code: str, message: str, data: object = None) -> None:
        super().__init__(f"{code}: {message}" if message != code else code)

        self.code = code
        self.data = data
