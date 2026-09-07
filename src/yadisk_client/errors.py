"""Errors intentionally omit tokens, signed URLs and response bodies."""


class YandexDiskError(Exception):
    """Base library error."""


class APIError(YandexDiskError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"Yandex API returned HTTP {status_code}")


class AuthenticationError(APIError):
    """Missing, expired or rejected credentials."""


class NetworkError(YandexDiskError):
    """Network operation failed after bounded retries."""


class IntegrityError(YandexDiskError):
    """Transferred contents could not be verified."""


class ProtocolError(YandexDiskError):
    """Unexpected or unsafe server response."""
