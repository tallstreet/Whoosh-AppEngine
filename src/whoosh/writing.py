#===============================================================================
# Copyright 2007 Matt Chaput
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#    http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#===============================================================================

"""
This module contains classes for writing to an index.
"""

from array import array
from collections import defaultdict
from tempfile import TemporaryFile

from whoosh import fields, index, postpool, reading, structfile, tables, lifetime
from whoosh.fields import UnknownFieldError
from whoosh.util import fib

# Exceptions

class IndexingError(Exception):
    pass


def _import_field_map(source_schema, destination_schema):
    """Validate stored representations and map source field numbers by name."""
    posting_types = (fields.Stored, fields.Existence, fields.Frequency,
                     fields.Positions)
    vector_types = (fields.Frequency, fields.Positions)
    result = {}
    for source_number in range(len(source_schema)):
        name = source_schema.number_to_name(source_number)
        if name not in destination_schema:
            raise IndexingError("Destination has no field %r" % name)
        source = source_schema.field_by_number(source_number)
        destination_number = destination_schema.name_to_number(name)
        destination = destination_schema.field_by_number(destination_number)

        if type(source.format) not in posting_types or type(source.format) is not type(destination.format):
            raise IndexingError("Incompatible posting format for %r" % name)
        if getattr(source.format, "field_boost", None) != getattr(destination.format, "field_boost", None):
            raise IndexingError("Incompatible posting boost for %r" % name)
        if any(bool(getattr(source, flag)) != bool(getattr(destination, flag))
               for flag in ("stored", "scorable", "unique")):
            raise IndexingError("Incompatible field flags for %r" % name)

        source_vector = source.vector
        destination_vector = destination.vector
        if source_vector is None or destination_vector is None:
            if source_vector is not destination_vector:
                raise IndexingError("Incompatible vector presence for %r" % name)
        else:
            if type(source_vector) not in vector_types or type(source_vector) is not type(destination_vector):
                raise IndexingError("Incompatible vector format for %r" % name)
            if source_vector.field_boost != destination_vector.field_boost:
                raise IndexingError("Incompatible vector boost for %r" % name)

        # Analysis settings affect future text ingestion, not these materialized
        # postings. Do not compare or invoke the source/destination analyzers.
        result[source_number] = destination_number
    return result


DOCLENGTH_TYPE = "H"
DOCLENGTH_LIMIT = 2**16-1


# Merge policies

# A merge policy is a callable that takes the Index object,
# the SegmentWriter object, and the current SegmentSet
# (not including the segment being written), and returns an
# updated SegmentSet (not including the segment being
# written).

def NO_MERGE(ix, writer, segments):
    """This policy does not merge any existing segments.
    """
    return segments


def MERGE_SMALL(ix, writer, segments):
    """This policy merges small segments, where small is
    defined using a heuristic based on the fibonacci sequence.
    """
    
    newsegments = index.SegmentSet()
    sorted_segment_list = sorted(((s.doc_count_all(), s) for s in segments), key=lambda pair: pair[0])
    total_docs = 0
    for i, (count, seg) in enumerate(sorted_segment_list):
        if count > 0:
            total_docs += count
            if total_docs < fib(i + 5):
                writer.add_segment(ix, seg)
            else:
                newsegments.append(seg)
    return newsegments


def OPTIMIZE(ix, writer, segments):
    """This policy merges all existing segments.
    """
    for seg in segments:
        writer.add_segment(ix, seg)
    return index.SegmentSet()


# Writing classes

