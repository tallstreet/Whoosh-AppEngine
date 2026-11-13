# Whoosh optimistic online compaction task

Read `instruction.md`. Modify the engine in `src/whoosh` and run the public
checks via `PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py'`.
Do not fetch dependencies during testing. This repository starts without the
new compaction API, so the public compaction smoke test initially fails.
