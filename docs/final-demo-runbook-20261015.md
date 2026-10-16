# CodeGraph recording runbook — Click 8.5.0

## Demo source and evidence

- Upstream: <https://github.com/pallets/click>.
- Source commit: `8b19813f2bfca99f1018a587a8cf54fc959f2e5d` (Click 8.5.0).
- Primary READY snapshot: `snap_4eda820463ad40306ca36ddd5dc91336d981577164b895a1707c7f0b1a0ff1f0`.
- Diff base: `2103e157683c5e4cadc8ee1838df526a54bde9a4`.
- Diff target: `8b19813f2bfca99f1018a587a8cf54fc959f2e5d`.
- The diff service materializes separate immutable commit snapshots: base `snap_59636c6938679d25b59cd900c4d32a0e9b31e6f22958f515c2adef53407760da`, target `snap_ad4c06703b8b52e12176215231e6ae0c2525dd3842b334bb3eb6b935cd2eb403`. Both describe the same official Click repository; the target has the same Git commit as the primary snapshot. Do not imply that these snapshot IDs are identical.

Click is the best recording candidate because corrected ingestion reached READY, its argument parser has a two-step source-backed function-call path, the graph and Inspector render each call, and a real release diff identifies the existing `Editor.edit_files` change and two callers. Flask reached READY but its tested release diff was not prepared for this recording. Requests previously supported approved sandbox execution, but its retained demo graph predates the document-local SCIP identity fix. Do not use that old graph as proof of current general correctness.

The Click diff correctly returns **no test recommendations**: no runtime baseline or configured immutable test image was available in this isolated environment. Show the explicit abstention. Do not approve or execute tests during the recording. An earlier, separately verified Requests run demonstrated approval and two passing tests; that is historical evidence, not a Click result.

## Start or resume the isolated demo

Working directory: `/Users/keshavnanda/wemakedevs - track1`. Disposable official checkout: `/private/tmp/codegraph-validation-20261014/repos/click`. Evidence: `/private/tmp/codegraph-validation-20261014/evidence`.

The existing non-auto-removed FalkorDB container owns port `127.0.0.1:16395` and has no mounted volume. Keep that container; stopping and starting it retains its data, while removing it loses the database. Check it, then start it only if stopped:

```sh
docker ps --filter name=codegraph-final-20261014-falkordb
docker start codegraph-final-20261014-falkordb
```

The verified backend command is:

```sh
cd '/Users/keshavnanda/wemakedevs - track1'
env APP_HOST=127.0.0.1 APP_PORT=18001 \
  FALKORDB_HOST=127.0.0.1 FALKORDB_PORT=16395 \
  ALLOWED_REPOSITORY_ROOTS='/private/tmp/codegraph-validation-20261014/repos/flask,/private/tmp/codegraph-validation-20261014/repos/click' \
  SNAPSHOT_CACHE_DIR='/private/tmp/codegraph-validation-20261014/cache/final/snapshots' \
  SCIP_PYTHON_BIN='/Users/keshavnanda/wemakedevs - track1/node_modules/.bin/scip-python' \
  SCIP_CLI_BIN='/Users/keshavnanda/wemakedevs - track1/.tools/scip/scip' \
  LOCAL_QUERY_ANALYZER_ENABLED=false PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port 18001
```

In another terminal, run the verified frontend configuration:

```sh
cd '/Users/keshavnanda/wemakedevs - track1'
VITE_CODEGRAPH_API_BASE_URL=http://127.0.0.1:18001 npm run dev -- --port 5173
```

Open <http://127.0.0.1:5173/> at 1440×900. The demo frontend and backend are already running at these addresses as of this runbook. The UI may list two `click · 8b19813 · ready` entries after the diff. Select the one whose snapshot ID is the **primary** ID above (currently the third dropdown entry). Its repository label is `click`; the newer diff materialization has a temporary commit directory as its label. All four final Flask/Click snapshots passed schema validation and contain zero `local N` external symbols.

For safe shutdown, send Ctrl-C to only these backend and Vite terminals, then run `docker stop codegraph-final-20261014-falkordb` if the isolated database should be stopped. Do not remove the container or cache. Do not stop or alter the existing Requests services or FalkorDB containers on ports 6379, 16379, or 16391.

## Four-minute recording sequence

