#!/usr/bin/env python3
"""Independent deterministic snapshot/segment-retention verifier.

Each named case runs in a new interpreter. Expected documents and postings are
computed from fixture token strings, and expected retention is a union of
recorded segment-file sets for deliberately live owners, never an engine registry.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
import json
import os
from pathlib import Path
import random
import shutil
import re
import subprocess
import sys
import tempfile
import traceback

SEGMENT_RE = re.compile(r"^[0-9]+\.(?:dci|dcz|tiz|fvz)$")
TOC_RE = re.compile(r"^_MAIN_([0-9]+)\.toc$")


def load_repo(repo):
    global analysis, fields, index, reading, searching, store, writing, query, TrackingStorage
    sys.path.insert(0, str(Path(repo).resolve() / "src"))
    from whoosh import analysis, fields, index, reading, searching, store, writing, query

    class TrackingStorage(store.FileStorage):
        def __init__(self, path):
            super().__init__(str(path))
            self.opened = []
            self.vector_opens = []
            self.failure_suffix = None
            self.failure_remaining = 0
            self.delete_failures = set()

        def arm_open_failure(self, suffix, occurrence=1):
            self.failure_suffix = suffix
            self.failure_remaining = occurrence

        def open_file(self, name, *args, **kwargs):
            if name.endswith(".fvz"):
                self.vector_opens.append(name)
            if self.failure_remaining and name.endswith(self.failure_suffix):
                self.failure_remaining -= 1
                if self.failure_remaining == 0:
                    raise OSError("injected open failure: " + name)
            result = super().open_file(name, *args, **kwargs)
            self.opened.append(result)
            return result

        def create_file(self, name):
            result = super().create_file(name)
            self.opened.append(result)
            return result

        def delete_file(self, name):
            if name in self.delete_failures:
                raise OSError("injected delete failure: " + name)
            return super().delete_file(name)

        def live_handles(self):
            return [f for f in self.opened if not f.is_closed and not f.file.closed]


def schema():
    result = fields.Schema()
    result.add("id", fields.ID(stored=True, unique=True))
    result.add("body", fields.TEXT(analyzer=analysis.KeywordAnalyzer(), phrase=True,
                                   vector=fields.Positions(analysis.KeywordAnalyzer()), stored=True))
    return result


def document(number):
    return {"id": "d%d" % number, "body": "shared token%d shared" % number}


@contextmanager
def workspace():
    with tempfile.TemporaryDirectory(prefix="whoosh-snapshot-verifier-") as folder:
        yield Path(folder)


def make_index(root, name="db", batches=None, ram=False, tracked=True):
    if ram:
        storage = store.RamStorage()
    else:
        path = root / name
        path.mkdir()
        storage = TrackingStorage(path) if tracked else store.FileStorage(str(path))
    ix = index.Index(storage, schema(), create=True)
    batches = [[document(0), document(1)], [document(2)]] if batches is None else batches
    for batch in batches:
        writer = ix.writer()
        for item in batch:
            writer.add_document(**item)
        writer.commit(writing.NO_MERGE)
    return ix


def docs_by_id(documents):
    return {d["id"]: d for d in documents}


def current_segment_files(storage):
    return {name for name in storage.list() if SEGMENT_RE.fullmatch(name)}


def files_for(ix):
    result = set()
    for segment in ix.segments:
        result.update([segment.doclen_filename, segment.docs_filename, segment.term_filename])
        if ix.schema.has_vectored_fields():
            result.add(segment.vector_filename)
    return result


def assert_files(storage, expected):
    actual = current_segment_files(storage)
    assert actual == expected, "segment files: missing=%r extra=%r" % (sorted(expected - actual), sorted(actual - expected))


def assert_tocs(storage, generations):
    actual = {name for name in storage.list() if TOC_RE.fullmatch(name)}
    expected = {"_MAIN_%d.toc" % number for number in generations}
    assert actual == expected, "TOC files: missing=%r extra=%r" % (sorted(expected - actual), sorted(actual - expected))


def snapshot_model(ix, documents, live=None):
    """Correlate opaque physical numbers with independently known document IDs."""
    by_id = docs_by_id(documents)
    reader = ix.doc_reader()
    try:
        physical = [reader[n]["id"] for n in range(reader.doc_count_all())]
    finally:
        reader.close()
    assert Counter(physical) == Counter(by_id.keys()), ("fixture physical corpus mismatch", physical, sorted(by_id))
    live = set(by_id) if live is None else set(live)
    words = {key: value["body"].split() for key, value in by_id.items()}
    expected_max_weight = 0
    offset = 0
    for segment in ix.segments:
        count = Counter()
        for docid in physical[offset:offset + segment.max_doc]:
            count.update(words[docid])
        expected_max_weight = max([expected_max_weight] + list(count.values()))
        offset += segment.max_doc
    return {"physical": physical, "documents": by_id, "live": live,
            "words": words, "files": files_for(ix), "generation": ix.generation,
            "field_length": sum(len(v) for v in words.values()),
            "max_weight": expected_max_weight}


def positions(words):
    result = defaultdict(list)
    for number, word in enumerate(words):
        result[word].append(number)
    return dict(result)


def assert_doc_reader(reader, model, *, vectors=False):
    expected = [model["documents"][key] for key in model["live"]]
    actual = list(reader)
    assert sorted(actual, key=lambda x:x["id"]) == sorted(expected, key=lambda x:x["id"]), ("snapshot documents", actual, expected)
    assert reader.doc_count() == len(model["live"]), "snapshot live count changed"
    assert reader.doc_count_all() == len(model["physical"]), "snapshot physical count changed"
    assert reader.field_length("body") == model["field_length"], "snapshot field total changed"
    fieldnum = reader.schema.name_to_number("body")
    for docnum, docid in enumerate(model["physical"]):
        assert reader[docnum] == model["documents"][docid]
        assert reader.doc_field_length(docnum, fieldnum) == len(model["words"][docid])
        if vectors and docid in model["live"]:
            actual_vector = dict(reader.vector_as(docnum, fieldnum, "positions"))
            assert actual_vector == positions(model["words"][docid]), ("snapshot vector", docid, actual_vector)


def assert_term_reader(reader, model):
    fieldnum = reader.schema.name_to_number("body")
    all_words = sorted(set(word for words in model["words"].values() for word in words))
    assert list(reader.lexicon(fieldnum)) == all_words
    for word in all_words:
        expected = []
        for number, docid in enumerate(model["physical"]):
            pp = positions(model["words"][docid])
            if docid in model["live"] and word in pp:
                expected.append((number, pp[word]))
        assert list(reader.positions(fieldnum, word)) == expected, ("snapshot posting", word)
        assert reader.frequency(fieldnum, word) == sum(words.count(word) for words in model["words"].values())
        assert reader.doc_frequency(fieldnum, word) == sum(word in words for words in model["words"].values())


def assert_searcher(searcher, model, *, vectors=False):
    assert_doc_reader(searcher.doc_reader, model, vectors=vectors)
    assert_term_reader(searcher.term_reader, model)
    assert searcher.doc_count_all() == len(model["physical"])
    assert searcher.max_weight() == model["max_weight"]
    found = sorted(d["id"] for d in searcher.documents(body="shared"))
    assert found == sorted(model["live"]), ("snapshot query", found)


def assert_index_view(ix, model, *, vectors=False):
    assert ix.generation == model["generation"]
    assert ix.doc_count() == len(model["live"])
    assert ix.doc_count_all() == len(model["physical"])
    assert ix.field_length("body") == model["field_length"]
    reader = ix.doc_reader()
    term = ix.term_reader()
    try:
        assert_doc_reader(reader, model, vectors=vectors)
        assert_term_reader(term, model)
    finally:
        term.close()
        reader.close()


def delete_id(ix, identifier, *, commit=True):
    reader = ix.doc_reader()
    try:
        numbers = [n for n in range(reader.doc_count_all()) if reader[n]["id"] == identifier]
    finally:
        reader.close()
    assert len(numbers) == 1
    writer = ix.writer()
    writer.delete_document(numbers[0])
    if commit:
        writer.commit(writing.NO_MERGE)
    return writer


def add_and_optimize(ix, item):
    writer = ix.writer()
    writer.add_document(**item)
    writer.commit(writing.NO_MERGE)
    ix.optimize()


def new_handle(ix, *, alias=False):
    if isinstance(ix.storage, store.RamStorage):
        return index.Index(ix.storage)
    folder = Path(ix.storage.folder)
    path = folder / ".." / folder.name if alias else folder
    return index.Index(TrackingStorage(path))


def test_baseline_model():
    with workspace() as root:
        ix = make_index(root)
        model = snapshot_model(ix, [document(i) for i in range(3)])
        assert_index_view(ix, model, vectors=True)
        assert {d["id"] for d in ix.find("body:shared")} == model["live"]
        searcher = ix.searcher()
        assert_searcher(searcher, model, vectors=True)
        searcher.close()
        ix.close()


def delayed_vector_case(ram):
    with workspace() as root:
        ix = make_index(root, ram=ram)
        model = snapshot_model(ix, [document(i) for i in range(3)])
        if not ram:
            ix.storage.vector_opens.clear()
        reader = ix.doc_reader()
        if not ram:
            assert not ix.storage.vector_opens, "document reader opened vectors eagerly"
        delete_id(ix, "d0")
        ix.optimize()
        current_files = files_for(ix)
        latest = new_handle(ix)
        ix.close()
        latest.clean_files()
        assert_doc_reader(reader, model, vectors=True)
        assert model["files"] <= current_segment_files(latest.storage)
        reader.close()
        latest.clean_files()
        assert_files(latest.storage, current_files)
        latest.close()


def test_delayed_vector_after_parent_close_file():
    delayed_vector_case(False)


def test_delayed_vector_after_parent_close_ram():
    delayed_vector_case(True)


def test_old_index_can_create_readers_later():
    with workspace() as root:
        current = make_index(root)
        old = new_handle(current)
        model = snapshot_model(old, [document(i) for i in range(3)])
        add_and_optimize(current, document(3))
        current.clean_files()
        assert_index_view(old, model, vectors=True)
        old.close()
        current.clean_files()
        assert_files(current.storage, files_for(current))
        current.close()


def test_refresh_preserves_old_owner_and_toc_lifetimes():
    with workspace() as root:
        current = make_index(root)
        old = new_handle(current)
        model = snapshot_model(old, [document(i) for i in range(3)])
        assert old.refresh() is old
        add_and_optimize(current, document(3))
        refreshed = old.refresh()
        assert refreshed is not old and refreshed.generation == current.generation
        current.clean_files()
        assert_tocs(current.storage, {current.generation, old.generation})
        assert_index_view(old, model, vectors=True)
        reader = old.doc_reader()
        old.close()
        old.close()
        refreshed.clean_files()
        assert_tocs(current.storage, {current.generation})
        assert_doc_reader(reader, model, vectors=True)
        reader.close()
        refreshed.close()
        current.clean_files()
        assert_files(current.storage, files_for(current))
        current.close()


def test_empty_generation_owners_and_readers():
    with workspace() as root:
        current = make_index(root, batches=[])
        empty = new_handle(current)
        model = snapshot_model(empty, [])
        reader, term, searcher = empty.doc_reader(), empty.term_reader(), empty.searcher()
        writer = current.writer()
        writer.add_document(**document(0))
        writer.commit(writing.NO_MERGE)
        current.clean_files()
        assert_tocs(current.storage, {current.generation, empty.generation})
        assert_doc_reader(reader, model)
        assert_term_reader(term, model)
        assert_searcher(searcher, model)
        empty.close()
        current.clean_files()
        assert_tocs(current.storage, {current.generation})
        assert_doc_reader(reader, model)
        searcher.close()
        term.close()
        reader.close()
        current.clean_files()
        assert_files(current.storage, files_for(current))
        current.close()


def test_stale_cleanup_preserves_latest_without_latest_owner():
    with workspace() as root:
        current = make_index(root)
        old = new_handle(current)
        old_model = snapshot_model(old, [document(i) for i in range(3)])
        add_and_optimize(current, document(3))
        latest_files = files_for(current)
        latest_generation = current.generation
        current.close()
        old.clean_files()
        assert ("_MAIN_%d.toc" % latest_generation) in old.storage.list(), "stale cleanup deleted authoritative TOC"
        fresh = index.Index(TrackingStorage(root / "db"))
        fresh_reader = fresh.doc_reader()
        assert sorted(d["id"] for d in fresh_reader) == ["d0", "d1", "d2", "d3"]
        fresh_reader.close()
        assert_index_view(old, old_model, vectors=True)
        old.close()
        fresh.clean_files()
        assert_files(fresh.storage, latest_files)
        fresh.close()


def tombstone_isolation_case(cancel):
    with workspace() as root:
        ix = make_index(root)
        delete_id(ix, "d0")
        model = snapshot_model(ix, [document(i) for i in range(3)], {"d1", "d2"})
        dr, tr, searcher = ix.doc_reader(), ix.term_reader(), ix.searcher()
        writer = delete_id(ix, "d1", commit=False)
        assert_doc_reader(dr, model)
        assert_term_reader(tr, model)
        assert_searcher(searcher, model)
        if cancel:
            writer.cancel()
            writer = ix.writer()
            writer.add_document(**document(3))
            writer.commit(writing.NO_MERGE)
            expected_current = {"d1", "d2", "d3"}
        else:
            writer.commit(writing.NO_MERGE)
            expected_current = {"d2"}
        assert_doc_reader(dr, model, vectors=True)
        assert_term_reader(tr, model)
        assert_searcher(searcher, model, vectors=True)
        fresh = new_handle(ix)
        reader = fresh.doc_reader()
        assert {d["id"] for d in reader} == expected_current
        reader.close()
        fresh.close()
        searcher.close()
        tr.close()
        dr.close()
        ix.close()


def test_uncommitted_and_canceled_tombstones_are_isolated():
    tombstone_isolation_case(True)


def test_committed_tombstones_do_not_mutate_existing_readers():
    tombstone_isolation_case(False)


def test_searcher_outlives_index_and_keeps_statistics():
    with workspace() as root:
        current = make_index(root)
        model = snapshot_model(current, [document(i) for i in range(3)])
        searcher = current.searcher()
        next_index = new_handle(current)
        current.close()
        add_and_optimize(next_index, {"id": "long", "body": "shared " * 40})
        assert_searcher(searcher, model, vectors=True)
        searcher.close()
        next_index.clean_files()
        assert_files(next_index.storage, files_for(next_index))
        next_index.close()


def test_direct_term_reader_owns_segment_after_parent_close():
    with workspace() as root:
        current = make_index(root)
        model = snapshot_model(current, [document(i) for i in range(3)])
        term = current.term_reader()
        next_index = new_handle(current)
        current.close()
        add_and_optimize(next_index, document(3))
        assert_term_reader(term, model)
        assert model["files"] <= current_segment_files(next_index.storage)
        term.close()
        next_index.clean_files()
        assert_files(next_index.storage, files_for(next_index))
        next_index.close()


def test_double_close_does_not_release_other_owners():
    with workspace() as root:
        ix = make_index(root)
        model = snapshot_model(ix, [document(i) for i in range(3)])
        a, b = ix.doc_reader(), ix.doc_reader()
        a.close()
        a.close()
        add_and_optimize(ix, document(3))
        ix.clean_files()
        assert_doc_reader(b, model, vectors=True)
        b.close()
        b.close()
        ix.clean_files()
        assert_files(ix.storage, files_for(ix))
        ix.close()
        ix.close()


def test_double_searcher_close_preserves_another_searcher():
    with workspace() as root:
        current = make_index(root)
        model = snapshot_model(current, [document(i) for i in range(3)])
        first, second = current.searcher(), current.searcher()
        first.close()
        first.close()
        add_and_optimize(current, document(3))
        current.clean_files()
        assert_searcher(second, model, vectors=True)
        second.close()
        second.close()
        current.clean_files()
        assert_files(current.storage, files_for(current))
        current.close()


def test_path_aliases_share_lifetimes():
    with workspace() as root:
        current = make_index(root)
        link = root / "alias"
        link.symlink_to(root / "db", target_is_directory=True)
        old = index.Index(TrackingStorage(link))
        model = snapshot_model(old, [document(i) for i in range(3)])
        reader = old.doc_reader()
        old.close()
        add_and_optimize(current, document(3))
        cleaner = index.Index(TrackingStorage(root / "db" / ".." / "db"))
        current.close()
        cleaner.clean_files()
        assert_doc_reader(reader, model, vectors=True)
        reader.close()
        cleaner.clean_files()
        assert_files(cleaner.storage, files_for(cleaner))
        cleaner.close()


def test_distinct_directories_do_not_share_segment_owners():
    with workspace() as root:
        left = make_index(root, "left")
        # Reuse committed bytes so names actually overlap, independently of
        # whether the implementation allocates counters, random IDs, or another
        # format-compatible immutable identity in separately built indexes.
        shutil.copytree(root / "left", root / "right")
        right = index.Index(TrackingStorage(root / "right"))
        assert files_for(left) == files_for(right), "cloned fixture must reuse segment names"
        left_model = snapshot_model(left, [document(i) for i in range(3)])
        reader = left.doc_reader()
        add_and_optimize(left, document(3))
        add_and_optimize(right, document(3))
        right.clean_files()
        assert_files(right.storage, files_for(right))
        assert_doc_reader(reader, left_model, vectors=True)
        reader.close()
        left.clean_files()
        assert_files(left.storage, files_for(left))
        left.close()
        right.close()


def test_active_writer_staging_survives_other_handle_cleanup():
    with workspace() as root:
        ix = make_index(root)
        cleaner = new_handle(ix)
        committed_files = files_for(ix)
        writer = ix.writer()
        writer.add_document(**document(3))
        staged = current_segment_files(ix.storage) - committed_files
        assert staged, "file fixture did not create pending segment files"
        cleaner.clean_files()
        assert staged <= current_segment_files(ix.storage), "cleanup removed active writer staging"
        writer.add_document(**document(4))
        writer.commit(writing.NO_MERGE)
        cleaner.close()
        reader = ix.doc_reader()
        assert {d["id"] for d in reader} == {"d0", "d1", "d2", "d3", "d4"}
        reader.close()
        ix.close()


def test_cancel_reclaims_staging_and_releases_handles():
    with workspace() as root:
        ix = make_index(root)
        cleaner = new_handle(ix)
        live_before = len(ix.storage.live_handles())
        writer = ix.writer()
        writer.add_document(**document(3))
        writer.cancel()
        assert len(ix.storage.live_handles()) == live_before, "cancel left staged files open"
        cleaner.clean_files()
        assert_files(ix.storage, files_for(ix))
        next_writer = ix.writer()
        next_writer.add_document(**document(4))
        next_writer.commit(writing.NO_MERGE)
        reader = ix.doc_reader()
        assert {d["id"] for d in reader} == {"d0", "d1", "d2", "d4"}
        reader.close()
        cleaner.close()
        ix.close()


def terminal_cancel_case(first_commits):
    with workspace() as root:
        ix = make_index(root)
        first = ix.writer()
        first.add_document(**document(3))
        if first_commits:
            first.commit(writing.NO_MERGE)
        else:
            first.cancel()
        second = ix.writer()
        second.add_document(**document(4))
        first.cancel()
        rejected = False
        try:
            accidental = ix.writer()
        except (OSError, store.LockError, index.IndexLockedError):
            rejected = True
        else:
            accidental.cancel()
        assert rejected, "repeated cancellation released another writer's lock"
        second.commit(writing.NO_MERGE)
        reader = ix.doc_reader()
        expected = {"d0", "d1", "d2", "d4"} | ({"d3"} if first_commits else set())
        assert {d["id"] for d in reader} == expected
        reader.close()
        ix.close()


def test_terminal_cancel_does_not_unlock_new_writer():
    terminal_cancel_case(False)


def test_cancel_after_successful_commit_is_harmless():
    terminal_cancel_case(True)


def test_failed_stale_commit_releases_staging_and_lock():
    with workspace() as root:
        current = make_index(root)
        stale = new_handle(current)
        old_model = snapshot_model(stale, [document(i) for i in range(3)])
        add_and_optimize(current, document(3))
        writer = stale.writer()
        writer.add_document(**document(99))
        failure = None
        try:
            writer.commit(writing.NO_MERGE)
        except index.OutOfDateError as err:
            failure = err
        assert failure is not None, "stale writer commit did not raise OutOfDateError"
        assert_index_view(stale, old_model, vectors=True)
        assert {d["id"] for d in stale.find("body:shared")} == old_model["live"], "stale failure damaged Index.find"
        next_writer = current.writer()
        next_writer.add_document(**document(4))
        writer.cancel()
        rejected = False
        try:
            accidental = current.writer()
        except (OSError, store.LockError, index.IndexLockedError):
            rejected = True
        else:
            accidental.cancel()
        assert rejected, "finalizing failed stale writer released another writer's lock"
        next_writer.commit(writing.NO_MERGE)
        assert_index_view(stale, old_model, vectors=True)
        assert {d["id"] for d in stale.find("body:shared")} == old_model["live"], "stale Index.find changed after later commit"
        stale.close()
        current.clean_files()
        assert_files(current.storage, files_for(current))
        reader = current.doc_reader()
        assert {d["id"] for d in reader} == {"d0", "d1", "d2", "d3", "d4"}
        reader.close()
        current.close()


def failed_factory_case(kind, suffix, occurrence):
    with workspace() as root:
        current = make_index(root)
        owner = new_handle(current)
        tracked = owner.storage
        before = len(tracked.live_handles())
        tracked.arm_open_failure(suffix, occurrence)
        failure = None
        try:
            if kind == "index":
                made = index.Index(tracked)
            else:
                made = getattr(owner, kind)()
        except OSError as err:
            failure = err
        else:
            made.close()
        assert failure is not None, "injected construction failure was not observed"
        assert len(tracked.live_handles()) == before, "failed %s construction leaked handles" % kind
        # Keep failure/traceback and opened StructFiles strongly reachable. The
        # following cleanup therefore cannot rely on refcount or garbage collection.
        add_and_optimize(current, document(3))
        owner.close()
        current.clean_files()
        assert_files(current.storage, files_for(current))
        current.close()


def test_partial_document_reader_construction_unwinds():
    failed_factory_case("doc_reader", ".dcz", 2)


def test_partial_term_reader_construction_unwinds():
    failed_factory_case("term_reader", ".tiz", 2)


def test_partial_searcher_construction_unwinds():
    failed_factory_case("searcher", ".dcz", 2)


def test_partial_index_construction_unwinds():
    failed_factory_case("index", ".dcz", 2)


def test_failed_lazy_vector_open_can_retry():
    with workspace() as root:
        current = make_index(root)
        model = snapshot_model(current, [document(i) for i in range(3)])
        owner = new_handle(current)
        tracked = owner.storage
        reader = owner.doc_reader()
        owner.close()
        add_and_optimize(current, document(3))
        tracked.arm_open_failure(".fvz", 1)
        failure = None
        try:
            list(reader.vector(0, reader.schema.name_to_number("body")))
        except OSError as err:
            failure = err
        assert failure is not None
        assert_doc_reader(reader, model, vectors=True)
        reader.close()
        current.clean_files()
        assert_files(current.storage, files_for(current))
        current.close()


def test_cleanup_delete_failure_is_retryable():
    with workspace() as root:
        current = make_index(root)
        model = snapshot_model(current, [document(i) for i in range(3)])
        reader = current.doc_reader()
        add_and_optimize(current, document(3))
        reader.close()
        victim = sorted(model["files"] - files_for(current))[0]
        current.storage.delete_failures.add(victim)
        try:
            current.clean_files()
        except OSError:
            pass
        assert victim in current.storage.list(), "fixture delete failure did not preserve failed file"
        current.storage.delete_failures.clear()
        current.clean_files()
        assert_files(current.storage, files_for(current))
        model_now = snapshot_model(current, [document(i) for i in range(4)])
        assert_index_view(current, model_now, vectors=True)
        current.close()


def test_cleanup_preserves_unrelated_files():
    with workspace() as root:
        current = make_index(root)
        names = ["notes.txt", "segment.backup", "123.txt", "123xdcz", "_MAIN_999.toc.backup"]
        for name in names:
            f = current.storage.create_file(name)
            f.write(b"unrelated")
            f.close()
        add_and_optimize(current, document(3))
        current.clean_files()
        for name in names:
            assert current.storage.file_exists(name), "cleanup removed unrelated file: " + name
        current.close()


def test_overlapping_generations_sequential_trace():
    rng = random.Random(81073)
    with workspace() as root:
        current = make_index(root)
        documents = [document(i) for i in range(3)]
        owners = []
        for step in range(7):
            for _ in range(2):
                parent = new_handle(current, alias=bool(rng.randrange(2)))
                model = snapshot_model(parent, documents)
                kind = rng.choice(["index", "doc_reader", "term_reader", "searcher"])
                obj = parent if kind == "index" else getattr(parent, kind)()
                if kind != "index":
                    parent.close()
                owners.append({"kind":kind,"obj":obj,"model":model,"closed":False})
            item = document(step + 3)
            add_and_optimize(current, item)
            documents.append(item)
            if step % 2:
                candidate = rng.choice([o for o in owners if not o["closed"]])
                candidate["obj"].close()
                candidate["obj"].close()
                candidate["closed"] = True
            current.clean_files()
            required = files_for(current)
            for owner in owners:
                if owner["closed"]:
                    continue
                required |= owner["model"]["files"]
                kind, obj, model = owner["kind"], owner["obj"], owner["model"]
                if kind == "index":
                    assert_index_view(obj, model, vectors=True)
                elif kind == "doc_reader":
                    assert_doc_reader(obj, model, vectors=True)
                elif kind == "term_reader":
                    assert_term_reader(obj, model)
                else:
                    assert_searcher(obj, model, vectors=True)
            assert_files(current.storage, required)
            assert_tocs(current.storage, {current.generation} | {o["model"]["generation"] for o in owners if not o["closed"] and o["kind"] == "index"})
        rng.shuffle(owners)
        for owner in owners:
            if not owner["closed"]:
                owner["obj"].close()
                owner["closed"] = True
            current.clean_files()
            required = files_for(current)
            for still_open in owners:
                if not still_open["closed"]:
                    required |= still_open["model"]["files"]
            assert_files(current.storage, required)
            assert_tocs(current.storage, {current.generation} | {o["model"]["generation"] for o in owners if not o["closed"] and o["kind"] == "index"})
        current.close()


TESTS = {name[5:]: obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--case", choices=sorted(TESTS))
    parser.add_argument("--output")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--baseline-only", action="store_true")
    args = parser.parse_args()
    if args.case:
        try:
            load_repo(args.repo)
            TESTS[args.case]()
            result = {"name": args.case, "passed": True}
        except Exception as error:
            result = {"name": args.case, "passed": False,
                      "error": type(error).__name__ + ": " + str(error)[:4000],
                      "traceback": traceback.format_exc(limit=14)}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["passed"] else 1
    cases = ["baseline_model"] if args.baseline_only else sorted(TESTS)
    results = []
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    for name in cases:
        command = [sys.executable, str(Path(__file__).resolve()), "--repo", str(Path(args.repo).resolve()), "--case", name]
        try:
            run = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout, env=env)
            lines = run.stdout.strip().splitlines()
            result = json.loads(lines[-1]) if lines else {"name": name, "passed": False, "error": "No verifier result"}
            if run.stderr.strip():
                result["stderr"] = run.stderr[-5000:]
            result["exit_code"] = run.returncode
            if run.returncode != 0:
                result["passed"] = False
        except Exception as error:
            result = {"name": name, "passed": False, "error": type(error).__name__ + ": " + str(error)}
        results.append(result)
    summary = {"suite": "whoosh-snapshot-v2", "repo": str(Path(args.repo).resolve()),
               "passed": sum(r["passed"] for r in results), "total": len(results),
               "success": all(r["passed"] for r in results), "results": results}
    encoded = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(encoded + "\n")
    print(encoded)
    return 0 if summary["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
