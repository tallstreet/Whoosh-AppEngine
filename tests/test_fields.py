import unittest

from whoosh import fields, index

class TestSchema(unittest.TestCase):
    def test_schema_eq(self):
        a = fields.Schema()
        b = fields.Schema()
        self.assertEqual(a, b)

        a = fields.Schema(id=fields.ID)
        b = fields.Schema(id=fields.ID)
        self.assertEqual(a[0], b[0])
        self.assertEqual(a, b)

        c = fields.Schema(id=fields.TEXT)
        self.assertNotEqual(a, c)
        
    def test_creation1(self):
        s = fields.Schema()
        s.add("content", fields.TEXT(phrase = True))
        s.add("title", fields.TEXT(stored = True))
        s.add("path", fields.ID(stored = True))
        s.add("tags", fields.KEYWORD(stored = True))
        s.add("quick", fields.NGRAM)
        s.add("note", fields.STORED)
        
        self.assertEqual(s.field_names(), ["content", "title", "path", "tags", "quick", "note"])
        self.assertTrue("content" in s)
        self.assertFalse("buzz" in s)
        self.assertTrue(isinstance(s["tags"], fields.KEYWORD))
        self.assertTrue(isinstance(s[3], fields.KEYWORD))
        self.assertTrue(s[0] is s.field_by_number(0))
        self.assertTrue(s["title"] is s.field_by_name("title"))
        self.assertTrue(s.name_to_number("path") == 2)
        self.assertTrue(s.number_to_name(4) == "quick")
        self.assertEqual(s.scorable_fields(), [0, 1, 4])
        
    def test_creation2(self):
        s = fields.Schema(content = fields.TEXT(phrase = True),
                          title = fields.TEXT(stored = True),
                          path = fields.ID(stored = True),
                          tags = fields.KEYWORD(stored = True),
                          quick = fields.NGRAM)
        

if __name__ == '__main__':
    unittest.main()

