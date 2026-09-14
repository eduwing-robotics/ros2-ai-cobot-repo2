"""Transport-independent client interface for Incoming Material QA."""

from abc import ABC, abstractmethod
from shared.schemas.vision import IncomingMaterialQARequest, IncomingMaterialQAResult

class IncomingMaterialQAClientError(Exception):
    """Base exception for QA client errors."""
    pass

class IncomingMaterialQAClientTimeoutError(IncomingMaterialQAClientError):
    """Exception raised when the QA request times out."""
    pass

class IncomingMaterialQAClient(ABC):
    """
    Abstract interface for requesting Incoming Material QA from the AI Vision system.
    This hides UDP / Transport specifics from the Business Layer.
    """

    @abstractmethod
    def request_inspection(
        self, request: IncomingMaterialQARequest, timeout_sec: float = 10.0
    ) -> IncomingMaterialQAResult:
        """
        Sends an inspection request to the Vision system and blocks until a result is received
        or a timeout occurs.

        Raises:
            IncomingMaterialQAClientTimeoutError: if no response is received in time.
            IncomingMaterialQAClientError: for other transport/system failures.
        """
        pass
