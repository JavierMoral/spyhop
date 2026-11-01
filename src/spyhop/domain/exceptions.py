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
