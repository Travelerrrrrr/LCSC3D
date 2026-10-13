"""Keep urllib's cookie/redirect behavior while reusing verified connections."""
from email.message import Message
import http.client
import socket
import ssl
import urllib.error
import urllib.request
import urllib.response
from urllib.parse import urlsplit

import urllib3
from urllib3.util import Timeout

from lcsc3d.services.backend import connection_pool


def translate_error(error):
    """Preserve the diagnostic categories used by the storefront UI."""
    if isinstance(error, urllib3.exceptions.ProtocolError):
        cause = next((v for v in error.args if isinstance(v, http.client.HTTPException)), None)
        raise cause or http.client.HTTPException('Incomplete network response') from error
    cause = getattr(error, 'reason', None) or getattr(error, '_reason', None) or error.__cause__
    if isinstance(error, urllib3.exceptions.SSLError):
        cause = next((v for v in error.args if isinstance(v, ssl.SSLError)), cause)
        cause = cause or ssl.SSLError('TLS connection failed')
    elif isinstance(error, urllib3.exceptions.NameResolutionError):
        cause = cause or socket.gaierror('Name resolution failed')
    elif isinstance(error, urllib3.exceptions.ReadTimeoutError) or (
            isinstance(error, urllib3.exceptions.ConnectTimeoutError)
            and not isinstance(error, urllib3.exceptions.NewConnectionError)):
        cause = TimeoutError('Connection timed out')
    raise urllib.error.URLError(cause or error) from error


class ResponseBody:
    def __init__(self, response):
        self.response = response
        self.closed = False

    def read(self, size=-1):
        try:
            value = self.response.read(None if size < 0 else size, decode_content=False)
            if not value or self.response.length_remaining == 0:
                self.response.release_conn()
            return value
        except urllib3.exceptions.HTTPError as exc:
            self.close()
            translate_error(exc)

    def close(self):
        if not self.closed:
            self.closed = True
            self.response.close()
            self.response.release_conn()


class PooledHandler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    def __init__(self, proxies):
        super().__init__()
        self.proxies = proxies

    def http_open(self, request):
        parsed = urlsplit(request.full_url)
        proxy = self.proxies.get(parsed.scheme)
        if proxy and urllib.request.proxy_bypass(parsed.hostname or ''):
            proxy = None
        try:
            response = connection_pool(proxy).request(request.get_method(), request.full_url,
                body=request.data, headers=dict(request.header_items()), preload_content=False,
                decode_content=False, redirect=False, retries=False,
                timeout=Timeout(connect=request.timeout, read=request.timeout), pool_timeout=request.timeout)
        except urllib3.exceptions.HTTPError as exc:
            translate_error(exc)
        headers = Message()
        for name in response.headers:
            for value in response.headers.getlist(name):
                headers[name] = value
        wrapped = urllib.response.addinfourl(ResponseBody(response), headers, request.full_url, response.status)
        wrapped.msg = response.reason
        return wrapped

    https_open = http_open
