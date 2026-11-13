"""Process-local ownership of immutable segment files.

Leases contain only file identities, never their owner objects. A handle must
explicitly release its lease; closing one lease cannot affect another owner.
"""
import os

_owners = {}

def storage_key(storage):
    if hasattr(storage, "folder"):
        return ("file", os.path.realpath(os.path.abspath(storage.folder)))
    return ("ram", storage)

class Lease:
    def __init__(self, storage, names=(), toc=None):
        self.key = storage_key(storage)
        self.token = object()
        self.closed = False
        self.update(names, toc)

    def update(self, names, toc=None):
        if self.closed:
            raise RuntimeError("Cannot update a closed segment lease")
        _owners.setdefault(self.key, {})[self.token] = (set(names), toc)

    def close(self):
        if self.closed:
            return
        self.closed = True
        owners = _owners.get(self.key)
        if owners is not None:
            owners.pop(self.token, None)
            if not owners:
                _owners.pop(self.key, None)

def retained(storage):
    names, tocs = set(), set()
    for segments, toc in _owners.get(storage_key(storage), {}).values():
        names.update(segments)
        if toc is not None:
            tocs.add(toc)
    return names, tocs