class IndexWriter(index.DeletionMixin):
    """High-level object for writing to an index. This object takes care of
    instantiating a SegmentWriter to create a new segment as you add documents,
    as well as merging existing segments (if necessary) when you finish.
    
    You can use this object as a context manager. If an exception is thrown
    from within the context it calls cancel(), otherwise it calls commit()
    when the context ends.
    """
    
    # This class is mostly a shell for SegmentWriter. It exists to handle
    # multiple SegmentWriters during merging/optimizing.
    
    def __init__(self, ix, postlimit = 4 * 1024 * 1024,
                 term_blocksize = 1 * 1024, doc_blocksize = 8 * 1024,
                 vector_blocksize = 8 * 1024, postfanin = 8,
                 poststorage = None):
        """
        :ix: the Index object you want to write to.
        :blocksize: the block size for tables created by this writer.
        """
        
        if isinstance(postlimit, bool) or not isinstance(postlimit, int) or postlimit <= 0:
            raise ValueError("postlimit must be a positive integer")
        if isinstance(postfanin, bool) or not isinstance(postfanin, int) or postfanin < 2:
            raise ValueError("postfanin must be an integer of at least two")
        # Obtain a lock
        self.locked = ix.lock()
        
        self.index = ix
        self.segments = ix.segments.copy()
        self.postlimit = postlimit
        self.postfanin = postfanin
        self.poststorage = poststorage
        self.term_blocksize = term_blocksize
        self.doc_blocksize = doc_blocksize
        self.vector_blocksize = vector_blocksize
        self._segment_writer = None
        self._searcher = None
        self._finished = False
        self._lease = lifetime.Lease(ix.storage, (s.name for s in self.segments))
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.cancel()
        else:
            self.commit()
    
    def _finish(self):
        if self._finished:
            return
        self._finished = True
        try:
            self._close_searcher()
            if self._segment_writer is not None:
                try:
                    self._segment_writer.cancel()
                finally:
                    self._segment_writer._lease.close()
                    self._segment_writer = None
        finally:
            self._lease.close()
            if self.locked:
                self.locked = False
                self.index.unlock()
    
    def segment_writer(self):
        """Returns the underlying SegmentWriter object."""
        
        if not self._segment_writer:
            self._segment_writer = SegmentWriter(self.index, self.postlimit,
                                                 self.term_blocksize,
                                                 self.doc_blocksize, self.vector_blocksize,
                                                 postfanin=self.postfanin,
                                                 poststorage=self.poststorage)
        return self._segment_writer
    
    def searcher(self):
        """Returns a searcher for the existing index."""
        if not self._searcher:
            self._searcher = self.index.searcher()
        return self._searcher
    
    def _close_searcher(self):
        if self._searcher:
            self._searcher.close()
            self._searcher = None
    
    def start_document(self):
        """Starts recording information for a new document. This should be followed by
        add_field() calls, and must be followed by an end_document() call.
        Alternatively you can use add_document() to add all fields at once.
        """
        self.segment_writer().start_document()
        
    def add_field(self, fieldname, text, stored_value = None):
        """Adds a the value of a field to the document opened with start_document().
        
        :fieldname: The name of the field in which to index/store the text.
        :text: The unicode text to index.
        """
        self.segment_writer().add_field(fieldname, text, stored_value = stored_value)
        
    def end_document(self):
        """
        Closes a document opened with start_document().
        """
        self.segment_writer().end_document()
    
    def add_document(self, **fields):
        """Adds all the fields of a document at once. This is an alternative to calling
        start_document(), add_field() [...], end_document().
        
        The keyword arguments map field names to the values to index/store.
        
        For fields that are both indexed and stored, you can specify an alternate
        value to store using a keyword argument in the form "_stored_<fieldname>".
        For example, if you have a field named "title" and you want to index the
        text "a b c" but store the text "e f g", use keyword arguments like this::
        
            add_document(title=u"a b c", _stored_title=u"e f g")
        """
        self.segment_writer().add_document(fields)
    
    def add_index(self, source_index):
        """Append the live indexed content of a compatible source index."""
        # Finish validation before creating files or touching pending documents.
        field_map = _import_field_map(source_index.schema, self.index.schema)
        if source_index.doc_count() == 0:
            return
        writer = self.segment_writer()
        if writer._doc_state.active:
            raise IndexingError("Cannot import while a document is open")
        for segment in source_index.segments:
            writer.add_segment(source_index, segment, field_map=field_map)

    def update_document(self, **fields):
        """Adds or replaces a document. At least one of the fields for which you
        supply values must be marked as 'unique' in the index's schema.
        
        The keyword arguments map field names to the values to index/store.
        
        For fields that are both indexed and stored, you can specify an alternate
        value to store using a keyword argument in the form "_stored_<fieldname>".
        For example, if you have a field named "title" and you want to index the
        text "a b c" but store the text "e f g", use keyword arguments like this::
        
            update_document(title=u"a b c", _stored_title=u"e f g")
        """
        
        # Check which of the supplied fields are unique
        unique_fields = [name for name, field
                         in self.index.schema.fields()
                         if name in fields and field.unique]
        if not unique_fields:
            raise IndexingError("None of the fields in %r are unique" % list(fields.keys()))
        
        # Delete documents in which the supplied unique fields match
        s = self.searcher()
        for name in unique_fields:
            self.delete_by_term(name, fields[name])
        
        # Add the given fields
        self.add_document(**fields)
    
    def commit(self, mergetype=MERGE_SMALL):
        if self._finished:
            raise IndexingError("Writer is already finished")
        try:
            if not self.index.up_to_date():
                raise index.OutOfDateError
            self._close_searcher()
            if self._segment_writer or mergetype is OPTIMIZE:
                self._merge_segments(mergetype)
            self.index.commit(self.segments)
        finally:
            self._finish()
        
    def cancel(self):
        """Cancels any documents/deletions added by this object
        and unlocks the index.
        """
        self._finish()
    
    def _merge_segments(self, mergetype):
        sw = self.segment_writer()
        new_segments = mergetype(self.index, sw, self.segments)
        sw.close()
        new_segments.append(sw.segment())
        self.segments = new_segments


