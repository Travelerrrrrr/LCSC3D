"""Bound gzip expansion before retaining an entire network response."""
import gzip
import io


class ResponseTooLarge(ValueError):
    pass


def decompress_gzip(payload, limit, check=lambda: None):
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
        while True:
            check()
            block = stream.read(min(65536, limit - output.tell() + 1))
            if not block:
                break
            if output.tell() + len(block) > limit:
                raise ResponseTooLarge('Expanded response exceeds its size limit')
            output.write(block)
    check()
    return output.getvalue()
