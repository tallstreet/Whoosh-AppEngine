# Implement optimistic online compaction for Whoosh

The supplied legacy Whoosh engine supports appending, deleting, importing,
ordinary optimized merges, and retained reader snapshots. Its normal writer
holds an exclusive index lock while merging. Add a **new** compaction facility
that constructs replacement segments while normal writes can commit, and
publishes the replacement without losing later writes or resurrecting deletes.

This is a coding task. Modify the repository implementation; preserve existing
APIs and persistent index format. All dependencies are installed and tests run
offline on Python 3.12 or newer.

The starting code already includes schema-aware import, retained reader
snapshots, and the bounded stable posting sorter. Preserve these working
features, including the existing `postfanin` and `poststorage` writer options.

## API

Provide `Index.compaction(segment_names)` returning a job with:

- `prepare()`: construct a complete replacement for the named source segments,
  but do not make it visible to new readers;
- `commit()`: publish the prepared replacement atomically as one committed index
  generation, or raise `CompactionConflict` if it cannot be reconciled;
- `cancel()`: discard unpublished work safely and idempotently.

Export `CompactionConflict` and `CompactionStateError` from the module
`whoosh.compaction`. These exception types can subclass `Exception`.
The `segment_names` argument is a finite sequence of immutable segment names
obtained from the `.name` attributes of `Index.segments`.
Empty, repeated, or unknown source names raise `ValueError` at job creation.
Successful publication advances the latest committed generation by exactly one.

## Visibility and reconciliation

1. The job captures the latest **committed** view at creation, even if the
   creating Index handle is stale or has uncommitted direct deletions. Later
   changes before `prepare()` do not change this captured view. Source segments
   are selected by identity, not by their current global document offsets.
   `prepare()` copies only documents that were live in that view, together with
   their original indexed postings, positions, vectors, stored fields, and
   metadata, without reconstructing postings by analyzing stored text.
2. An ordinary writer using another Index handle must be able to finish its
   commit when preparation is paused while reading or copying source data.
   Compaction must not hold the exclusive writer lock throughout preparation.
   Temporary short critical sections for file/name allocation are permitted.
3. At publication, preserve the complete latest committed index state outside
   the selected source segments, including appended segments, recent deletions,
   and successful disjoint compactions. Additional deletions affecting selected
   source rows since job creation must also remain effective in the merged
   replacement. Compaction alone does not change the logical live document set.
4. If a document deleted in the captured creation-time view is live in a
   selected source segment at publication, `commit()` must raise `CompactionConflict`
   rather than lose that document. The same exception is required if any
   selected source segment has been replaced/removed, including by an
   overlapping compaction. A conflict leaves the index unchanged and permits
   `cancel()` of the failed job. An append or additional deletion by itself
   must NOT cause a conflict.
5. Disjoint jobs prepared from the same generation can both commit in either
   order. Overlapping jobs cannot both publish. The surviving job must not
   lose the other job's unrelated changes.
6. Before publication, a newly opened Index observes the old committed view.
   After successful publication, a new Index observes the new generation.
   A new Index must remain openable while publication is paused during
   creation or writing of its metadata: it must see a complete old or new
   generation, never a partially written manifest. This is concurrent-reader
   visibility, not a power-loss durability requirement.
   The original Index handle used to create the job also reflects the published
   generation once `commit()` returns. Old independently acquired readers must
   retain their pre-commit view, including first-time lazy vector reads after
   compaction. Once the last owner releases old files, collection must be able
   to reclaim them; retaining obsolete files indefinitely is not acceptable.
7. Failed or cancelled jobs may remove only their own unpublished artifacts.
   Do not close another owner's handles, release another owner's writer lock,
   delete another job's staging files, or delete the files of a different named
   index sharing the same Storage. The other named index must stay reopenable
   even when no Index handle for it is currently open.
8. Preserve ordinary writing, schema-aware import, deletion/undeletion,
   search, optimize, retained readers, and cancellation behavior. Segment
   files continue to use the existing storage interfaces and on-disk format.

## Lifecycle

- A job starts in a new state. It can prepare exactly once; a repeated
  `prepare()` raises `CompactionStateError`.
- `commit()` before successful `prepare()`, after `cancel()`, or after an
  earlier successful `commit()` raises `CompactionStateError`.
- A failed `commit()` due to a conflict leaves the job cancellable.
- Calling `cancel()` repeatedly is harmless. `cancel()` after a successful
  commit is harmless.
- Interruption of preparation by an ordinary Python exception must not publish
  partially constructed files or leave the ordinary writer lock held.
  `cancel()` must be safe after such an exception and reclaim the job's
  unpublished files and ownership; the exception traceback may remain alive.

Compaction must read and copy from the captured source view during `prepare()`;
job creation may capture metadata and open readers, but must not perform the
full merge. Cancellation of one job must remain safe while an unrelated writer
holds the write lock. A returned job owns its captured sources until it commits
or is cancelled, so later publication and collection cannot invalidate its
source reads.

## Test model and exclusions

The verifier uses FileStorage and RamStorage, multiple Index instances in one
process, deterministic histories of append/delete/undelete/prepare/publish,
reader ownership and lazy vector access, and controlled pause points in source
segment reads. No power-loss durability, cross-process lease protocol, or
multi-process writer synchronization is required. The verifier does not require
one particular compaction algorithm or private job fields.

Grading uses the final submitted source tree. Tests are run offline. Do not
modify tests, relax correctness conditions, or replace the index engine.
