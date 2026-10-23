from slowapi import Limiter
from slowapi.util import get_remote_address

# SINGLE global limiter instance
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["1500/minute"],  # Global limit
    enabled=True,
)

# Re-export for use in routers
__all__ = ["limiter"]
