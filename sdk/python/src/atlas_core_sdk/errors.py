class AtlasError(Exception):
    """Base exception for SDK-visible Atlas failures."""


class InvalidRequestError(AtlasError):
    """The request did not satisfy an Atlas contract."""


class ProposalConflictError(AtlasError):
    """The proposal does not exist or can no longer accept a decision."""


class ExecutionConflictError(AtlasError):
    """The execution cannot transition from its current durable state."""


class AtlasTransportError(AtlasError):
    """The selected Atlas transport could not complete the operation."""


class AuthenticationError(AtlasTransportError):
    """The remote service did not accept the supplied service identity."""


class AuthorizationError(AtlasTransportError):
    """The service identity is not allowed to perform the requested operation."""
