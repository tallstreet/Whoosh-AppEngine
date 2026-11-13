"""Visible regression coverage added before the v3 experiment is frozen."""
import unittest

from whoosh import fields, index, store
from whoosh.writing import NO_MERGE


class TestLegacyDirectAPI(unittest.TestCase):
    def setUp(self):
        self.ix = index.Index(store.RamStorage(),
                              schema=fields.Schema(uid=fields.ID(stored=True)),
                              create=True)
        writer = self.ix.writer()
        writer.add_document(uid="a")
        writer.add_document(uid="b")
        writer.commit(NO_MERGE)

    def tearDown(self):
        self.ix.close()

    def test_repeated_direct_term_deletion(self):
        self.assertEqual(self.ix.delete_by_term("uid", "a"), 1)
        self.assertEqual(self.ix.delete_by_term("uid", "a"), 0)

    def test_direct_find_tracks_edits_while_external_reader_keeps_view(self):
        reader = self.ix.doc_reader()
        try:
            self.assertEqual(reader[0]["uid"], "a")
            self.ix.delete_by_term("uid", "a")
            self.assertEqual(list(self.ix.find("uid:a")), [])
            self.assertEqual([d["uid"] for d in reader], ["a", "b"])
            self.assertEqual(reader[0]["uid"], "a")
            self.assertEqual([d["uid"] for d in self.ix.find("uid:b")], ["b"])
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main()
