##
## 
##   EEEEE   X   X   CCCCC   EEEEE   PPPPPP   TTTTTTT   IIIIIII   OOOOO   N     N   SSSSS
##   E        X X   C        E       P    P      T        I      O     O  NN    N   S
##   EEEEE     X    C        EEEEE   PPPPPP      T        I      O     O  N N   N   SSSSS
##   E        X X   C        E       P           T        I      O     O  N  N  N       S
##   EEEEE   X   X   CCCCC   EEEEE   P           T      IIIIIII   OOOOO   N     N   SSSSS
##
##
##

# exceptions.py
from typing import Optional, List

class WizError(Exception):
    """Base exception class for all Wiz SDK errors."""
    def __init__(self, message: str, original_error: Optional[Exception] = None) -> None:
        super().__init__(message)
        self.original_error = original_error

class WizAuthenticationError(WizError):
    """Raised when authentication fails."""
    pass

class WizAPIError(WizError):
    """Raised when API calls fail."""
    def __init__(
        self, 
        message: str, 
        status_code: Optional[int] = None, 
        response_text: Optional[str] = None, 
        original_error: Optional[Exception] = None
    ) -> None:
        super().__init__(message, original_error)
        self.status_code = status_code
        self.response_text = response_text

class WizConfigurationError(WizError):
    """Raised when configuration is invalid or missing."""
    pass

class WizCredentialsError(WizError):
    """Raised when credentials are invalid or missing."""
    pass

class WizRateLimitError(WizError):
    """Raised when rate limit is exceeded."""
    def __init__(
        self, 
        message: str, 
        retry_after: Optional[int] = None, 
        original_error: Optional[Exception] = None
    ) -> None:
        super().__init__(message, original_error)
        self.retry_after = retry_after

class WizQueryError(WizError):
    """Raised when GraphQL query is invalid or fails."""
    def __init__(
        self, 
        message: str, 
        query: Optional[str] = None, 
        errors: Optional[List[dict]] = None, 
        original_error: Optional[Exception] = None
    ) -> None:
        super().__init__(message, original_error)
        self.query = query
        self.errors = errors or []

class WizReportError(WizError):
    """Raised when report generation or download fails."""
    def __init__(
        self, 
        message: str, 
        report_id: Optional[str] = None, 
        report_name: Optional[str] = None, 
        original_error: Optional[Exception] = None
    ) -> None:
        super().__init__(message, original_error)
        self.report_id = report_id
        self.report_name = report_name

class WizTimeoutError(WizError):
    """Raised when operations timeout."""
    pass

class WizFileError(WizError):
    """Raised when file operations fail."""
    pass

class WizSchemaValidationError(WizQueryError):
    """Raised when a query fails validation against the cached Wiz schema."""
    def __init__(
        self,
        message: str,
        query: Optional[str] = None,
        validation_errors: Optional[List[str]] = None,
        original_error: Optional[Exception] = None
    ) -> None:
        super().__init__(message, query=query, errors=None, original_error=original_error)
        self.validation_errors = validation_errors or []

class WizServerlessError(WizError):
    """Raised when serverless-specific operations fail."""
    pass