#!/usr/bin/env python3
"""Visible reproduction: a selected generation retires before metadata open."""
from collections import Counter
from pathlib import Path
import queue
import sys
import tempfile
import threading
import traceback
import unittest

ROOT = Path(__file__).resolve().parents[1]
if (ROOT / 'src').is_dir():
    sys.path.insert(0, str(ROOT / 'src'))
from whoosh import analysis, fields, index, store, writing


GUARD = 12


class PausedOpenStorage(store.FileStorage):
    def __init__(self, folder):
        super().__init__(folder)
        self.target = None
        self.events = queue.Queue()
        self.resume = threading.Event()
        self.paused = False

    def open_file(self, name, *args, **kwargs):
        if (threading.current_thread() is self.target and not self.paused
                and str(name).endswith('.toc')):
            self.paused = True
            self.events.put('paused')
            if not self.resume.wait(GUARD * 4):
                raise AssertionError('test controller did not release metadata pause')
        return super().open_file(name, *args, **kwargs)


class PublicReaderRetirement(unittest.TestCase):
    def seed(self, storage):
        schema = fields.Schema(key=fields.ID(stored=True),
                               text=fields.TEXT(stored=True, analyzer=analysis.KeywordAnalyzer()))
        owner = index.Index(storage, schema, create=True)
        for key, text in [('a', 'shared red'), ('b', 'shared blue')]:
            writer = owner.writer()
            writer.add_document(key=key, text=text)
            writer.commit(writing.NO_MERGE)
        return owner

    def assert_content(self, opened, expected):
        searcher = opened.searcher()
        try:
            rows = list(searcher.documents(text='shared'))
            self.assertEqual(Counter(row['key'] for row in rows), Counter(list(expected)))
            self.assertEqual({row['key']: row['text'] for row in rows}, expected)
        finally:
            searcher.close()

    def update(self, owner):
        writer = owner.writer()
        writer.delete_document(1)
        writer.add_document(key='c', text='shared green')
        writer.commit(writing.OPTIMIZE)
        owner.clean_files()

    def test_existing_reader_retains_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = store.FileStorage(folder)
            owner = self.seed(storage)
            older = index.Index(storage)
            try:
                self.update(owner)
                self.assert_content(older, {'a': 'shared red', 'b': 'shared blue'})
                fresh = index.Index(storage)
                try:
                    self.assertEqual(fresh.generation, owner.generation)
                    self.assert_content(fresh, {'a': 'shared red', 'c': 'shared green'})
                finally:
                    fresh.close()
            finally:
                older.close()
                owner.close()

    def test_open_survives_selected_generation_retirement(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = PausedOpenStorage(folder)
            owner = self.seed(storage)
            before = owner.generation
            result = {}
            reader_done = threading.Event()
            writer_done = threading.Event()

            def open_reader():
                try:
                    result['opened'] = index.Index(storage)
                except BaseException as exc:
                    result['reader_error'] = ''.join(traceback.format_exception(exc))
                finally:
                    reader_done.set()
                    storage.events.put('done')

            def publish_and_collect():
                try:
                    self.update(owner)
                except BaseException as exc:
                    result['writer_error'] = ''.join(traceback.format_exception(exc))
                finally:
                    writer_done.set()

            opening = threading.Thread(target=open_reader, name='public-reader', daemon=True)
            publishing = threading.Thread(target=publish_and_collect, name='public-writer', daemon=True)
            storage.target = opening
            opening.start()
            try:
                # Cached/otherwise I/O-free opens may legally complete first.
                self.assertIn(storage.events.get(timeout=GUARD), ('paused', 'done'))
                publishing.start()
                self.assertTrue(writer_done.wait(GUARD),
                                'writer/collection blocked behind paused reader')
                self.assertNotIn('writer_error', result, result.get('writer_error'))
                storage.resume.set()
                self.assertTrue(reader_done.wait(GUARD), 'reader did not complete')
                self.assertNotIn('reader_error', result, result.get('reader_error'))
                opened = result['opened']
                expected = {before: {'a': 'shared red', 'b': 'shared blue'},
                            owner.generation: {'a': 'shared red', 'c': 'shared green'}}
                self.assertIn(opened.generation, expected)
                self.assert_content(opened, expected[opened.generation])
                fresh = index.Index(storage)
                try:
                    self.assertEqual(fresh.generation, owner.generation)
                    self.assert_content(fresh, expected[owner.generation])
                finally:
                    fresh.close()
            finally:
                storage.resume.set()
                opening.join(GUARD)
                if publishing.ident is not None:
                    publishing.join(GUARD)
                if not opening.is_alive() and not publishing.is_alive():
                    if 'opened' in result:
                        result['opened'].close()
                    owner.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