class SegmentWriter(object):
    """
    Do not instantiate this object directly; it is created by the IndexWriter object.
    
    Handles the actual writing of new documents to the index: writes stored fields,
    handles the posting pool, and writes out the term index.
    """
    
    class DocumentState(object):
        def __init__(self, scorable_fields):
            self._scorable_fields = scorable_fields
            self._fieldnum_to_pos = dict((fnum, i) for i, fnum in enumerate(scorable_fields))
            self.reset()
        
        def add_to_length(self, fieldnum, n):
            pos = self._fieldnum_to_pos[fieldnum]
            current = self.field_lengths[pos]
            if current >= DOCLENGTH_LIMIT: return
            self.field_lengths[pos] = min(current + n, DOCLENGTH_LIMIT)
        
        def reset(self):
            #: Whether a document is currently in progress
            self.active = False
            #: Maps field names to stored field contents for this document
            self.stored_fields = {}
            #: Keeps track of the last field that was added
            self.prev_fieldnum = None
            #: Keeps track of field lengths in this document
            self.field_lengths = array(DOCLENGTH_TYPE, [0] * len(self._scorable_fields))
    
    def __init__(self, ix, postlimit,
                 term_blocksize, doc_blocksize, vector_blocksize,
                 name = None, postfanin = 8, poststorage = None):
        """
        :ix: the Index object in which to write the new segment.
        :postlimit: the maximum size for a run in the posting pool.
        :name: the name of the segment.
        :blocksize: the block size to use for tables created by this writer.
        """
        
        self.index = ix
        self.schema = ix.schema
        self.storage = ix.storage
        self.name = name or ix._next_segment_name()
        self._lease = lifetime.Lease(self.storage, (self.name,))
        self.is_closed = False
        
        self.max_doc = 0
        self.max_weight = 0
        
        self.pool = postpool.PostingPool(limit=postlimit, fan_in=postfanin,
                                         run_storage=poststorage)
        self._scorable_fields = self.schema.scorable_fields()
        
        # Create a temporary segment object just so we can access
        # its *_filename attributes (so if we want to change the
        # naming convention, we only have to do it in one place).
        tempseg = index.Segment(self.name, 0, 0, None)
        
        # Open files for writing
        self.term_table = self.storage.create_table(tempseg.term_filename, postings = True,
                                                    blocksize = term_blocksize)
        
        self.doclength_table = self.storage.create_records(tempseg.doclen_filename,
                                                           DOCLENGTH_TYPE,
                                                           len(self._scorable_fields))
        
        self.docs_table = self.storage.create_table(tempseg.docs_filename,
                                                    blocksize = doc_blocksize, compressed = 9)
        
        self.vector_table = None
        if self.schema.has_vectored_fields():
            self.vector_table = self.storage.create_table(tempseg.vector_filename,
                                                          postings = True,
                                                          stringids = True,
                                                          blocksize = vector_blocksize)
        
        # Keep track of the total number of tokens (across all docs)
        # in each field
        self.field_length_totals = defaultdict(int)
        # Records the state of the writer's current document
        self._doc_state = SegmentWriter.DocumentState(self._scorable_fields)
            
    def segment(self):
        """Returns an index.Segment object for the segment being written."""
        return index.Segment(self.name, self.max_doc, self.max_weight,
                             dict(self.field_length_totals))
    
    def close(self):
        if self.is_closed:
            return
        if self._doc_state.active:
            raise IndexingError("Called SegmentWriter.close() with a document still opened")
        self._flush_pool()
        self.cancel()

    def cancel(self):
        # Settle every write callback before releasing the staging lease.
        # In RAM storage, close publishes the private file's bytes.
        if self.is_closed:
            return
        self.is_closed = True
        try:
            for table in (self.doclength_table, self.docs_table, self.term_table, self.vector_table):
                if table is not None:
                    table.close()
        finally:
            self.pool.close()
        
    def add_index(self, other_ix):
        """Append live source documents after validating its entire schema."""
        field_map = _import_field_map(other_ix.schema, self.schema)
        if self._doc_state.active:
            raise IndexingError("Cannot import while a document is open")
        for segment in other_ix.segments:
            self.add_segment(other_ix, segment, field_map=field_map)

    def add_segment(self, ix, segment, field_map=None):
        """Copy live indexed data, translating all three number spaces."""
        if self._doc_state.active:
            raise IndexingError("Cannot merge while a document is open")
        if segment.doc_count() == 0:
            return

        source_schema = ix.schema
        if field_map is None:
            # Ordinary compaction uses the same schema; name lookup also keeps
            # this low-level merge robust to differences in field numbering.
            field_map = {
                number: self.schema.name_to_number(source_schema.number_to_name(number))
                for number in range(len(source_schema))
            }

        destination_slots = {number: slot for slot, number
                             in enumerate(self._scorable_fields)}
        length_slots = [(source_slot, destination_slots[field_map[source_number]])
                        for source_slot, source_number
                        in enumerate(source_schema.scorable_fields())]
        # Vector rows bypass PostingPool, so order them by the OUTPUT field ID.
        vector_numbers = sorted(source_schema.vectored_fields(),
                                key=lambda number: field_map[number])
        document_map = {}

        document_reader = reading.DocReader(ix.storage, segment, source_schema)
        try:
            source_vectors = None
            if vector_numbers:
                document_reader._open_vectors()
                source_vectors = document_reader.vector_table

            state = SegmentWriter.DocumentState(self._scorable_fields)
            for source_document in range(segment.max_doc):
                if segment.is_deleted(source_document):
                    continue
                destination_document = self.max_doc
                document_map[source_document] = destination_document
                state.reset()
                state.stored_fields = dict(document_reader[source_document])
                source_lengths = document_reader.doc_field_lengths(source_document)
                for source_slot, destination_slot in length_slots:
                    state.field_lengths[destination_slot] = source_lengths[source_slot]

                for source_number in vector_numbers:
                    source_key = (source_document, source_number)
                    if not source_vectors.blockcount or source_key not in source_vectors:
                        continue
                    destination_number = field_map[source_number]
                    source_format = source_schema.field_by_number(source_number).vector
                    destination_format = self.schema.field_by_number(destination_number).vector
                    for term, data in source_vectors.postings(source_key, source_format.read_postvalue):
                        self.vector_table.write_posting(term, data, destination_format.write_postvalue)
                    self.vector_table.add_row((destination_document, destination_number), None)

                self._write_doc_entry(state)
                self.max_doc += 1
        finally:
            document_reader.close()

        term_reader = reading.TermReader(ix.storage, segment, source_schema)
        try:
            for source_number, term, _, _ in term_reader:
                destination_number = field_map[source_number]
                source_format = source_schema.field_by_number(source_number).format
                frequency = source_format.interpreter("frequency")
                for source_document, data in term_reader.postings(source_number, term):
                    self.pool.add_posting(destination_number, term,
                                          document_map[source_document], data)
                    # Aggregate counts are exact and live-only. Stored document
                    # lengths may be saturated or absent, and source totals may
                    # include tombstones; neither can supply this contribution.
                    self.field_length_totals[destination_number] += frequency(data)
        finally:
            term_reader.close()

    def start_document(self):
        ds = self._doc_state
        if ds.active:
            raise IndexingError("Called start_document() when a document was already opened")
        ds.active = True
        
    def end_document(self):
        ds = self._doc_state
        if not ds.active:
            raise IndexingError("Called end_document() when a document was not opened")
        
        self._write_doc_entry(ds)
        ds.reset()
        self.max_doc += 1

    def add_document(self, fields):
        self.start_document()
        fieldnames = [name for name in list(fields.keys()) if not name.startswith("_")]
        
        schema = self.schema
        for name in fieldnames:
            if name not in schema:
                raise UnknownFieldError("There is no field named %r" % name)
        
        fieldnames.sort(key = schema.name_to_number)
        for name in fieldnames:
            value = fields.get(name)
            if value:
                self.add_field(name, value, stored_value = fields.get("_stored_%s" % name))
        self.end_document()
    
    def add_field(self, fieldname, value, stored_value = None,
                  start_pos = 0, start_char = 0, **kwargs):
        if value is None:
            return
        
        # Get the field information
        schema = self.schema
        if fieldname not in schema:
            raise UnknownFieldError("There is no field named %r" % fieldname)
        fieldnum = schema.name_to_number(fieldname)
        field = schema.field_by_name(fieldname)
        format = field.format
        
        # Check that the user added the fields in schema order
        docstate = self._doc_state
        if docstate.prev_fieldnum is not None and fieldnum < docstate.prev_fieldnum:
            raise IndexingError("Added field %r out of order (add fields in schema order)" % fieldname)
        docstate.prev_fieldnum = fieldnum

        # If the field is indexed, add the words in the value to the index
        if format.analyzer:
            if not isinstance(value, str):
                raise ValueError("%r in field %s is not unicode" % (value, fieldname))
            
            # Count of all terms in the value
            count = 0
            # Count of UNIQUE terms in the value
            unique = 0
            for w, freq, data in format.word_datas(value,
                                                   start_pos = start_pos, start_char = start_char,
                                                   **kwargs):
                assert w != ""
                self.pool.add_posting(fieldnum, w, self.max_doc, data)
                count += freq
                unique += 1
            
            # Add the term count to the total for this field
            self.field_length_totals[fieldnum] += count
            # Add the term count to the per-document field length
            if field.scorable:
                docstate.add_to_length(fieldnum, count)
        
        # If the field is vectored, add the words in the value to
        # the vector table
        vector = field.vector
        if vector:
            vtable = self.vector_table
            vdata = dict((w, data) for w, freq, data
                          in vector.word_datas(value,
                                               start_pos = start_pos, start_char = start_char,
                                               **kwargs))
            write_postvalue = vector.write_postvalue
            for word in sorted(vdata.keys()):
                vtable.write_posting(word, vdata[word], writefn = write_postvalue)
            vtable.add_row((self.max_doc, fieldnum), None)
        
        # If the field is stored, add the value to the doc state
        if field.stored:
            if stored_value is None: stored_value = value
            docstate.stored_fields[fieldname] = stored_value
        
    def _write_doc_entry(self, ds):
        docnum = self.max_doc
        self.doclength_table.append(ds.field_lengths)
        self.docs_table.add_row(docnum, ds.stored_fields)

    def _flush_pool(self):
        # This method pulls postings out of the posting pool (built up
        # as documents are added) and writes them to the posting file.
        # Each time it encounters a posting for a new term, it writes
        # the previous term to the term index (by waiting to write the
        # term entry, we can easily count the document frequency and
        # sum the terms by looking at the postings).
        
        term_table = self.term_table
        
        write_posting_method = None
        current_fieldnum = None # Field number of the current term
        current_text = None # Text of the current term
        first = True
        current_weight = 0
        
        # Loop through the postings in the pool.
        # Postings always come out of the pool in field number/alphabetic order.
        for fieldnum, text, docnum, data in self.pool:
            # If we're starting a new term, reset everything
            if write_posting_method is None or fieldnum > current_fieldnum or text > current_text:
                if fieldnum != current_fieldnum:
                    write_posting_method = self.schema.field_by_number(fieldnum).format.write_postvalue
                
                # If we've already written at least one posting, write the
                # previous term to the index.
                if not first:
                    term_table.add_row((current_fieldnum, current_text), current_weight)
                    
                    if current_weight > self.max_weight:
                        self.max_weight = current_weight
                
                # Reset term variables
                current_fieldnum = fieldnum
                current_text = text
                current_weight = 0
                first = False
            
            elif fieldnum < current_fieldnum or (fieldnum == current_fieldnum and text < current_text):
                # This should never happen!
                raise Exception("Postings are out of order: %s:%s .. %s:%s" %
                                (current_fieldnum, current_text, fieldnum, text))
            
            current_weight += term_table.write_posting(docnum, data, write_posting_method)
        
        # Finish up the last term
        if not first:
            term_table.add_row((current_fieldnum, current_text), current_weight)
            if current_weight > self.max_weight:
                self.max_weight = current_weight



if __name__ == '__main__':
    pass

