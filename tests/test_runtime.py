"""Environment checks for the prepared repository; no index import is exercised."""
import tempfile
import unittest

from whoosh import analysis, fields, index, store, writing


def schema(vectors=True):
    analyzer = analysis.RegexAnalyzer()
    return fields.Schema(
        uid=fields.ID(stored=True),
        body=fields.TEXT(analyzer=analyzer, stored=True,
                         vector=fields.Positions(analyzer) if vectors else None),
        tags=fields.KEYWORD(scorable=True, stored=False),
    )


class RuntimeTests(unittest.TestCase):
    def check_roundtrip(self, storage):
        ix = index.Index(storage, schema(), create=True)
        for uid, body in [('a', 'alpha beta alpha'), ('b', 'gamma alpha')]:
            writer = ix.writer()
            writer.add_document(uid=uid, body=body, tags='one two')
            writer.commit(writing.NO_MERGE)
        ix.close()
        ix = index.Index(storage)
        self.assertEqual(ix.doc_count(), 2)
        body = ix.schema.name_to_number('body')
        with ix.doc_reader() as reader:
            self.assertEqual(reader[0]['uid'], 'a')
            self.assertEqual(reader.doc_field_length(0, body), 3)
            self.assertEqual(dict(reader.vector_as(0, body, 'positions')),
                             {'alpha': [0, 2], 'beta': [1]})
            self.assertEqual(dict(reader.vector_as(1, body, 'positions')),
                             {'gamma': [0], 'alpha': [1]})
        with ix.term_reader() as reader:
            self.assertEqual(list(reader.positions(body, 'alpha')),
                             [(0, [0, 2]), (1, [1])])
            self.assertEqual(list(reader.postings(body, 'alpha', exclude_docs={1})),
                             [(0, [0, 2])])
            self.assertEqual(reader.doc_frequency(body, 'alpha'), 2)
            self.assertEqual(reader.frequency(body, 'alpha'), 3)
            self.assertEqual(list(reader.lexicon(body)), ['alpha', 'beta', 'gamma'])
        writer = ix.writer()
        writer.delete_document(0)
        writer.commit(writing.NO_MERGE)
        self.assertEqual(ix.doc_count(), 1)
        with ix.term_reader() as reader:
            self.assertEqual(list(reader.positions(body, 'alpha')), [(1, [1])])
        ix.close()

    def test_ram_roundtrip(self):
        self.check_roundtrip(store.RamStorage())

    def test_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            self.check_roundtrip(store.FileStorage(directory))

    def test_empty_and_stored_only_segments(self):
        ix = index.Index(store.RamStorage(), schema(), create=True)
        with ix.doc_reader() as reader:
            self.assertEqual(list(reader), [])
        writer = ix.writer()
        writer.add_document(uid='only-id')
        writer.commit(writing.NO_MERGE)
        with ix.term_reader() as reader:
            self.assertEqual(list(reader.lexicon('body')), [])
        ix.close()

    def test_saturated_lengths_and_exact_original_totals(self):
        ix = index.Index(store.RamStorage(), schema(False), create=True)
        writer = ix.writer()
        writer.add_document(uid='long', tags=' '.join(['token'] * 70000))
        writer.commit(writing.NO_MERGE)
        with ix.doc_reader() as reader:
            self.assertEqual(reader.doc_field_length(0, 'tags'), 65535)
        self.assertEqual(ix.field_length('tags'), 70000)
        ix.close()

    def test_regular_compaction_without_vectors(self):
        ix = index.Index(store.RamStorage(), schema(False), create=True)
        for uid in ['a', 'b', 'c']:
            writer = ix.writer()
            writer.add_document(uid=uid, body='same term')
            writer.commit(writing.NO_MERGE)
        ix.optimize()
        self.assertEqual(len(ix.segments), 1)
        self.assertEqual(ix.doc_count(), 3)
        with ix.term_reader() as reader:
            self.assertEqual(reader.doc_frequency('body', 'same'), 3)
        ix.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
