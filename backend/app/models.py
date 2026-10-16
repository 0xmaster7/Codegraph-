from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .identifiers import normalize_identifier


class SnapshotStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    MATERIALIZING = "MATERIALIZING"
    PARSING = "PARSING"
    SEMANTIC_INDEXING = "SEMANTIC_INDEXING"
    VALIDATING = "VALIDATING"
    READY = "READY"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    GC_PENDING = "GC_PENDING"


class IngestionJobStatus(StrEnum):
    QUEUED = "QUEUED"
    MATERIALIZING = "MATERIALIZING"
    PARSING = "PARSING"
    SEMANTIC_INDEXING = "SEMANTIC_INDEXING"
    VALIDATING = "VALIDATING"
    PUBLISHING = "PUBLISHING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class RuntimeRunStatus(StrEnum):
    PLANNED = "PLANNED"
    APPROVED = "APPROVED"
    RUNNING = "RUNNING"
    PERSISTING = "PERSISTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class ActionApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class RuntimeContext(StrEnum):
    COLLECTION_OR_SESSION = "COLLECTION_OR_SESSION"
    TEST_INVOCATION = "TEST_INVOCATION"


class SubprocessCoverage(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class RuntimeSiteValidationState(StrEnum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNRESOLVED = "UNRESOLVED"
    UNOBSERVED = "UNOBSERVED"


class SiteKind(StrEnum):
    CALL_CALLEE = "CALL_CALLEE"
    VALUE_REFERENCE = "VALUE_REFERENCE"
    TYPE_ANNOTATION = "TYPE_ANNOTATION"
    DECORATOR_REFERENCE = "DECORATOR_REFERENCE"
    BASE_CLASS_REFERENCE = "BASE_CLASS_REFERENCE"
    IMPORT_REFERENCE = "IMPORT_REFERENCE"
    CALLBACK_REFERENCE = "CALLBACK_REFERENCE"
    OTHER = "OTHER"


class DiagnosticCode(StrEnum):
    PARTIAL_PARSE = "PARTIAL_PARSE"
    UNSUPPORTED_SYNTAX_VERSION = "UNSUPPORTED_SYNTAX_VERSION"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    NOTEBOOK_UNSUPPORTED = "NOTEBOOK_UNSUPPORTED"
    VENDORED_CODE = "VENDORED_CODE"
    SCIP_UNAVAILABLE = "SCIP_UNAVAILABLE"
    SCIP_POSITION_ENCODING_UNSPECIFIED = "SCIP_POSITION_ENCODING_UNSPECIFIED"
    SCIP_DOCUMENT_MISSING = "SCIP_DOCUMENT_MISSING"
    RESOLVER_ERROR = "RESOLVER_ERROR"
    UNRESOLVED_NAME = "UNRESOLVED_NAME"
    UNSUPPORTED_V1 = "UNSUPPORTED_V1"
    PATH_REJECTED = "PATH_REJECTED"


class ReplayScope(StrEnum):
    FILE_OR_SPAN = "FILE_OR_SPAN"
    SNAPSHOT_SEMANTIC = "SNAPSHOT_SEMANTIC"
    RULE_PREMISES = "RULE_PREMISES"


class ReplayOutcome(StrEnum):
    NOT_RUN = "NOT_RUN"
    PASS = "PASS"
    SOURCE_HASH_MISMATCH = "SOURCE_HASH_MISMATCH"
    ENVIRONMENT_MISMATCH = "ENVIRONMENT_MISMATCH"
    EXTRACTOR_VERSION_MISMATCH = "EXTRACTOR_VERSION_MISMATCH"
    EDGE_NOT_REPRODUCED = "EDGE_NOT_REPRODUCED"
    PREMISE_MISSING = "PREMISE_MISSING"
    ERROR = "ERROR"


FAIL_CLOSED_REPLAY_OUTCOMES = frozenset(
    {ReplayOutcome.EDGE_NOT_REPRODUCED, ReplayOutcome.SOURCE_HASH_MISMATCH}
)


class ReviewState(StrEnum):
    NONE = "NONE"
    RESOLVER_DISAGREEMENT = "RESOLVER_DISAGREEMENT"
    REPLAY_FAILED = "REPLAY_FAILED"
    RUNTIME_MISMATCH = "RUNTIME_MISMATCH"
    SCHEMA_WARNING = "SCHEMA_WARNING"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"


class SourceSpan(BaseModel):
    model_config = ConfigDict(frozen=True)

    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    start_line: int = Field(ge=1)
    start_column: int = Field(ge=0)
    end_line: int = Field(ge=1)
    end_column: int = Field(ge=0)


class Diagnostic(BaseModel):
    code: DiagnosticCode
    message: str
    file_path: str | None = None
    span: SourceSpan | None = None


class SourceEntity(BaseModel):
    entity_id: str
    kind: str
    name: str
    qualified_name: str
    file_path: str
    span: SourceSpan
    parent_id: str | None = None
    identifier_raw: str = ""
    identifier_search_text: str = ""

    @model_validator(mode="before")
    @classmethod
    def populate_identifier_fields(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        fields = dict(value)
        name = str(fields.get("name", ""))
        fields.setdefault("identifier_raw", name)
        fields.setdefault("identifier_search_text", normalize_identifier(name))
        return fields


class SourceSite(BaseModel):
    site_id: str
    kind: SiteKind
    expression: str
    file_path: str
    span: SourceSpan
    context_id: str
    syntax_node_type: str


class ProvenanceRecord(BaseModel):
    provenance_id: str
    edge_id: str
    snapshot_id: str
    file_path: str
    file_content_hash: str
    span: SourceSpan
    source_coordinate_encoding: str = "UTF-8_BYTE_OFFSETS"
    scip_document: str | None = None
    scip_symbol: str | None = None
    scip_position_encoding: str | None = None
    syntax_node_type: str
    extractor_name: str = "tree-sitter-python"
    extractor_version: str
    rule_id: str
    premise_edge_ids: list[str] = Field(default_factory=list)
    replay_scope: ReplayScope = ReplayScope.FILE_OR_SPAN
    replay_state: ReplayOutcome = ReplayOutcome.NOT_RUN
    review_state: ReviewState = ReviewState.NONE


class ReplayResult(BaseModel):
    edge_id: str
    scope: ReplayScope
    outcome: ReplayOutcome
    review_state: ReviewState
    message: str
    verified_environment_id: str | None = None


class ExternalSymbol(BaseModel):
    external_symbol_id: str
    semantic_symbol: str
    environment_id: str
    module_name: str | None = None
    distribution_name: str | None = None
    distribution_version: str | None = None
    external_kind: str = "UNKNOWN_EXTERNAL"


class SemanticBinding(BaseModel):
    binding_id: str
    edge_id: str
    site_id: str
    target_entity_id: str | None = None
    external_symbol_id: str | None = None
    scip_symbol: str
    occurrence_start_byte: int = Field(ge=0)
    binding_state: str = "EXACT"
    resolver: str = "scip-python"


class StructuralImport(BaseModel):
    edge_id: str
    site_id: str
    source_module_id: str
    target_module_id: str | None = None
    target_external_symbol_id: str | None = None
    imported_module: str
    resolver: str
    span: SourceSpan


class DerivedEdge(BaseModel):
    edge_id: str
    relation: str
    source_id: str
    target_id: str
    rule_id: str
    premise_edge_ids: list[str] = Field(default_factory=list)


class DispatchState(StrEnum):
    SINGLE_CANDIDATE = "SINGLE_CANDIDATE"
    MULTI_CANDIDATE = "MULTI_CANDIDATE"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class SiteDispatch(BaseModel):
    site_id: str
    state: DispatchState
    candidate_entity_ids: list[str] = Field(default_factory=list)
    mro_entity_ids: list[str] = Field(default_factory=list)
    mro_qualified_names: list[str] = Field(default_factory=list)


class AnalysisEnvironment(BaseModel):
    environment_id: str
    python_version: str
    platform: str
    python_executable_hash: str
    sys_path_hash: str
    installed_distributions_hash: str
    scip_python_version: str
    scip_cli_version: str
    node_version: str
    source_manifest_hash: str


class RuntimeEnvironment(BaseModel):
    runtime_environment_id: str
    snapshot_id: str
    python_version: str
    platform: str
    python_executable_hash: str
    sys_path_hash: str
    installed_distributions_hash: str
    source_manifest_hash: str
    image_digest: str | None = None
    runtime_materialization_id: str | None = None


class TestDefinition(BaseModel):
    test_definition_id: str
    snapshot_id: str
    file_path: str
    qualified_name: str
    pytest_name: str


class TestInvocation(BaseModel):
    test_invocation_id: str
    snapshot_id: str
    test_definition_id: str
    node_id: str
    parameter_id: str | None = None


class RuntimeRun(BaseModel):
    run_id: str
    snapshot_id: str
    runtime_environment_id: str
    status: RuntimeRunStatus
    started_at: float | None = None
    ended_at: float | None = None
    subprocess_coverage: SubprocessCoverage = SubprocessCoverage.UNAVAILABLE


class ActionApproval(BaseModel):
    approval_id: str
    snapshot_id: str
    runtime_environment_id: str
    run_id: str | None = None
    test_ids: list[str]
    pytest_command: list[str]
    exact_command_hash: str
    created_at: float
    expires_at: float
    single_use: bool = True
    status: ActionApprovalStatus = ActionApprovalStatus.PENDING


class RuntimeCallAggregate(BaseModel):
    site_id: str
    source_entity_id: str
    target_entity_id: str
    observation_count: int
    test_invocation_ids: list[str] = Field(default_factory=list)


class RuntimeSiteValidation(BaseModel):
    site_id: str
    state: RuntimeSiteValidationState
    static_candidate_entity_ids: list[str] = Field(default_factory=list)
    observed_target_entity_ids: list[str] = Field(default_factory=list)
    unobserved_candidate_entity_ids: list[str] = Field(default_factory=list)
    observation_count: int = 0


class RuntimeValidationReport(BaseModel):
    run_id: str
    snapshot_id: str
    observed_call_count: int = 0
    mapped_call_count: int = 0
    unmapped_call_count: int = 0
    mismatch_count: int = 0
    aggregates: list[RuntimeCallAggregate] = Field(default_factory=list)
    sites: list[RuntimeSiteValidation] = Field(default_factory=list)


class TestExecutionResult(BaseModel):
    run_id: str
    snapshot_id: str
    runtime_environment_id: str
    status: RuntimeRunStatus
    exit_code: int | None = None
    collected_node_ids: list[str] = Field(default_factory=list)
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    environment: dict[str, str] = Field(default_factory=dict)
    runtime_entries: list[dict[str, object]] = Field(default_factory=list)
    runtime_calls: list[dict[str, object]] = Field(default_factory=list)
    runtime_diagnostics: list[dict[str, object]] = Field(default_factory=list)
    runtime_validation: RuntimeValidationReport | None = None
    stdout: str = ""
    stderr: str = ""
    started_at: float
    ended_at: float


class RuntimeCollection(BaseModel):
    snapshot_id: str
    runtime_environment: RuntimeEnvironment
    test_definitions: list[TestDefinition] = Field(default_factory=list)
    test_invocations: list[TestInvocation] = Field(default_factory=list)
    collection_result: TestExecutionResult


class RuntimeFunctionEntry(BaseModel):
    entry_id: str
    run_id: str
    snapshot_id: str
    runtime_environment_id: str
    test_invocation_id: str | None = None
    context: RuntimeContext
    entity_id: str
    observed_at: float


class RuntimeCallObservation(BaseModel):
    observation_id: str
    run_id: str
    snapshot_id: str
    runtime_environment_id: str
    test_invocation_id: str | None = None
    context: RuntimeContext
    source_entity_id: str
    target_entity_id: str
    site_id: str | None = None
    observed_at: float


class RuntimeDiagnostic(BaseModel):
    diagnostic_id: str
    run_id: str
    snapshot_id: str
    code: str
    message: str
    file_path: str
    qualified_name: str
    first_line: int
    site_id: str | None = None


class RepositoryInfo(BaseModel):
    repository_id: str
    root: str
    remote: str | None = None
    head_commit: str | None = None
    is_dirty: bool
    is_shallow: bool = False
    is_detached: bool = False


class GitDeveloper(BaseModel):
    developer_id: str
    name: str
    email: str


class GitCommit(BaseModel):
    commit_id: str
    commit_sha: str
    parent_commit_ids: list[str] = Field(default_factory=list)
    parent_commit_shas: list[str] = Field(default_factory=list)
    author_id: str
    committer_id: str
    authored_at: str
    committed_at: str
    message: str


class GitChangeStatus(StrEnum):
    ADDED = "ADDED"
    MODIFIED = "MODIFIED"
    DELETED = "DELETED"
    RENAMED = "RENAMED"
    COPIED = "COPIED"
    TYPE_CHANGED = "TYPE_CHANGED"


class ChangedPath(BaseModel):
    change_id: str
    commit_id: str
    parent_commit_sha: str | None = None
    parent_index: int = Field(ge=0)
    status: GitChangeStatus
    old_path: str | None = None
    new_path: str | None = None
    similarity: int | None = Field(default=None, ge=0, le=100)


class DependencyRequirement(BaseModel):
    requirement_id: str
    name: str
    normalized_name: str | None = None
    raw_requirement: str
    specifier: str = ""
    marker: str | None = None
    extras: list[str] = Field(default_factory=list)
    source_file: str
    optional_group: str | None = None
    component_id: str | None = None
    supported: bool = True
    diagnostic: str | None = None


class ResolvedDistribution(BaseModel):
    distribution_id: str
    requirement_id: str
    distribution_name: str
    version: str
    import_names: list[str] = Field(default_factory=list)
    evidence_source: str = "importlib.metadata"


class ProjectComponent(BaseModel):
    component_id: str
    name: str
    root_path: str
    manifest_files: list[str] = Field(default_factory=list)
    source_paths: list[str] = Field(default_factory=list)
    dependency_requirement_ids: list[str] = Field(default_factory=list)


class DependencyMetadata(BaseModel):
    requirements: list[DependencyRequirement] = Field(default_factory=list)
    resolved_distributions: list[ResolvedDistribution] = Field(default_factory=list)
    components: list[ProjectComponent] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class IngestionJob(BaseModel):
    job_id: str
    snapshot_id: str
    status: IngestionJobStatus
    attached: bool = False
    cancel_requested: bool = False
    error: str | None = None


class SnapshotManifestEntry(BaseModel):
    path: str
    content_hash: str
    blob_hash: str
    size_bytes: int
    analyzed: bool
    classification: str = "PROJECT"


class RetrievalChunk(BaseModel):
    """Text-only retrieval evidence with provenance to its canonical source node."""

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    snapshot_id: str
    source_node_id: str
    file_path: str
    chunk_kind: str
    source_start_byte: int
    source_end_byte: int
    text: str
    content_hash: str
    chunk_strategy_version: str


class SnapshotAnalysis(BaseModel):
    repository: RepositoryInfo
    snapshot_id: str
    runtime_materialization_id: str | None = None
    status: SnapshotStatus
    manifest: list[SnapshotManifestEntry]
    entities: list[SourceEntity]
    retrieval_chunks: list[RetrievalChunk] = Field(default_factory=list)
    sites: list[SourceSite]
    provenance: list[ProvenanceRecord]
    bindings: list[SemanticBinding] = Field(default_factory=list)
    imports: list[StructuralImport] = Field(default_factory=list)
    derived_edges: list[DerivedEdge] = Field(default_factory=list)
    dispatch: list[SiteDispatch] = Field(default_factory=list)
    external_symbols: list[ExternalSymbol] = Field(default_factory=list)
    analysis_environment: AnalysisEnvironment | None = None
    developers: list[GitDeveloper] = Field(default_factory=list)
    commits: list[GitCommit] = Field(default_factory=list)
    changed_paths: list[ChangedPath] = Field(default_factory=list)
    dependency_metadata: DependencyMetadata = Field(default_factory=DependencyMetadata)
    diagnostics: list[Diagnostic]
    counts: dict[str, int]
    semantic_indexing: str = "NOT_RUN"
