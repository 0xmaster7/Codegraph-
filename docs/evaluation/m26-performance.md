# M26 target-Mac performance measurement

## Test environment

The measurement ran on the intended demo host: MacBookPro15,1 (2018 15-inch Intel), Core i7-8850H with 6 cores, 16GB RAM, macOS 15.7.9 x86_64. Docker's VM memory limit was 8,400,605,184 bytes. The benchmark repository was the frozen Requests v2.32.5 checkout at commit `b25c87d7cb8d6a18a37fa12442b5f883f9e41741`.

FalkorDB ran in a separate ephemeral container named `codex-m26-falkordb`, limited to 2 CPUs and 2GiB RAM, with a loopback-only port and no persistent volume. The already-running `wemakedevs-track1-falkordb-1` service and its persistent volume were not stopped, reconfigured, or written by the benchmark. Docker-wide memory figures below are read-only observations and include this existing service and other already-running containers.

## Baseline before optimization

The cold ingestion produced a READY, schema-valid snapshot with 827 entities, 2,937 sites, 7,498 provenance records, and 866 retrieval chunks.

| Measure | Baseline |
| --- | ---: |
| Total analysis | 41.59s |
| Materialization stage | 0.346s |
| Parsing stage | 0.831s |
| Semantic indexing and join | 38.09s |
| Validation and cache write | 0.207s |
| Graph publication | 37.24s |
| First local ONNX encoding | 98.71s |
| Vector persistence and index | 3.62s |
| Callers endpoint P50 / P95 (25 in-process requests) | 462 / 622ms |
| Backend process peak RSS | 1.45GB |
| Isolated FalkorDB container | 109.7MiB / 2GiB |
| Initial Graph Explorer render | 2,649ms; 400 rows and 60 relationships |

The vector provider was local MiniLM ONNX on CPU. It made no hosted API calls and incurred no hosted cost. The endpoint timing includes snapshot loading, the READY guard, deterministic tool construction, query, and response serialization; it excludes browser/network latency.

## Optimization and browser comparison

The baseline React render joined each of 7,498 provenance records by scanning the complete bindings and derived-edge arrays. It also built every graph row before applying the 400-row display cap. The Overview unresolved-site count repeatedly scanned all bindings for every site.

The M26 change uses edge-ID maps for constant-time joins, memoizes the Explorer rows, exits row construction when the visible page is full, renders the first 100 rows, and adds a “Show next” action to expand in 100-row batches up to 400. Filtering still walks the complete ordered dataset, and the graph canvas still shows the same first 60 relationships. Overview now checks a set of bound site IDs.

The same READY Requests snapshot was exercised in the actual local Edge browser after the change:

| Measure | Before | After |
| --- | ---: | ---: |
| Graph Explorer transition | 2,649ms, one baseline observation | P50 797ms / P95 1,498ms across 7 samples |
| Initial graph rows | 400 | 100 |
| Relationship cards | 60 | 60 |
| Overview transition | Not recorded before the change | P50 396ms / P95 510ms across 7 samples |

The post-change median graph transition was 69.9% below the baseline observation; the measured P95 was 43.4% below that observation. The browser check also expanded the list to 400, selected the Edges filter, and searched for repository paths. All checks returned the expected rows. Each post-change timing brackets the actual UI click with page `Date.now()` reads; the separate accessibility-tree serialization is excluded.

## Resource observations and limits

The isolated FalkorDB container used 101MiB of its 2GiB cap after the browser run. Docker reported about 801.3MiB across all running containers at that point; that total includes the pre-existing persistent WeMakeDevs FalkorDB service and unrelated already-running containers, so it is not attributable to M26 alone. The Docker VM cap was 8.40GB.

During a 30-second sample at 5-second intervals after the browser run, host memory free percentage was 64%, 61%, 62%, 63%, 54%, and 52%. Swap use was 14,300.75MB, 13,916.25MB, 13,966.25MB, 14,009MB, 13,169MB, and 12,993.75MB. Swap use decreased over the sample window and memory pressure stayed at or above 52% free, so no sustained swap thrashing was observed during this workload. The host still has high absolute swap use; these samples are not a long-duration soak test.

The changed code only affects frontend rendering, so ingestion, graph publication, embedding, and API benchmarks were not rerun after the change. Their measured baseline values are retained for comparison and are not represented as post-change gains. The frontend production build and all 7 existing runtime-approval UI state tests passed.
