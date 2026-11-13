#===============================================================================
# Copyright 2007 Matt Chaput
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#===============================================================================

"""Stable, bounded-fan-in external sorting of posting records."""

import heapq
from marshal import dumps, loads
from tempfile import TemporaryDirectory
from uuid import uuid4

from whoosh.store import FileStorage


def encode_posting(fieldNum, text, doc, data):
    """Serialize a posting without treating text as a NUL-delimited field."""
    return dumps((fieldNum, text, doc, data), 2)


def decode_posting(posting):
    return loads(posting)


class _PoolIterator(object):
    """An immediately reserved, explicitly closable view of one posting pool."""

    def __init__(self, pool):
        self.pool = pool
        self.source = None

    def __iter__(self):
        return self

    def __next__(self):
        if self.pool.finished:
            raise StopIteration
        if self.source is None:
            self.source = self.pool._iterate()
        try:
            return next(self.source)
        except BaseException:
            # The generator has stopped executing when this catch runs, so
            # closing it is safe even if the exception retains its traceback.
            self.pool.close()
            raise

    def _stop(self):
        source, self.source = self.source, None
        if source is not None:
            source.close()

    def close(self):
        self.pool.close()


class PostingPool(object):
    """Sort postings with bounded buffered data and merge fan-in.

    Runs contain an explicit insertion ordinal. Their payload is never part of
    a comparison, and an ordinal survives every intermediate merge pass. Files
    are closed between passes and only this pool's unique names are reclaimed.
    """

    def __init__(self, limit, fan_in=8, run_storage=None):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if isinstance(fan_in, bool) or not isinstance(fan_in, int) or fan_in < 2:
            raise ValueError("fan_in must be an integer of at least two")
        self.limit = limit
        self.fan_in = fan_in
        self.size = 0
        self.postings = []
        self.runs = []
        self.count = 0
        self.finished = False
        self._reserved = False
        self._iterator = None
        self._owned = set()
        self._handles = []
        self._serial = 0
        self._prefix = "whoosh-pool-%s-" % uuid4().hex
        self._directory = None
        if run_storage is None:
            self._directory = TemporaryDirectory(prefix="whoosh-pool-")
            run_storage = FileStorage(self._directory.name)
        self.run_storage = run_storage

    def add_posting(self, field_num, text, doc, data):
        if self.finished or self._reserved:
            raise RuntimeError("Cannot add to a consumed or closed posting pool")
        try:
            ordinal = self.count
            raw = dumps((field_num, text, doc, ordinal, data), 2)
            if self.postings and self.size + len(raw) > self.limit:
                self._flush_run()
            self.postings.append(((field_num, text, doc, ordinal), raw))
            self.size += len(raw)
            self.count += 1
            # An oversized record remains valid, but do not combine it with
            # any later record in the ingestion buffer.
            if self.size >= self.limit:
                self._flush_run()
        except BaseException:
            self.close()
            raise

    def _new_name(self):
        name = "%s%d.run" % (self._prefix, self._serial)
        self._serial += 1
        self._owned.add(name)
        return name

    def _create(self, name):
        handle = self.run_storage.create_file(name)
        self._handles.append(handle)
        return handle

    def _open(self, name):
        handle = self.run_storage.open_file(name)
        self._handles.append(handle)
        return handle

    def _close_handle(self, handle):
        if handle in self._handles:
            try:
                handle.close()
            finally:
                self._handles.remove(handle)

    def _delete_run(self, name):
        try:
            self.run_storage.delete_file(name)
        except (FileNotFoundError, NameError):
            # Missing names after failed creation use different exception
            # types in FileStorage and RamStorage.
            pass
        self._owned.discard(name)

    def _write_run(self, records):
        name = self._new_name()
        stream = None
        count = 0
        try:
            stream = self._create(name)
            for raw in records:
                stream.write_string(raw)
                count += 1
        finally:
            # A failing output write must unwind active input generators too.
            close = getattr(records, "close", None)
            if close is not None:
                close()
            if stream is not None:
                self._close_handle(stream)
        return name, count

    def _flush_run(self):
        if not self.postings:
            return
        self.postings.sort(key=lambda item: item[0])
        run = self._write_run(raw for key, raw in self.postings)
        self.runs.append(run)
        self.postings.clear()
        self.size = 0

    @staticmethod
    def _key(raw):
        posting = loads(raw)
        return posting[:4]

    def _merge_records(self, runs):
        streams = []
        heap = []
        remaining = []
        try:
            for position, (name, count) in enumerate(runs):
                stream = self._open(name)
                streams.append(stream)
                remaining.append(count)
                if count:
                    raw = stream.read_string()
                    remaining[position] -= 1
                    heap.append((self._key(raw), position, raw))
            heapq.heapify(heap)
            while heap:
                key, position, raw = heapq.heappop(heap)
                yield raw
                if remaining[position]:
                    raw = streams[position].read_string()
                    remaining[position] -= 1
                    heapq.heappush(heap, (self._key(raw), position, raw))
        finally:
            for stream in streams:
                self._close_handle(stream)
            heap.clear()

    def _reduce_runs(self):
        while len(self.runs) > self.fan_in:
            old_runs = self.runs
            new_runs = []
            for offset in range(0, len(old_runs), self.fan_in):
                group = old_runs[offset:offset + self.fan_in]
                if len(group) == 1:
                    new_runs.append(group[0])
                    continue
                output = self._write_run(self._merge_records(group))
                new_runs.append(output)
                for name, count in group:
                    self._delete_run(name)
            self.runs = new_runs

    @staticmethod
    def _decode_record(raw):
        field_num, text, doc, ordinal, data = loads(raw)
        return field_num, text, doc, data

    def _iterate(self):
        if not self.runs:
            self.postings.sort(key=lambda item: item[0])
            for key, raw in self.postings:
                yield self._decode_record(raw)
            return
        self._flush_run()
        self._reduce_runs()
        records = self._merge_records(self.runs)
        try:
            for raw in records:
                yield self._decode_record(raw)
        finally:
            records.close()

    def __iter__(self):
        if self.finished or self._reserved:
            raise RuntimeError("A posting pool is single-use")
        self._reserved = True
        self._iterator = _PoolIterator(self)
        return self._iterator

    def close(self):
        if self.finished:
            return
        self.finished = True
        if self._iterator is not None:
            self._iterator._stop()
        for handle in list(self._handles):
            self._close_handle(handle)
        for name in list(self._owned):
            self._delete_run(name)
        self.runs.clear()
        self.postings.clear()
        self.size = 0
        if self._directory is not None:
            directory, self._directory = self._directory, None
            directory.cleanup()
