# coding=utf-8
from __future__ import absolute_import

import threading


class OutputBuffer(object):
    """Byte buffer with absolute offsets that keeps only the newest ``max_size`` bytes.

    Clients poll with the offset they got last time and receive everything after it.
    If they fell too far behind, they get what is still there.
    """

    def __init__(self, max_size):
        self._max_size = max_size
        self._data = bytearray()
        self._base = 0  # absolute offset of _data[0]
        self._lock = threading.Lock()

    def append(self, data):
        with self._lock:
            self._data.extend(data)
            overflow = len(self._data) - self._max_size
            if overflow > 0:
                del self._data[:overflow]
                self._base += overflow

    def read(self, offset=None):
        with self._lock:
            end = self._base + len(self._data)
            if offset is None or offset < self._base or offset > end:
                offset = self._base
            return bytes(self._data[offset - self._base:]), end
