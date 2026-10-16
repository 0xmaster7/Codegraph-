const EXACT_REPLAY_FAILURES = new Set(["EDGE_NOT_REPRODUCED", "SOURCE_HASH_MISMATCH"]);
const REPLAY_INCONCLUSIVE = new Set(["ERROR", "ENVIRONMENT_MISMATCH", "EXTRACTOR_VERSION_MISMATCH", "PREMISE_MISSING"]);

export function humanRelationshipLabel(relation, modality = "") {
  const value = String(relation ?? "").toUpperCase();
  const mode = String(modality ?? "").toUpperCase();
  if (value.includes("UNRESOLVED") || mode.includes("UNRESOLVED")) return "Target unresolved";
  if (value.includes("RUNTIME") || mode.includes("RUNTIME")) return "Runtime observed call";
  if (value === "DEFINES_METHOD") return "Defines method";
  if (value === "DEFINES") return "Defines";
  if (value === "INHERITS") return "Inherits from";
  if (value === "OVERRIDES") return "Overrides";
  if (value === "MAY_DISPATCH_TO" || mode.includes("MAY")) return "May call";
  if (["CALL_SITE_BINDING", "BINDS_TO", "MUST_CALL", "EXACT"].includes(value)) return "Calls";
  if (value.includes("IMPORT")) return "Imports";
  if (value.includes("CALL")) return "Calls";
  return value.toLowerCase().replaceAll("_", " ").replace(/^\w/, (letter) => letter.toUpperCase()) || "Code relationship";
}

export function evidenceStateLabel({ modality = "", replayState = null, reviewState = "NONE" } = {}) {
  const mode = String(modality).toUpperCase();
  const replay = String(replayState ?? "").toUpperCase();
  if (mode.includes("UNRESOLVED")) return "Unresolved target";
  if (mode.includes("RUNTIME")) return "Runtime observation";
  if (mode.includes("MAY")) return "Possible call";
  if (EXACT_REPLAY_FAILURES.has(replay)) return replay === "SOURCE_HASH_MISMATCH" ? "Source changed · excluded from exact paths" : "Replay mismatch · excluded from exact paths";
  if (REPLAY_INCONCLUSIVE.has(replay)) return "Replay inconclusive";
  if (replay === "PASS" && reviewState === "NONE") return "Replay verified";
  if (mode.includes("DERIVED")) return "Derived static relationship";
  if (reviewState && reviewState !== "NONE") return "Static evidence flagged for review";
  if (replay === "NOT_RUN") return "Exact static relationship · replay not run";
  return "Exact static relationship";
}

export function replayStateLabel(value) {
  const replay = String(value ?? "").toUpperCase();
  if (replay === "PASS") return "Replay verified";
  if (replay === "NOT_RUN") return "Replay not run";
  if (replay === "EDGE_NOT_REPRODUCED") return "Replay mismatch";
  if (replay === "SOURCE_HASH_MISMATCH") return "Source changed since capture";
  if (REPLAY_INCONCLUSIVE.has(replay)) return "Replay inconclusive";
  return replay ? replay.toLowerCase().replaceAll("_", " ").replace(/^\w/, (letter) => letter.toUpperCase()) : "Replay status unavailable";
}

export function humanSymbolName(name, kind = "") {
  const value = String(name ?? "");
  if (["Function", "Method"].includes(String(kind))) return `${value.replace(/\(\)$/, "")}()`;
  return value;
}

export function humanClaimType(value) {
  const labels = { DIRECT_CALL: "Direct call", STRUCTURAL_PATH: "Call path", PASSAGE_STATEMENT: "Source passage" };
  return labels[String(value ?? "").toUpperCase()] ?? String(value ?? "Evidence").toLowerCase().replaceAll("_", " ").replace(/^\w/, (letter) => letter.toUpperCase());
}

export function humanDispatchState(value, candidateCount = 0) {
  const state = String(value ?? "").toUpperCase();
  if (state === "SINGLE_CANDIDATE") return `One possible target${candidateCount ? ` · ${candidateCount}` : ""}`;
  if (state === "MULTI_CANDIDATE") return `Several possible targets${candidateCount ? ` · ${candidateCount}` : ""}`;
  if (state === "UNKNOWN") return "Possible target unknown";
  if (state === "NOT_APPLICABLE") return "Not applicable";
  return "Dispatch state unavailable";
}

export function humanExternalName(semanticSymbol, distributionName = null, moduleName = null) {
  const value = String(semanticSymbol ?? "");
  if (/^local\s+\d+$/i.test(value)) return distributionName ?? moduleName ?? "External code";
  const rawName = value.split(/\s+/).at(-1) ?? value;
  const shortName = rawName.split("/").at(-1)?.replace(/[#.]+$/, "").replaceAll("#", ".");
  return shortName || distributionName || moduleName || "External code";
}

export function humanAnswerText(value) {
  return String(value ?? "")
    .replace(/\bA verified graph path connects\b/gi, "An exact static path connects")
    .replace(/\bA possible graph path connects\b/gi, "A possible call path connects");
}

export function humanRepositoryName(root) {
  const segment = String(root ?? "").split("/").filter(Boolean).at(-1) ?? "";
  if (!segment || /^[0-9a-f]{12,}(?:[-_][0-9a-f]{8,})+$/i.test(segment)) return "Python repository";
  return segment;
}
