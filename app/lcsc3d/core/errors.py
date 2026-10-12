"""Exceptions shared by downloads and previews."""


class Cancelled(Exception):
    pass


class DownloadError(Exception):
    pass


class EdaError(DownloadError):
    """Structured, non-payload conversion diagnostics shared by all targets."""
    def __init__(self, message, *, code, domain='', primitive='', source_id=''):
        super().__init__(message)
        self.eda_code, self.eda_domain = code, domain
        self.eda_primitive, self.eda_source_id = primitive, source_id
