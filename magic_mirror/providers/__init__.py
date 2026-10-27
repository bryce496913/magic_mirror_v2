"""Independent read-only dashboard data sources."""


class ProviderUnavailable(Exception):
    """A source has no usable data; message is safe for the dashboard."""
