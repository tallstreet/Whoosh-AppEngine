import unittest
from os import mkdir
from os.path import exists
from shutil import rmtree

from whoosh import fields, index, qparser, store, writing

class TestIndexing(unittest.TestCase):
    def make_index(self, dirname, schema):
        if not exists(dirname):
            mkdir(dirname)
        st = store.FileStorage(dirname)
        ix = index.Index(st, schema, create = True)
        return ix
    
    def destroy_index(self, dirname):
        if exists(dirname):
            rmtree(dirname)
    
    def test_creation(self):
        s = fields.Schema()
        s.add("content", fields.TEXT(phrase = True))
        s.add("title", fields.TEXT(stored = True))
        s.add("path", fields.ID(stored = True))
        s.add("tags", fields.KEYWORD(stored = True))
        s.add("quick", fields.NGRAM)
        s.add("note", fields.STORED)
        st = store.RamStorage()
        
        ix = index.Index(st, s, create = True)
        w = writing.IndexWriter(ix)
        w.add_document(title = "First", content = "This is the first document", path = "/a",
                       tags = "first second third", quick = "First document", note = "This is the first document")
        w.start_document()
        w.add_field("content", "Let's try this again")
        w.add_field("title", "Second")
        w.add_field("path", "/b")
        w.add_field("tags", "Uno Dos Tres")
        w.add_field("quick", "Second document")
        w.add_field("note", "This is the second document")
        w.end_document()
        
        w.commit()
        
    def test_integrity(self):
        s = fields.Schema(name = fields.TEXT, value = fields.TEXT)
        st = store.RamStorage()
        ix = index.Index(st, s, create = True)
        
        w = writing.IndexWriter(ix)
        w.add_document(name = "Yellow brown", value = "Blue red green purple?")
        w.add_document(name = "Alpha beta", value = "Gamma delta epsilon omega.")
        w.commit()
        
        w = writing.IndexWriter(ix)
        w.add_document(name = "One two", value = "Three four five.")
        w.commit()
        
        tr = ix.term_reader()
        self.assertEqual(ix.doc_count_all(), 3)
        self.assertEqual(list(tr.lexicon("name")), ["alpha", "beta", "brown", "one", "two", "yellow"])
    
    def test_lengths(self):
        s = fields.Schema(f1 = fields.KEYWORD(stored = True, scorable = True),
                          f2 = fields.KEYWORD(stored = True, scorable = True))
        ix = self.make_index("testindex", s)
        
        try:
            w = ix.writer()
            tokens = "ABCDEFG"
            from itertools import cycle, islice
            lengths = [10, 20, 2, 102, 45, 3, 420, 2]
            for length in lengths:
                w.add_document(f2 = " ".join(islice(cycle(tokens), length)))
            w.commit()
            dr = ix.doc_reader()
            ls1 = [dr.doc_field_length(i, "f1") for i in range(0, len(lengths))]
            ls2 = [dr.doc_field_length(i, "f2") for i in range(0, len(lengths))]
            self.assertEqual(ls1, [0]*len(lengths))
            self.assertEqual(ls2, lengths)
            dr.close()
            
            ix.close()
        finally:
            self.destroy_index("testindex")
    
    def test_lengths_ram(self):
        s = fields.Schema(f1 = fields.KEYWORD(stored = True, scorable = True),
                          f2 = fields.KEYWORD(stored = True, scorable = True))
        st = store.RamStorage()
        ix = index.Index(st, s, create = True)
        w = writing.IndexWriter(ix)
        w.add_document(f1 = "A B C D E", f2 = "X Y Z")
        w.add_document(f1 = "B B B B C D D Q", f2 = "Q R S T")
        w.add_document(f1 = "D E F", f2 = "U V A B C D E")
        w.commit()
        
        dr = ix.doc_reader()
        ls1 = [dr.doc_field_length(i, "f1") for i in range(0, 3)]
        ls2 = [dr.doc_field_length(i, "f2") for i in range(0, 3)]
        self.assertEqual(dr[0]["f1"], "A B C D E")
        self.assertEqual(dr.doc_field_length(0, "f1"), 5)
        self.assertEqual(dr.doc_field_length(1, "f1"), 8)
        self.assertEqual(dr.doc_field_length(2, "f1"), 3)
        self.assertEqual(dr.doc_field_length(0, "f2"), 3)
        self.assertEqual(dr.doc_field_length(1, "f2"), 4)
        self.assertEqual(dr.doc_field_length(2, "f2"), 7)
        
        self.assertEqual(ix.field_length("f1"), 16)
        self.assertEqual(ix.field_length("f2"), 14)
        
    def test_merged_lengths(self):
        s = fields.Schema(f1 = fields.KEYWORD(stored = True, scorable = True),
                          f2 = fields.KEYWORD(stored = True, scorable = True))
        st = store.RamStorage()
        ix = index.Index(st, s, create = True)
        w = writing.IndexWriter(ix)
        w.add_document(f1 = "A B C", f2 = "X")
        w.add_document(f1 = "B C D E", f2 = "Y Z")
        w.commit()
        
        w = writing.IndexWriter(ix)
        w.add_document(f1 = "A", f2 = "B C D E X Y")
        w.add_document(f1 = "B C", f2 = "X")
        w.commit(writing.NO_MERGE)
        
        w = writing.IndexWriter(ix)
        w.add_document(f1 = "A B X Y Z", f2 = "B C")
        w.add_document(f1 = "Y X", f2 = "A B")
        w.commit(writing.NO_MERGE)
        
        dr = ix.doc_reader()
        self.assertEqual(dr[0]["f1"], "A B C")
        self.assertEqual(dr.doc_field_length(0, "f1"), 3)
        self.assertEqual(dr.doc_field_length(2, "f2"), 6)
        self.assertEqual(dr.doc_field_length(4, "f1"), 5)
        
    def test_frequency_keyword(self):
        s = fields.Schema(content = fields.KEYWORD)
        st = store.RamStorage()
        ix = index.Index(st, s, create = True)
        
        w = ix.writer()
        w.add_document(content = "A B C D E")
        w.add_document(content = "B B B B C D D")
        w.add_document(content = "D E F")
        w.commit()
        
        tr = ix.term_reader()
        self.assertEqual(tr.doc_frequency("content", "B"), 2)
        self.assertEqual(tr.frequency("content", "B"), 5)
        self.assertEqual(tr.doc_frequency("content", "E"), 2)
        self.assertEqual(tr.frequency("content", "E"), 2)
        self.assertEqual(tr.doc_frequency("content", "A"), 1)
        self.assertEqual(tr.frequency("content", "A"), 1)
        self.assertEqual(tr.doc_frequency("content", "D"), 3)
        self.assertEqual(tr.frequency("content", "D"), 4)
        self.assertEqual(tr.doc_frequency("content", "F"), 1)
        self.assertEqual(tr.frequency("content", "F"), 1)
        self.assertEqual(tr.doc_frequency("content", "Z"), 0)
        self.assertEqual(tr.frequency("content", "Z"), 0)
        self.assertEqual(list(tr), [(0, "A", 1, 1), (0, "B", 2, 5),
                                    (0, "C", 2, 2), (0, "D", 3, 4),
                                    (0, "E", 2, 2), (0, "F", 1, 1)])
        
    def test_frequency_text(self):
        s = fields.Schema(content = fields.KEYWORD)
        st = store.RamStorage()
        ix = index.Index(st, s, create = True)
        
        w = ix.writer()
        w.add_document(content = "alfa bravo charlie delta echo")
        w.add_document(content = "bravo bravo bravo bravo charlie delta delta")
        w.add_document(content = "delta echo foxtrot")
        w.commit()
        
        tr = ix.term_reader()
        self.assertEqual(tr.doc_frequency("content", "bravo"), 2)
        self.assertEqual(tr.frequency("content", "bravo"), 5)
        self.assertEqual(tr.doc_frequency("content", "echo"), 2)
        self.assertEqual(tr.frequency("content", "echo"), 2)
        self.assertEqual(tr.doc_frequency("content", "alfa"), 1)
        self.assertEqual(tr.frequency("content", "alfa"), 1)
        self.assertEqual(tr.doc_frequency("content", "delta"), 3)
        self.assertEqual(tr.frequency("content", "delta"), 4)
        self.assertEqual(tr.doc_frequency("content", "foxtrot"), 1)
        self.assertEqual(tr.frequency("content", "foxtrot"), 1)
        self.assertEqual(tr.doc_frequency("content", "zulu"), 0)
        self.assertEqual(tr.frequency("content", "zulu"), 0)
        self.assertEqual(list(tr), [(0, "alfa", 1, 1), (0, "bravo", 2, 5),
                                    (0, "charlie", 2, 2), (0, "delta", 3, 4),
                                    (0, "echo", 2, 2), (0, "foxtrot", 1, 1)])
    
    def test_deletion(self):
        s = fields.Schema(key = fields.ID, name = fields.TEXT, value = fields.TEXT)
        st = store.RamStorage()
        ix = index.Index(st, s, create = True)
        
        w = writing.IndexWriter(ix)
        w.add_document(key = "A", name = "Yellow brown", value = "Blue red green purple?")
        w.add_document(key = "B", name = "Alpha beta", value = "Gamma delta epsilon omega.")
        w.add_document(key = "C", name = "One two", value = "Three four five.")
        w.commit()
        
        count = ix.delete_by_term("key", "B")
        self.assertEqual(count, 1)
        ix.commit()
        
        self.assertEqual(ix.doc_count_all(), 3)
        self.assertEqual(ix.doc_count(), 2)
        
        ix.optimize()
        self.assertEqual(ix.doc_count(), 2)
        tr = ix.term_reader()
        self.assertEqual(list(tr.lexicon("name")), ["brown", "one", "two", "yellow"])

    def test_update(self):
        # Test update with multiple unique keys
        SAMPLE_DOCS = [{"id": "test1", "path": "/test/1", "text": "Hello"},
                       {"id": "test2", "path": "/test/2", "text": "There"},
                       {"id": "test3", "path": "/test/3", "text": "Reader"},
                       ]
        
        schema = fields.Schema(id=fields.ID(unique=True, stored=True),
                               path=fields.ID(unique=True, stored=True),
                               text=fields.TEXT)
        ix = self.make_index("testindex", schema)
        try:
            writer = ix.writer()
            for doc in SAMPLE_DOCS:
                writer.add_document(**doc)
            writer.commit()
            
            writer = ix.writer()
            writer.update_document(**{"id": "test2",
                                      "path": "test/1",
                                      "text": "Replacement"})
            writer.commit()
            ix.close()
        finally:
            self.destroy_index("testindex")

    def test_reindex(self):
        SAMPLE_DOCS = [
            {'id': 'test1', 'text': 'This is a document. Awesome, is it not?'},
            {'id': 'test2', 'text': 'Another document. Astounding!'},
            {'id': 'test3', 'text': 'A fascinating article on the behavior of domestic steak knives.'},
        ]

        schema = fields.Schema(text=fields.TEXT(stored=True),
                               id=fields.ID(unique=True, stored=True))
        ix = self.make_index("testindex", schema)
        try:
            def reindex():
                writer = ix.writer()
            
                for doc in SAMPLE_DOCS:
                    writer.update_document(**doc)
            
                writer.commit()

            reindex()
            self.assertEqual(ix.doc_count_all(), 3)
            reindex()
            
            ix.close()
            
        finally:
            self.destroy_index("testindex")



if __name__ == '__main__':
    unittest.main()

