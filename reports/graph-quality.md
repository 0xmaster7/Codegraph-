# M25 graph quality: Requests v2.32.5

- Frozen repository commit: `b25c87d7cb8d6a18a37fa12442b5f883f9e41741`
- Annotation: `requests-v2.32.5-m10-v1`
- Requests snapshot: `snap_34b5c73e702277e8060a1f4885bddd584b982f4f6ebdfc6b43641ef6ab605f50`

## Annotated real repository

| Measure | Result |
|---|---:|
| Definitions precision / recall | 1.000 / 1.000 (53/53) |
| Imports precision / recall | 1.000 / 1.000 (58/58) |
| Exact binding precision / recall | 1.000 / 1.000 (100/100) |
| False-exact count / rate | 0 / 0.000 |
| Evidence paths | 30/30 (1.000) |
| Provenance coverage | 1.000 |
| Schema-invalid facts / dangling IDs / duplicate edge IDs | 0 / 0 / 0 |
| Unicode coordinate errors | 0 |
| Dispatch and runtime mismatch | Unmeasured: frozen Requests annotation contains no dispatch cases or runtime observations |

## Controlled fixture

- Verification: **PASS** (2 targeted real-SCIP tests).
- Fixture cases: 54; definitions 47; imports 25; exact bindings 3; dispatch cases 1
- All asserted controlled-fixture quality metrics passed; see `controlled_fixture` in the JSON for the test-backed values and reproduction command.

## Limitations

- Dispatch is checked on the controlled fixture, but the annotated Requests sample has no frozen dispatch cases.
- Requests runtime mismatch remains unmeasured because its frozen annotation contains no runtime observations.
