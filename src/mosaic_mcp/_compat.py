"""Compatibility shims for monorepo imports not shipped in the standalone package."""

def get_settings():
    """Stub — standalone package uses DATABASE_URL env var directly."""
    raise ImportError("Use DATABASE_URL env var")


def user_active_team(user_id):
    """Stub — no team support in the standalone package."""
    return None


def check_team_quota(team_id, month):
    """Stub — no team support in the standalone package."""
    return None
