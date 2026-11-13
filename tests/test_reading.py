import unittest

from whoosh import analysis, fields, index, store, writing

class TestReading(unittest.TestCase):
    def _create_index(self):
        s = fields.Schema(f1 = fields.KEYWORD(stored = True),
                          f2 = fields.KEYWORD,
                          f3 = fields.KEYWORD)
        st = store.RamStorage()
        ix = index.Index(st, s, create = True)
        return ix
    
    def _one_segment_index(self):
        ix = self._create_index()
        w = writing.IndexWriter(ix)
        w.add_document(f1 = "A B C", f2 = "1 2 3", f3 = "X Y Z")
        w.add_document(f1 = "D E F", f2 = "4 5 6", f3 = "Q R S")
        w.add_document(f1 = "A E C", f2 = "1 4 6", f3 = "X Q S")
        w.add_document(f1 = "A A A", f2 = "2 3 5", f3 = "Y R Z")
        w.add_document(f1 = "A B", f2 = "1 2", f3 = "X Y")
        w.commit()
        
        return ix
    
    def _multi_segment_index(self):
        ix = self._create_index()
        w = writing.IndexWriter(ix)
        w.add_document(f1 = "A B C", f2 = "1 2 3", f3 = "X Y Z")
        w.add_document(f1 = "D E F", f2 = "4 5 6", f3 = "Q R S")
        w.commit()
        
        w = writing.IndexWriter(ix)
        w.add_document(f1 = "A E C", f2 = "1 4 6", f3 = "X Q S")
        w.add_document(f1 = "A A A", f2 = "2 3 5", f3 = "Y R Z")
        w.commit(writing.NO_MERGE)
        
        w = writing.IndexWriter(ix)
        w.add_document(f1 = "A B", f2 = "1 2", f3 = "X Y")
        w.commit(writing.NO_MERGE)
        
        return ix
    
    def test_readers(self):
        target = [(0, 'A', 4, 6), (0, 'B', 2, 2), (0, 'C', 2, 2),
                  (0, 'D', 1, 1), (0, 'E', 2, 2), (0, 'F', 1, 1),
                  (1, '1', 3, 3), (1, '2', 3, 3), (1, '3', 2, 2),
                  (1, '4', 2, 2), (1, '5', 2, 2), (1, '6', 2, 2),
                  (2, 'Q', 2, 2), (2, 'R', 2, 2), (2, 'S', 2, 2),
                  (2, 'X', 3, 3), (2, 'Y', 3, 3), (2, 'Z', 2, 2)]
        
        stored = [{"f1": "A B C"}, {"f1": "D E F"}, {"f1": "A E C"},
                  {"f1": "A A A"}, {"f1": "A B"}]
        
        def t(ix):
            tr = ix.term_reader()
            self.assertEqual(list(tr), target)
            
            dr = ix.doc_reader()
            self.assertEqual(list(dr), stored)
        
        ix = self._one_segment_index()
        self.assertEqual(len(ix.segments), 1)
        t(ix)
        
        ix = self._multi_segment_index()
        self.assertEqual(len(ix.segments), 3)
        t(ix)
        
    def test_vector_postings(self):
        s = fields.Schema(id=fields.ID(stored=True, unique=True),
                          content=fields.TEXT(vector=fields.Positions(analyzer=analysis.StandardAnalyzer())))
        st = store.RamStorage()
        ix = index.Index(st, s, create = True)
        
        writer = ix.writer()
        writer.add_document(id='1', content='the quick brown fox jumped over the lazy dogs')
        writer.commit()
        dr = ix.doc_reader()
        
        terms = list(dr.vector_as(0, 0, "weight"))
        self.assertEqual(terms, [('brown', 1.0),
                                 ('dogs', 1.0),
                                 ('fox', 1.0),
                                 ('jumped', 1.0),
                                 ('lazy', 1.0),
                                 ('over', 1.0),
                                 ('quick', 1.0),
                                 ])

        
if __name__ == '__main__':
    unittest.main()

