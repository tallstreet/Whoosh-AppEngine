import cgi

from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app

from whoosh import store
from whoosh.fields import Schema, STORED, ID, KEYWORD, TEXT
from whoosh.index import getdatastoreindex
from whoosh.qparser import QueryParser, MultifieldParser
import logging

SEARCHSCHEMA = Schema(content=TEXT(stored=True))


class MainPage(webapp.RequestHandler):
  def get(self):
    self.response.out.write('<html><body>')
    self.response.out.write("""
          <form action="/search" method="get">
            <div><input name="query" type="text" value=""><input type="submit" value="Search"></div>
          </form>
        </body>
      </html>""")     

    # Write the submission form and the footer of the page
    self.response.out.write("""
          <form action="/sign" method="post">
            <div><textarea name="content" rows="3" cols="60"></textarea></div>
            <div><input type="submit" value="Sign Guestbook"></div>
          </form>
        </body>
      </html>""")
      
class SearchPage(webapp.RequestHandler):
  def get(self):
    self.response.out.write('<html><body>')
    self.response.out.write("""
          <form action="/search" method="get">
            <div><input name="query" type="text" value=""><input type="submit" value="Search"></div>
          </form>
        </body>
      </html>""")       
    ix = getdatastoreindex("hello", schema=SEARCHSCHEMA)
    parser = QueryParser("content", schema = ix.schema)
    q = parser.parse(self.request.get('query'))
    results = ix.searcher().search(q)

    for result in results:
      self.response.out.write('<blockquote>%s</blockquote>' %
                              cgi.escape(result['content']))

    # Write the submission form and the footer of the page
    self.response.out.write("""
          <form action="/sign" method="post">
            <div><textarea name="content" rows="3" cols="60"></textarea></div>
            <div><input type="submit" value="Sign Guestbook"></div>
          </form>
        </body>
      </html>""")      

class Guestbook(webapp.RequestHandler):
  def post(self):
    ix = getdatastoreindex("hello", schema=SEARCHSCHEMA)
    writer = ix.writer()
    writer.add_document(content=u"%s" %  self.request.get('content'))
    writer.commit()
    self.redirect('/')

application = webapp.WSGIApplication(
                                     [('/', MainPage),
                                      ('/search', SearchPage),
                                      ('/sign', Guestbook)],
                                     debug=True)

def main():
  run_wsgi_app(application)

if __name__ == "__main__":
  main()
