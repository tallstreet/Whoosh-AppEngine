# Safe generation reclamation in Whoosh

Read `instruction.md` for the task and `AGENTS.md` for repository instructions.
The engine and all test dependencies are supplied; no downloads are needed.
The starting code includes atomic generation metadata publication. The target
defect is concurrent reader acquisition versus obsolete-file reclamation,
including protection of the latest commit after its last owner closes and
normal writing after canceled output is collected.

Run the visible reproduction with:

```sh
PYTHONPATH=src python tests/test_reader_retirement_public.py
```

Run ordinary tests with:

```sh
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py'
```

Additional supplied compatibility checks are `tests/import_contract.py`,
`tests/snapshot_contract.py`, and `tests/test_public.py`. Preserve those APIs
and behaviors while repairing the reader acquisition race.