| Time | Screen and exact action | Expected result and suggested narration |
| --- | --- | --- |
| 0:00–0:20 | Show CodeGraph home and open the READY Click 8.5.0 primary snapshot. | “Here is an unfamiliar Python project. CodeGraph gives me a snapshot of its structure so I can inspect a change before trusting an answer.” Skip ingestion on camera; the snapshot is already indexed. |
| 0:20–0:55 | In **Investigate**, type `What exact call path connects src.click.parser._OptionParser.parse_args to src.click.parser._OptionParser._process_opts through src.click.parser._OptionParser._process_args_for_options within 2 hops?` and click **Investigate**. | The exact static path is `parse_args()` → **Calls** → `_process_args_for_options()` → **Calls** → `_process_opts()`. It is two calls in one class, not a class-membership edge. The route evidence is `src/click/parser.py:309` and `src/click/parser.py:336`. Say: “Click parses the arguments, processes option arguments, then processes the options themselves. CodeGraph found both calls in the source.” |
| 0:55–1:30 | Show **How the code connects** and click **View source evidence** on the first edge. Then click the second edge **_process_args_for_options() → _process_opts()**. | Each relationship is labeled **Calls**, classified **Exact static relationship · replay not run**, and linked to its source line. The graph highlights the route. Keep the short symbol names and file locations visible; raw IDs stay under **Technical details**. |
| 1:30–2:00 | In Graph Inspector, show the second relationship and its source preview. | Inspector shows **Calls**, exact static target, `src/click/parser.py`, location `336:16–336:34`, and the source preview for `_process_opts`. Replay remains explicitly **not run**. Say: “The source evidence is one click away. Static resolution is exact here; this does not claim the call was replayed.” |
| 2:00–2:50 | Click **Changes & tests**. Enter base `2103e157683c5e4cadc8ee1838df526a54bde9a4`, keep target `8b19813f2bfca99f1018a587a8cf54fc959f2e5d`, and click **Analyze diff**. Scroll to the `MODIFIED` card for `src.click._termui_impl.Editor.edit_files` and the **Impact paths 2** section. | The real release diff has 8 changed files, 11 changed symbol records, and 2 exact one-hop dependent paths. Verified callers are `src.click.termui.edit` at `src/click/termui.py:909` (`ed.edit_files`) and `Editor.edit` at `src/click/_termui_impl.py:783` (`self.edit_files`). Say: “The changed method is identified from the Git ranges, and these two dependent call sites are grounded in the target graph.” The impact cards show a concise path label and source location; expand Technical details only when needed. |
| 2:50–3:20 | Show **Recommended tests** and the workflow strip. | Tier A/B say no runtime baseline; Tier C says collection unavailable; there are zero recommendations and no approve or execute action to take. The diff also identifies added `tests.test_termui.test_edit_pathlib`, but **do not call it a recommendation**. Say: “This environment does not have a verified test baseline or immutable test image, so CodeGraph stops here instead of inventing a command or running tests.” |
| 3:20–3:45 | Return to **Explore graph** and leave the highlighted `Editor` path visible. | “FalkorDB stores the snapshot’s entities, bindings, and provenance so I can traverse from a proposed change to grounded evidence, then judge the impact myself.” End on the populated graph. |

## Source checks and limits

- The pinned target source has `parse_args` call `_process_args_for_options` at `src/click/parser.py:309`, and `_process_args_for_options` call `_process_opts` at line 336. Both exact call edges are present in the READY target graph and have source evidence. `Editor.edit_files` remains the change-impact example: it is defined at `src/click/_termui_impl.py:713`; it calls `self.get_editor()` at line 718. `Editor.edit` calls `self.edit_files` at line 783; public `click.termui.edit` calls `ed.edit_files` at line 909. A structural path is static evidence, not proof that the call executed at runtime.
- The `edit` overloads and public `termui.edit` overloads produce ambiguous same-signature symbol matches in the diff. CodeGraph labels them `UNMAPPED`; do not claim a one-to-one symbol pairing for those overload records.
- The diff service uses separate commit materializations and therefore different base/target snapshot IDs. Both are READY and contain zero document-local external identities. The original checkout snapshot remains selected for browser investigation so Changes & Tests has a live repository path.
- No Click tests were approved or executed. The recorded prior Requests sandbox run is a separate historical result and should not be spliced into this Click video. On 2026-10-15, the browser **Analyze diff** action displayed “Required snapshot analysis is unavailable,” while the same local `/api/diff/analyze` request for these SHAs returned HTTP 200. Retry the UI action before recording this scene; do not claim its browser view passed until the results render.
- The local query analyzer is disabled in this isolated demo. Deterministic routing and claim validation produced the shown answer.

## Recording fallbacks

- If the question takes several seconds, keep the **Working** indicator visible and wait for the validated answer. The measured corrected Click API queries completed in roughly 1–4 seconds; browser rendering may take longer.
- If the graph is clipped, click **Fit graph**, narrow search to `Editor.edit_files`, or scroll down to center the graph. Do not change the question or use a fabricated path.
- If the browser loses its saved investigation, select the exact saved Click Editor question from **Saved investigations** or type it again. Verify the primary snapshot ID before recording.
- If the diff request temporarily fails, retry the same two real SHAs after confirming backend port 18001 and FalkorDB port 16395. Both commit snapshots are already READY, so no indexing should be needed.
- If no test recommendations appear, use the verified abstention scene above. Do not fill the gap with an ad hoc pytest command or approval.

## Captured browser evidence

The earlier saved screenshots in `/private/tmp/codegraph-validation-20261014/evidence/` (`demo-click-editor-*.jpg`) show the previous Editor investigation UI and are historical only. The updated parser-path graph and Inspector were captured from the live application in the acceptance handoff; API evidence remains `demo-click-final-question.json` and `click-final-diff.json`. These are real Requests/Click-backed results, not mockups.
