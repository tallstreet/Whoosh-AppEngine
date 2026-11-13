import unittest

from whoosh import fields, index, qparser, searching, scoring, store, writing
from whoosh.query import *

class TestReading(unittest.TestCase):
    def setUp(self):
        s = fields.Schema(key = fields.ID(stored = True),
                          name = fields.TEXT,
                          value = fields.TEXT)
        st = store.RamStorage()
        ix = index.Index(st, s, create = True)
        
        w = writing.IndexWriter(ix)
        w.add_document(key = "A", name = "Yellow brown", value = "Blue red green render purple?")
        w.add_document(key = "B", name = "Alpha beta", value = "Gamma delta epsilon omega.")
        w.add_document(key = "C", name = "One two", value = "Three rendered four five.")
        w.add_document(key = "D", name = "Quick went", value = "Every red town.")
        w.add_document(key = "E", name = "Yellow uptown", value = "Interest rendering outer photo!")
        w.commit()
        
        self.ix = ix
    
    def _get_keys(self, stored_fields):
        return sorted([d.get("key") for d in stored_fields])
    
    def _docs(self, q, s):
        return self._get_keys([s.stored_fields(docnum) for docnum
                               in q.docs(s)])
    
    def _doc_scores(self, q, s, w):
        return self._get_keys([s.stored_fields(docnum) for docnum, score
                               in q.doc_scores(s, weighting = w)])
    
    def test_empty_index(self):
        schema = fields.Schema(key = fields.ID(stored=True), value = fields.TEXT)
        st = store.RamStorage()
        self.assertRaises(index.EmptyIndexError, index.Index, st, schema)
    
    def test_docs_method(self):
        s = self.ix.searcher()
        
        self.assertEqual(self._get_keys(s.documents(name = "yellow")), ["A", "E"])
        self.assertEqual(self._get_keys(s.documents(value = "red")), ["A", "D"])
    
    def test_queries(self):
        s = self.ix.searcher()
        
        tests = [
                 (Term("name", "yellow"),
                  ["A", "E"]),
                 (Term("value", "red"),
                  ["A", "D"]),
                 (Term("value", "zeta"),
                  []),
                 (Require([Term("value", "red"), Term("name", "yellow")]),
                  ["A"]),
                 (And([Term("value", "red"), Term("name", "yellow")]),
                  ["A"]),
                 (Or([Term("value", "red"), Term("name", "yellow")]),
                  ["A", "D", "E"]),
                 (Or([Term("value", "red"), Term("name", "yellow"), Not(Term("name", "quick"))]),
                  ["A", "E"]),
                 (AndNot(Term("name", "yellow"), Term("value", "purple")),
                  ["E"]),
                 (Variations("value", "render"), ["A", "C", "E"]),
                 (Or([Wildcard('value', '*red*'), Wildcard('name', '*yellow*')]),
                  ["A", "C", "D", "E"]),
                ]
        
        for query, result in tests:
            self.assertEqual(self._docs(query, s), result)
        
        for wcls in dir(scoring):
            if wcls is scoring.Weighting: continue
            if isinstance(wcls, scoring.Weighting):
                for query, result in tests:
                    self.assertEqual(self._doc_scores(query, s, wcls), result)
        
        for methodname in ("_docs", "_doc_scores"):
            method = getattr(self, methodname)

    def test_score_retrieval(self):
        schema = fields.Schema(title=fields.TEXT(stored=True),
                               content=fields.TEXT(stored=True))
        storage = store.RamStorage()
        ix = index.Index(storage, schema, create=True)
        writer = ix.writer()
        writer.add_document(title="Miss Mary",
                            content="Mary had a little white lamb its fleece was white as snow")
        writer.add_document(title="Snow White",
                            content="Snow white lived in the forrest with seven dwarfs")
        writer.commit()
        
        searcher = ix.searcher()
        results = searcher.search(Term("content", "white"))
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['title'], "Miss Mary")
        self.assertEqual(results[1]['title'], "Snow White")
        self.assertNotEqual(results.score(0), None)
        self.assertNotEqual(results.score(0), 0)
        self.assertNotEqual(results.score(0), 1)

    def test_missing_field_scoring(self):
        schema = fields.Schema(name=fields.TEXT(stored=True), hobbies=fields.TEXT(stored=True))
        storage = store.RamStorage()
        idx = index.Index(storage, schema, create=True)
        writer = idx.writer() 
        writer.add_document(name='Frank', hobbies='baseball, basketball')
        writer.commit()
        self.assertEqual(idx.segments[0].field_length(0), 2) # hobbies
        self.assertEqual(idx.segments[0].field_length(1), 1) # name
        
        writer = idx.writer()
        writer.add_document(name='Jonny') 
        writer.commit()
        self.assertEqual(len(idx.segments), 1)
        self.assertEqual(idx.segments[0].field_length(0), 2) # hobbies
        self.assertEqual(idx.segments[0].field_length(1), 2) # name
        
        parser = qparser.MultifieldParser(['name', 'hobbies'], schema=schema)
        searcher = idx.searcher()
        result = searcher.search(parser.parse('baseball'))
        self.assertEqual(len(result), 1)


if __name__ == '__main__':
    unittest.main()

