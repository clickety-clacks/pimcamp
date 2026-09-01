# Repository guidance

## Scope

Build only the seven public `pimcamp.v1` operations named in `README.md`.
The reviewed specification that `README.md` identifies is the product authority.

## Verification

Run the standard gate from the repository root:

```sh
python3 -m compileall -q src tests pimcamp
python3 -m unittest discover -s tests -v
```

Run a focused boundary test with:

```sh
python3 -m unittest tests.test_process_boundary -v
```

Documentation-only commits require `git diff --check`.

The ordinary suite must not need network access, a mail account, Himalaya, or
Mirador. Tests may use deterministic implementations of Pimcamp's private
adapter ports. They must not present invented Himalaya output or Mirador events
as lower-adapter fixtures.

Run the credentialed adapter journey with:

```sh
python3 -m unittest tests.test_live_adapters -v
```

The live test must skip with a specific reason when its documented environment,
real tools, test account, or test recipient is absent. A local skip is honest,
but it is not release evidence. Release evidence requires that live test to pass
with redacted captures from the real lower adapters.

Before each commit, inspect `git status --short` and the complete staged diff.
Run the gate that applies to the changed files. Before review, run the complete
standard gate and the live journey, then report every pass and skip separately.
