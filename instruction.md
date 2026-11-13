# Safe generation reclamation in Whoosh

Repair the race between opening an index snapshot and reclaiming obsolete
generations. Keep the existing public API:

```python
opened = index.Index(storage)
reader = opened.searcher()
```

`Index.refresh()` must retain its existing behavior and use the same safe
opening semantics when it creates a new handle. No new application-facing
reader, transaction, or compaction API is required.

The supplied engine already publishes complete generation metadata through a
temporary file and rename, maintains leases for existing readers and writers,
and has working synchronous commits, merges, import, and posting sorting.
Preserve those features. The defect to repair is the handoff between selecting
a committed generation and protecting the files needed to use it.

## Required behavior

1. An open overlapping a commit or `Index.clean_files()` must return a coherent
   committed snapshot. Its metadata, stored documents, postings, deletion
   state, and vectors must agree. It may use a generation current during that
   open; it must not expose a mixture or fail because ordinary concurrent
   collection removed a generation it had started to acquire. Safely retaining
   that generation or retrying a newer committed generation are both allowed.
   An open begun after a completed commit must see that commit or a later one.
2. Once an Index or reader owns a snapshot, concurrent commits and collection
   must not invalidate it. Preserve the supplied independent reader lifetimes:
   closing one handle does not release another handle's ownership, and a
   searcher or direct reader can outlive its parent Index.
3. A reader paused inside a Storage operation while discovering or reading
   generation metadata must not prevent an independent writer from committing
   or a collector from completing. A collector paused while inspecting
   generation metadata must also allow an independent writer to commit.
   A collector paused immediately before a Storage deletion takes effect
   must not prevent another reader from opening
   and using a complete committed snapshot. Short coordination is allowed;
   serializing these entire operations behind a shared lock is not sufficient.
4. Collection must remain useful during acquisition. An acquiring reader may
   retain the generation it is acquiring, but it must not prevent collection
   of unrelated obsolete generations with no owners. After all owners and
   acquisition attempts for an obsolete generation have ended, an ordinary
   `clean_files()` call must reclaim its unreferenced metadata and segment files.
   Committed current files, active writer output, and legitimately retained
   snapshots must remain protected. The latest committed generation remains
   authoritative even after every Index, reader, and writer owning it has
   closed. A collector that began before that publication must still preserve
   it, including when only older-generation handles remain open.
5. Preserve normal writes, deletion semantics, stored fields, positional
   postings, vectors, the on-disk format, and the supplied import, snapshot,
   sorter, and ordinary search tests. No manual recovery or application retry
   is required for this race. Ordinary writer cancellation, collection of its
   abandoned output, and subsequent writes through a freshly opened Index must
   continue to work. The supplied allocator can reuse a canceled output's
   segment name after collection; retirement of an earlier file must not make
   a later valid file with that name unusable. Safely avoiding reuse is also
   allowed. After the subsequent commit returns, a fresh reader must see the
   new document alongside the previously committed content.

## Bounded concurrency model

Tests run offline on Python 3.12 or newer with FileStorage and RamStorage, in
one process with multiple threads and handles, and one named index per Storage.
FileStorage handles referring to the same directory share the same index.
Ordinary writers are serialized by the existing writer lock. Readers and
collectors may overlap those writers and one another; the same individual
Index or reader object is not simultaneously mutated by multiple callers.

The index already exists before concurrent work begins. Storage operations
are reliable, except that opening a file legitimately removed by concurrent
collection can report the backend's usual missing-file error. Tests can pause
public Storage operations at controlled boundaries before or after discovery,
metadata access, or before deletion takes effect. Progress is checked while
the chosen operation remains paused; timeouts only guard against deadlock.
No particular implementation fields, allocation counts, filenames beyond the
existing format, or synchronization algorithm are prescribed.

Cross-process coordination, external file modifications, corrupt metadata,
power-loss recovery, storage fault injection, replacing or recreating an index
while operations are in flight, a new online compaction API, and reconciliation
of concurrent document updates are outside this task. No additional memory
complexity bound or prescribed coordination-state representation is required.

Modify the implementation under `src/whoosh/`. Do not change supplied tests or
weaken their assertions. Keep the storage and index engine supplied here.
