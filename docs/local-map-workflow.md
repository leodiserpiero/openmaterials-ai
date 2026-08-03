# Use and reconcile the OpenMaterials map locally

The map has two different kinds of content address:

- A node or operator uid hashes that element's semantic identity. Adding an
  unrelated element does not change existing element uids.
- The map head hashes the accepted history. Every appended record includes the
  previous head, so any accepted change produces a new head even when every old
  element uid stays the same.

That distinction is the reason local forks need a semantic rebase. Two forks
from one base both create valid local heads, but their `seq`, `prev`, and
`version` fields describe different single-parent histories. Concatenating or
conflict-merging their `map/log.jsonl` lines creates a broken chain.

## Author and verify a local fork

1. Clone or fork the repository and record the starting head:

   ```bash
   python -c 'from pathlib import Path; from omai.store import Store; print(Store(Path("map")).head)'
   ```

2. Add nodes and operators through the Python domain modules. Uids come from
   semantic identity; do not assign or edit them by hand.

3. Inspect the Python-to-store difference without changing the store:

   ```bash
   python -m omai.sync
   ```

   Review additions, metadata edits, deprecations, and any reported re-mint. A
   re-mint is an identity change, not a metadata edit, and requires an explicit
   `supersede` decision.

4. Put the ordered semantic operations in a bundle. Use
   `MapChangeBundle.create(...)` and serialize `bundle.to_dict()` as canonical
   JSON. The schema is `openmaterials.map-change-bundle@1`:

   ```json
   {
     "schema": "openmaterials.map-change-bundle@1",
     "base_version": "<head recorded in step 1>",
     "operations": [{"op": "add_node", "payload": {}}],
     "author": "your-name",
     "reason": "what this contribution adds",
     "source_head": "<optional verified local head>",
     "bundle_id": "<derived by MapChangeBundle>"
   }
   ```

   `operations` contain only `op` and `payload`. Never copy local `seq`, `prev`,
   or `version` fields into a bundle.

5. Copy the base store to a throwaway directory, apply the local semantic
   operations through `Store.propose`, and run `Store.verify()` plus the
   relevant tests. The resulting local head can be included as `source_head`
   provenance; it is not asserted to be the future canonical head.

6. Dry-run the reviewed bundle against the current canonical store:

   ```bash
   python -m omai.reconcile change-bundle.json --store map
   ```

   The command prints a content-addressed reconciliation receipt and never
   mutates the store unless `--apply` is present. A canonical maintainer can
   apply an accepted bundle to a throwaway authority store with an explicit
   date:

   ```bash
   python -m omai.reconcile change-bundle.json --store /tmp/openmaterials-map-review --apply --date 2026-08-03
   ```

7. Submit the bundle, receipt, and test evidence for review. The repository's
   canonical store remains the authority and assigns new sequence, parent, and
   version hashes when the reviewed operations are replayed.

## Reconciliation rules

Reconciliation is a Git-like three-way semantic rebase: compare the bundle's
base, its requested operations, and the current canonical view.

- The base must exist in canonical history. Unknown ancestry refuses.
- The same uid with the same identity is a no-op. Reapplying an accepted bundle
  appends nothing.
- Independent identities can replay in reviewed order only after the existing
  registry, identity, reachability, connectivity, gauge, and dimensional gates
  pass.
- A metadata field changed differently on both sides is a structured conflict.
  Different fields can rebase independently.
- A bundle that adds a new identity while retiring a base identity with the
  same semantic anchor is a re-mint, not an independent addition. It requires
  an explicit human decision: replacements use `supersede`, and equivalence
  claims use `equate`. A new operator that leaves existing same-output
  producers live remains an independent alternative and still passes the
  normal gates.
- Edit/deprecate, supersede/deprecate, competing supersede/equate, and missing
  dependency cases refuse instead of choosing a winner.

The receipt records the bundle and optional local source head, the canonical
head before reconciliation, accepted and no-op operations, structured
conflicts, newly assigned canonical record versions, and the resulting head.
Its own id is recomputable from those fields.

This is deliberately not a network protocol, CRDT, automatic equivalence
engine, multi-parent chain, or user-interface merge editor. Review chooses one
canonical order, just as a Git maintainer chooses the history that lands.
