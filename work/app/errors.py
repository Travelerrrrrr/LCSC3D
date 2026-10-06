"""Exceptions shared by downloads and the offline Altium converter."""


class Cancelled(Exception):
    pass


class DownloadError(Exception):
    pass
