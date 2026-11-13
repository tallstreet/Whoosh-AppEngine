#!/usr/bin/env python3
"""Deterministic behavioral verifier for lossless Whoosh index import.

Expected records are computed from fixture token strings using only Python
containers, never by importing or re-indexing data through the submitted merge.
Each test executes in a fresh interpreter with its own temporary stores.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
import copy
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import traceback


def spec(kind="Frequency", *, stored=False, scorable=False, unique=False,
         vector=None, boost=1.0, vboost=1.0, lower=False, id_analyzer=False):
    return dict(kind=kind, stored=stored, scorable=scorable, unique=unique,
                vector=vector, boost=boost, vboost=vboost, lower=lower,
                id_analyzer=id_analyzer)


def uid():
    return spec("Existence", stored=True, unique=True, id_analyzer=True)


def schema_for(definitions):
    schema = fields.Schema()
    for name, s in definitions:
        def analyzer():
            if s["id_analyzer"]:
                return analysis.IDAnalyzer(lowercase=s["lower"])
            return analysis.KeywordAnalyzer(lowercase=s["lower"])
        fmt = fields.Stored() if s["kind"] == "Stored" else getattr(fields, s["kind"])(analyzer(), field_boost=s["boost"])
        vector = (getattr(fields, s["vector"])(analyzer(), field_boost=s["vboost"])
                  if s["vector"] else None)
        schema.add(name, fields.FieldType(fmt, vector=vector, stored=s["stored"],
                                         scorable=s["scorable"], unique=s["unique"]))
    return schema


def tokens(value, s):
    items = [value.strip()] if s["id_analyzer"] else value.split()
    return [x.lower() for x in items] if s["lower"] else items


def payload(kind, words):
    if kind == "Existence":
        return {word: None for word in words}
    if kind == "Frequency":
        return dict(Counter(words))
    if kind == "Positions":
        result = defaultdict(list)
        for position, word in enumerate(words):
            result[word].append(position)
        return dict(result)
    return {}


def expected_document(definitions, values, destination=None):
    """Model the documented add_document semantics for controlled fixtures."""
    destination = definitions if destination is None else destination
    record = {"stored": {}, "postings": {}, "vectors": {}, "weights": {}, "vector_weights": {},
              "lengths": {name: 0 for name, s in destination if s["scorable"]}}
    for name, s in definitions:
        value = values.get(name)
        if not value:
            continue
        if s["stored"]:
            record["stored"][name] = values.get("_stored_" + name, value)
        if s["kind"] == "Stored":
            continue
        words = tokens(value, s)
        data = payload(s["kind"], words)
        if data:
            record["postings"][name] = data
            record["weights"][name] = {
                word: (1 if s["kind"] == "Existence" else len(raw) if s["kind"] == "Positions" else raw) * s["boost"]
                for word, raw in data.items()}
        if s["scorable"]:
            length = len(data) if s["kind"] == "Existence" else len(words)
            record["lengths"][name] = min(length, 65535)
        if s["vector"]:
            record["vectors"][name] = payload(s["vector"], words)
            record["vector_weights"][name] = {
                word: (len(raw) if s["vector"] == "Positions" else raw) * s["vboost"]
                for word, raw in record["vectors"][name].items()}
    return record


def normalized(record):
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def describe_difference(expected, actual):
    missing = list((expected - actual).elements())
    extra = list((actual - expected).elements())
    return "missing=%r extra=%r" % ([x[:1200] for x in missing[:2]], [x[:1200] for x in extra[:2]])


def actual_records(ix):
    records = []
    term_metadata = defaultdict(lambda: [0, 0])
    for segment in ix.segments:
        dr = reading.DocReader(ix.storage, segment, ix.schema)
        tr = reading.TermReader(ix.storage, segment, ix.schema)
        try:
            local = {}
            for docnum in range(segment.max_doc):
                if segment.is_deleted(docnum):
                    continue
                local[docnum] = {
                    "stored": dr[docnum], "postings": {}, "vectors": {}, "weights": {}, "vector_weights": {},
                    "lengths": {ix.schema.number_to_name(fn): dr.doc_field_length(docnum, fn)
                                for fn in ix.schema.scorable_fields()},
                }
            for fn, term, df, cf in tr:
                name = ix.schema.number_to_name(fn)
                term_metadata[(name, term)][0] += df
                term_metadata[(name, term)][1] += cf
                for docnum, data in tr.postings(fn, term):
                    if docnum not in local:
                        raise AssertionError("posting refers to invalid/deleted document %r" % docnum)
                    local[docnum]["postings"].setdefault(name, {})[term] = data
                for docnum, weight in tr.weights(fn, term):
                    if docnum not in local:
                        raise AssertionError("weight refers to invalid/deleted document %r" % docnum)
                    local[docnum]["weights"].setdefault(name, {})[term] = weight
            if ix.schema.vectored_fields():
                dr._open_vectors()
                # Iterating the keys safely handles empty vector tables without
                # depending on the original TableReader.__contains__ bug.
                keys = set(dr.vector_table.keys())
                for docnum, record in local.items():
                    for fn in ix.schema.vectored_fields():
                        if (docnum, fn) in keys:
                            name = ix.schema.number_to_name(fn)
                            record["vectors"][name] = dict(dr.vector(docnum, fn))
                            record["vector_weights"][name] = dict(dr.vector_as(docnum, fn, "weight"))
            records.extend(local.values())
        finally:
            tr.close()
            dr.close()
    return records, dict(term_metadata)


def expected_statistics(records, definitions):
    definitions = dict(definitions)
    lengths = Counter()
    terms = defaultdict(lambda: [0, 0])
    for record in records:
        for name, posting in record["postings"].items():
            kind = definitions[name]["kind"]
            for term, data in posting.items():
                frequency = 1 if kind == "Existence" else (len(data) if kind == "Positions" else data)
                lengths[name] += frequency
                terms[(name, term)][0] += 1
                # Existence.write_postvalue encodes no frequency and returns 0;
                # the API's term-table statistic is distinct from field length.
                terms[(name, term)][1] += 0 if kind == "Existence" else frequency
    return lengths, dict(terms)


def assert_index(ix, expected, definitions, *, exact_stats=True, compacted=False,
                 physical_count=True):
    records, term_metadata = actual_records(ix)
    actual = Counter(map(normalized, records))
    wanted = Counter(map(normalized, expected))
    assert actual == wanted, describe_difference(wanted, actual)
    assert ix.doc_count() == len(expected), ("live count", ix.doc_count(), len(expected))
    if physical_count:
        assert ix.doc_count_all() == len(expected), ("physical document count", ix.doc_count_all(), len(expected))
    if exact_stats:
        totals, expected_terms = expected_statistics(expected, definitions)
        assert term_metadata == expected_terms, ("term metadata", term_metadata, expected_terms)
        for name, s in definitions:
            assert ix.field_length(name) == totals[name], ("field length", name, ix.field_length(name), totals[name])
    if compacted:
        if expected:
            assert len(ix.segments) == 1, "nonempty optimize must produce one segment"
        else:
            assert len(ix.segments) <= 1, "empty optimized representation has too many segments"
        assert all(not segment.has_deletions() for segment in ix.segments), "compacted output retains tombstones"


@contextmanager
def stores():
    with tempfile.TemporaryDirectory(prefix="whoosh-import-verifier-") as temp:
        yield Path(temp)


def build(root, name, definitions, batches=(), deleted=(), *, ram=False):
    if ram:
        storage = store.RamStorage()
    else:
        folder = root / name
        folder.mkdir()
        storage = store.FileStorage(str(folder))
    ix = index.Index(storage, schema_for(definitions), create=True)
    for batch in batches:
        w = ix.writer()
        for values in batch:
            w.add_document(**values)
        w.commit(writing.NO_MERGE)
    if deleted:
        w = ix.writer()
        for docnum in deleted:
            w.delete_document(docnum)
        w.commit(writing.NO_MERGE)
    return ix


def reopen(ix):
    storage = ix.storage
    ix.close()
    return index.Index(storage)


def byte_snapshot(ix):
    data = {}
    for name in sorted(ix.storage.list()):
        f = ix.storage.open_file(name)
        try:
            raw = f.read(ix.storage.file_length(name))
        finally:
            f.close()
        data[name] = hashlib.sha256(raw).hexdigest()
    return data


def import_unchanged(writer, source):
    before = byte_snapshot(source)
    reader_before = actual_records(source)[0]
    names_before = list(source.schema.field_names())
    writer.add_index(source)
    assert byte_snapshot(source) == before, "import changed source files"
    assert Counter(map(normalized, actual_records(source)[0])) == Counter(map(normalized, reader_before)), "import changed source reader results"
    assert list(source.schema.field_names()) == names_before, "import mutated source schema"


def test_baseline_model():
    definitions = [("id", uid()), ("body", spec("Positions", scorable=True, stored=True, vector="Positions")),
                   ("tags", spec(scorable=False, vector="Frequency", boost=2.0)),
                   ("flags", spec("Existence")), ("note", spec("Stored", stored=True))]
    values = [{"id": "base", "body": "Red blue Red", "_stored_body": "DISPLAY ONLY",
               "tags": "a a b", "flags": "a a b", "note": {"nested": [1, 2]}},
              {"id": "missing", "tags": "only"}]
    with stores() as root:
        ix = build(root, "baseline", definitions, [values])
        assert_index(ix, [expected_document(definitions, x) for x in values], definitions)
        ix = reopen(ix)
        assert_index(ix, [expected_document(definitions, x) for x in values], definitions)


def test_simple_import():
    definitions = [("id", uid()), ("body", spec(scorable=True))]
    values = [{"id": "one", "body": "alpha alpha beta"}, {"id": "two", "body": "gamma"}]
    with stores() as root:
        src = build(root, "src", definitions, [values])
        dst = build(root, "dst", definitions)
        w = dst.writer()
        import_unchanged(w, src)
        w.commit(writing.NO_MERGE)
        assert_index(dst, [expected_document(definitions, x) for x in values], definitions)


def test_permuted_vectors_and_lengths():
    source = [("body", spec("Positions", scorable=True, stored=True, vector="Positions", boost=2.0, vboost=1.5)),
              ("id", uid()), ("tags", spec(scorable=True, vector="Frequency")),
              ("note", spec("Stored", stored=True))]
    byname = dict(source)
    destination = [("tags", copy.deepcopy(byname["tags"])), ("extra", spec(scorable=True, vector="Frequency", stored=True)),
                   ("note", copy.deepcopy(byname["note"])), ("id", copy.deepcopy(byname["id"])),
                   ("body", copy.deepcopy(byname["body"]))]
    imported = [{"id": "s1", "body": "Blue Red Blue", "_stored_body": "wrong text for reindexing",
                 "tags": "one two two", "note": {"n": 9}},
                {"id": "s2", "tags": "three three"}, {"id": "s3", "body": "green"}]
    existing = {"id": "existing", "extra": "only destination", "body": "old words"}
    pending = {"id": "pending", "body": "pending tokens", "tags": "last"}
    after = {"id": "after", "extra": "after extra"}
    with stores() as root:
        src = reopen(build(root, "src", source, [imported]))
        dst = reopen(build(root, "dst", destination, [[existing]]))
        w = dst.writer()
        w.add_document(**pending)
        import_unchanged(w, src)
        w.add_document(**after)
        w.commit(writing.NO_MERGE)
        expected = [expected_document(destination, v) for v in [existing, pending, after]]
        expected += [expected_document(source, v, destination) for v in imported]
        assert_index(dst, expected, destination)
        dst = reopen(dst)
        assert_index(dst, expected, destination)


def test_deletions_across_segments():
    source = [("text", spec("Positions", scorable=True, vector="Positions")), ("id", uid()),
              ("tags", spec())]
    destination = [source[2], ("unused", spec(scorable=True)), source[1], source[0]]
    values = [{"id": "doc%d" % i, "text": "word%d shared shared" % i, "tags": "k%d k%d" % (i, i)} for i in range(8)]
    deleted = {0, 2, 5, 7}
    with stores() as root:
        src = reopen(build(root, "src", source, [values[:3], values[3:5], values[5:]], deleted=sorted(deleted)))
        dst = build(root, "dst", destination)
        w = dst.writer()
        import_unchanged(w, src)
        w.commit(writing.NO_MERGE)
        expected = [expected_document(source, v, destination) for i, v in enumerate(values) if i not in deleted]
        assert_index(dst, expected, destination)
        dst.optimize()
        assert_index(reopen(dst), expected, destination, compacted=True)


def test_analyzer_independent_import():
    source = [("id", uid()), ("text", spec("Positions", scorable=True, vector="Frequency", lower=False))]
    destination = [("text", dict(source[1][1], lower=True)), ("id", uid())]
    values = [{"id": "source", "text": "UPPER lower UPPER"}]
    added = {"id": "new", "text": "UPPER lower"}
    with stores() as root:
        src = reopen(build(root, "src", source, [values]))
        dst = reopen(build(root, "dst", destination))
        w = dst.writer()
        import_unchanged(w, src)
        w.add_document(**added)
        w.commit(writing.NO_MERGE)
        expected = [expected_document(source, values[0], destination), expected_document(destination, added)]
        assert_index(reopen(dst), expected, destination)


def test_exact_lengths_and_existence():
    source = [("id", uid()), ("body", spec(scorable=True)), ("tags", spec()),
              ("flags", spec("Existence"))]
    destination = [source[3], source[2], ("spare", spec(scorable=True)), source[0], source[1]]
    values = [{"id": "keep", "body": " ".join(["long"] * 70000), "tags": "a a a b", "flags": "x x y"},
              {"id": "gone", "body": "long long long", "tags": "dead dead", "flags": "dead"}]
    with stores() as root:
        src = build(root, "src", source, [values], deleted=[1])
        dst = build(root, "dst", destination)
        w = dst.writer()
        import_unchanged(w, src)
        w.commit(writing.NO_MERGE)
        expected = [expected_document(source, values[0], destination)]
        assert_index(dst, expected, destination)
        assert dst.field_length("body") == 70000


def test_multiple_sources_and_duplicate_occurrences():
    destination = [("id", uid()), ("body", spec("Positions", scorable=True, vector="Positions")),
                   ("tags", spec(vector="Frequency")), ("extra", spec(scorable=True))]
    byname = dict(destination)
    source1 = [("body", byname["body"]), ("id", byname["id"])]
    source2 = [("tags", byname["tags"]), ("id", byname["id"]), ("body", byname["body"])]
    docs1 = [{"id": "duplicate", "body": "one two one"}]
    docs2 = [{"id": "duplicate", "tags": "x x", "body": "other body"}]
    before = {"id": "before", "extra": "before before"}
    after = {"id": "after", "extra": "after"}
    with stores() as root:
        src1 = build(root, "s1", source1, [docs1])
        src2 = build(root, "s2", source2, [docs2])
        dst = build(root, "dst", destination)
        w = dst.writer()
        w.add_document(**before)
        import_unchanged(w, src1)
        import_unchanged(w, src2)
        import_unchanged(w, src1)
        w.add_document(**after)
        w.commit(writing.NO_MERGE)
        expected = [expected_document(destination, x) for x in (before, after)]
        expected += [expected_document(source1, docs1[0], destination)] * 2
        expected += [expected_document(source2, docs2[0], destination)]
        assert_index(dst, expected, destination)
        dst.optimize()
        assert_index(reopen(dst), expected, destination, compacted=True)


def test_low_level_import_and_validation():
    source = [("body", spec("Positions", scorable=True, vector="Positions")), ("id", uid())]
    destination = [source[1], ("extra", spec(scorable=True)), source[0]]
    bad_schema = source + [("unknown", spec())]
    document = {"id": "import", "body": "original tokens original"}
    pending = {"id": "pending", "extra": "pending extra"}
    after = {"id": "after", "body": "after"}
    with stores() as root:
        src = build(root, "src", source, [[document]])
        bad = build(root, "bad", bad_schema, [[{"id": "bad", "body": "bad tokens", "unknown": "bad"}]])
        dst = build(root, "dst", destination)
        w = dst.writer()
        w.add_document(**pending)
        segment_writer = w.segment_writer()
        import_unchanged(segment_writer, src)
        try:
            segment_writer.add_index(bad)
        except writing.IndexingError:
            pass
        else:
            raise AssertionError("low-level import accepted an incompatible schema")
        w.add_document(**after)
        w.commit(writing.NO_MERGE)
        expected = [expected_document(destination, x) for x in (pending, after)]
        expected += [expected_document(source, document, destination)]
        assert_index(dst, expected, destination)


def test_ordinary_merge_after_import():
    source = [("body", spec("Positions", scorable=True, vector="Positions")), ("id", uid())]
    destination = [source[1], ("extra", spec(scorable=True)), source[0]]
    imported = {"id": "source", "body": "imported imported source"}
    old = [{"id": "old%d" % i, "body": "old%d old" % i, "extra": "extra"} for i in range(3)]
    pending = {"id": "pending", "body": "pending"}
    with stores() as root:
        src = build(root, "src", source, [[imported]])
        dst = build(root, "dst", destination, [[x] for x in old])
        w = dst.writer()
        w.add_document(**pending)
        import_unchanged(w, src)
        w.commit()  # Exercise the existing MERGE_SMALL default.
        expected = [expected_document(destination, x) for x in old + [pending]]
        expected += [expected_document(source, imported, destination)]
        assert_index(reopen(dst), expected, destination)


def rejection_case(change, *, empty=False):
    destination = [("id", uid()), ("body", spec("Positions", stored=True, scorable=True, vector="Frequency"))]
    bad = copy.deepcopy(destination)
    change(bad)
    gooddoc = {"id": "good", "body": "keep this"}
    pending = {"id": "pending", "body": "pending document"}
    after = {"id": "after", "body": "after document"}
    with stores() as root:
        src = build(root, "bad", bad, [] if empty else [[{"id": "bad", "body": "reject me", "unknown": "unknown"} if any(n == "unknown" for n, _ in bad) else {"id": "bad", "body": "reject me"}]])
        good = build(root, "good", destination, [[gooddoc]])
        dst = build(root, "dst", destination)
        w = dst.writer()
        w.add_document(**pending)
        try:
            w.add_index(src)
        except writing.IndexingError:
            pass
        else:
            raise AssertionError("incompatible schema did not raise IndexingError")
        import_unchanged(w, good)
        w.add_document(**after)
        w.commit(writing.NO_MERGE)
        assert_index(dst, [expected_document(destination, x) for x in (pending, gooddoc, after)], destination)


def test_reject_unknown_field():
    rejection_case(lambda s: s.append(("unknown", spec())))


def test_reject_empty_unknown_field():
    rejection_case(lambda s: s.append(("unknown", spec())), empty=True)


def test_reject_codec_mismatch():
    rejection_case(lambda s: s[1][1].update(kind="Frequency"))


def test_reject_posting_boost():
    rejection_case(lambda s: s[1][1].update(boost=2.0))


def test_reject_vector_presence():
    rejection_case(lambda s: s[1][1].update(vector=None))


def test_reject_vector_codec():
    rejection_case(lambda s: s[1][1].update(vector="Positions"))


def test_reject_vector_boost():
    rejection_case(lambda s: s[1][1].update(vboost=2.0))


def test_reject_stored_flag():
    rejection_case(lambda s: s[1][1].update(stored=False))


def test_reject_scorable_flag():
    rejection_case(lambda s: s[1][1].update(scorable=False))


def test_reject_unique_flag():
    rejection_case(lambda s: s[1][1].update(unique=True))


def test_empty_and_all_deleted():
    source = [("body", spec("Positions", vector="Positions", scorable=True)), ("id", uid())]
    destination = [source[1], ("extra", spec(scorable=True)), source[0]]
    with stores() as root:
        empty = build(root, "empty", source)
        dead = build(root, "dead", source, [[{"id": "dead", "body": "removed"}]], deleted=[0])
        dst = build(root, "dst", destination)
        w = dst.writer()
        import_unchanged(w, empty)
        import_unchanged(w, dead)
        w.commit(writing.NO_MERGE)
        assert_index(dst, [], destination)
        dst.optimize()
        assert_index(reopen(dst), [], destination, compacted=True)


def test_missing_vectors_and_stored_only():
    source = [("body", spec("Positions", vector="Positions", scorable=True)), ("note", spec("Stored", stored=True))]
    destination = [source[1], ("extra", spec(scorable=True)), source[0]]
    documents = [{"note": {"values": [0, "text", None]}}]
    with stores() as root:
        src = build(root, "src", source, [documents])
        dst = build(root, "dst", destination)
        w = dst.writer()
        import_unchanged(w, src)
        w.commit(writing.NO_MERGE)
        assert_index(dst, [expected_document(source, documents[0], destination)], destination)


def test_composed_history_and_optimize():
    source = [("body", spec("Positions", scorable=True, vector="Positions")), ("id", uid()),
              ("tags", spec(vector="Frequency"))]
    destination = [source[2], ("extra", spec(scorable=True)), source[1], source[0]]
    docs = [{"id": "s%d" % n, "body": "blue blue token%d" % n, "tags": "t%d" % n} for n in range(6)]
    pending = {"id": "pending", "extra": "extra extra", "body": "pending"}
    existing = {"id": "existing", "body": "old"}
    with stores() as root:
        src = build(root, "src", source, [docs[:2], docs[2:]], deleted=[1, 4])
        dst = build(root, "dst", destination, [[existing]])
        w = dst.writer()
        w.add_document(**pending)
        import_unchanged(w, src)
        w.commit(writing.NO_MERGE)
        expected = [expected_document(destination, x) for x in [existing, pending]]
        expected += [expected_document(source, d, destination) for i, d in enumerate(docs) if i not in {1, 4}]
        dst = reopen(dst)
        assert_index(dst, expected, destination)
        # Delete by the physical number discovered through ordinary stored data.
        offset = 0
        victim = None
        for segment in dst.segments:
            dr = reading.DocReader(dst.storage, segment, dst.schema)
            try:
                for local in range(segment.max_doc):
                    if dr[local].get("id") == "s2":
                        victim = offset + local
            finally:
                dr.close()
            offset += segment.max_doc
        assert victim is not None
        w = dst.writer()
        w.delete_document(victim)
        w.commit(writing.NO_MERGE)
        expected = [x for x in expected if x["stored"].get("id") != "s2"]
        assert_index(dst, expected, destination, exact_stats=False, physical_count=False)
        dst.optimize()
        dst = reopen(dst)
        assert_index(dst, expected, destination, compacted=True)
        # A second optimization is allowed to be a no-op.
        dst.optimize()
        assert_index(reopen(dst), expected, destination, compacted=True)


def test_seeded_permutations():
    rng = random.Random(517093)
    for trial in range(8):
        common = [("id", uid()), ("body", spec("Positions", scorable=True, vector="Positions")),
                  ("tags", spec(scorable=True, vector="Frequency")), ("flags", spec("Existence"))]
        source = copy.deepcopy(common)
        rng.shuffle(source)
        destination = copy.deepcopy(common) + [("extra", spec(scorable=True))]
        rng.shuffle(destination)
        documents = [{"id": "p%d-%d" % (trial, n), "body": " ".join(rng.choice(["a", "b", "c"]) for _ in range(rng.randint(1, 10))),
                      "tags": " ".join(rng.choice(["x", "y"]) for _ in range(rng.randint(1, 5))),
                      "flags": "q q r"} for n in range(7)]
        deleted = sorted(rng.sample(range(7), 3))
        with stores() as root:
            src = build(root, "src", source, [documents[:3], documents[3:]], deleted=deleted, ram=(trial % 2 == 0))
            dst = build(root, "dst", destination, ram=(trial % 2 == 1))
            w = dst.writer()
            import_unchanged(w, src)
            w.commit(writing.NO_MERGE)
            expected = [expected_document(source, d, destination) for n, d in enumerate(documents) if n not in deleted]
            assert_index(dst, expected, destination)


TESTS = {name[5:]: obj for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj)}


def load_repo(repo):
    global fields, analysis, index, store, writing, reading
    sys.path.insert(0, str(Path(repo).resolve() / "src"))
    from whoosh import fields, analysis, index, store, writing, reading


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
            result = {"name": args.case, "passed": False, "error": type(error).__name__ + ": " + str(error)[:4000],
                      "traceback": traceback.format_exc(limit=12)}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["passed"] else 1
    import os
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
    summary = {"suite": "whoosh-lossless-import-v1", "repo": str(Path(args.repo).resolve()),
               "passed": sum(r["passed"] for r in results), "total": len(results),
               "success": all(r["passed"] for r in results), "results": results}
    encoded = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(encoded + "\n")
    print(encoded)
    return 0 if summary["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
