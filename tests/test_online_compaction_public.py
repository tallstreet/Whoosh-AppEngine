"""Public smoke checks only. Hidden verification covers the full contract."""
import tempfile
import unittest
from whoosh import fields, index, store, writing


class TestCompactionPublic(unittest.TestCase):
    def test_api_and_basic_merge(self):
        from whoosh.compaction import CompactionConflict, CompactionStateError
        with tempfile.TemporaryDirectory() as folder:
            ix = index.Index(store.FileStorage(folder),
                             fields.Schema(key=fields.ID(stored=True), text=fields.TEXT(stored=True)),
                             create=True)
            try:
                for key in ('a', 'b'):
                    writer = ix.writer()
                    writer.add_document(key=key, text='hello '+key)
                    writer.commit(writing.NO_MERGE)
                job = ix.compaction([s.name for s in ix.segments])
                job.prepare()
                job.commit()
                self.assertEqual(ix.doc_count(), 2)
                self.assertEqual(len(ix.segments), 1)
                searcher = ix.searcher()
                try:
                    self.assertEqual({d['key'] for d in searcher.documents(text='hello')}, {'a','b'})
                finally:
                    searcher.close()
            finally:
                ix.close()

if __name__ == '__main__': unittest.main()
