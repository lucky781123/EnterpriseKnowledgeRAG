"""本地 P0 的可信请求上下文与开发身份验证组件。"""

from .authorization import (
    AuthorizationDenied,
    AuthorizationService,
    AuthorizedDocument,
    ResourceNotFound,
)
from .context import RequestContext
from .dev_token import DevTokenCodec, DevTokenError
from .governance import AuditEvent, InMemoryAuditSink, TenantGovernance, TenantQuotaExceeded
from .oidc import (
    MappingTenantMembershipResolver,
    OIDCSettings,
    PyJWTJWKSVerifier,
    build_oidc_context_dependency,
)

__all__ = [
    "AuditEvent",
    "AuthorizationDenied",
    "AuthorizationService",
    "AuthorizedDocument",
    "DevTokenCodec",
    "DevTokenError",
    "InMemoryAuditSink",
    "MappingTenantMembershipResolver",
    "OIDCSettings",
    "PyJWTJWKSVerifier",
    "RequestContext",
    "ResourceNotFound",
    "TenantGovernance",
    "TenantQuotaExceeded",
    "build_oidc_context_dependency",
]
