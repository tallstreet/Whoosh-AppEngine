#!/usr/bin/env python3
"""Visible stable-sort and early-close incident reproduction."""
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if (ROOT / "src").is_dir():
    sys.path.insert(0, str(ROOT / "src"))
from whoosh.postpool import PostingPool
from whoosh.store import FileStorage


def main():
    rows = [(number % 3, ["a", "a\x00b", ""][number % 3], number % 5,
             {"ordinal": number, "descending": 39-number}) for number in range(40)]
    with tempfile.TemporaryDirectory() as directory:
        storage = FileStorage(directory)
        pool = PostingPool(96, fan_in=2, run_storage=storage)
        for row in rows:
            pool.add_posting(*row)
        assert list(pool) == sorted(rows, key=lambda row: row[:3])
        assert storage.list() == []
        pool.close()
        other = PostingPool(73, fan_in=2, run_storage=storage)
        for row in rows:
            other.add_posting(*row)
        iterator = iter(other)
        iterator.close()
        assert storage.list() == []
        try:
            next(iterator)
        except StopIteration:
            pass
        else:
            raise AssertionError("closed iterator returned a record")
    print("Public stable-sort and cleanup reproduction passed.")


if __name__ == "__main__":
    main()
