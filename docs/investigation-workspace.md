# Investigation workspace and Evidence Path

The M20 workspace is available for a `READY` snapshot in the local UI. Its
question composer submits one bounded request to `POST /api/query/investigate`
with the selected snapshot and, when present, the selected node, site, or edge.
The route uses the M18 orchestrator and M17 allowlisted tools once. It does not
call an answer-generation model or transmit repository content to a hosted
provider.

The server turns only returned M17 records into bounded candidate claims and
passes those candidates to the M19 `ClaimValidator`. The client cannot submit a
graph path or provenance record as authoritative. Only supported or qualified
claims are rendered. Direct calls use exact static binding records; structural
paths preserve their ordered nodes, relationships, evidence IDs, provenance
IDs, snapshot, and exact/derived/conservative modality. Unresolved barriers
and result truncation remain visible.

Lexical and semantic passages appear in a separate passage panel with their
original text, repository-relative source, content hash, chunk/source identity,
and evidence pointer. They are never rendered as graph edges. Runtime evidence
is shown separately with its test definition, invocation, run IDs, and
observation kinds. Each call observation also retains its source/target pair,
run, invocation, runtime materialization, observation ID, and evidence ID; it
does not become a static claim.

Clicking graph evidence opens its canonical edge in the existing inspector.
Passage and runtime evidence open a record detail card. Selecting a node, site,
or edge in the inspector attaches that identity to the next question as optional
context. This supports call-site follow-ups without changing the M18 routing
rules.

The workspace labels evidence with text as well as styling. Exact static
bindings, derived graph relationships, conservative possible dispatch, runtime
observations, and retrieved passages remain distinguishable. Conservative
path edges remain visibly marked as possible dispatch. The overview also lists
possible dispatch candidates separately from exact bindings. A path or claim
is omitted if M19 cannot validate its server-produced evidence.
