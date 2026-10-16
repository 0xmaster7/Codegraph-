/** @typedef {import('./runtimeApprovalState').ActionApproval} ActionApproval */

/** @param {ActionApproval | null} approval @param {boolean} actionBusy */
export function canExecuteApproval(approval, actionBusy) {
  return approval?.status === "APPROVED" && !actionBusy;
}

/**
 * @param {string} approvalId
 * @param {typeof fetch} [fetcher]
 * @returns {Promise<ActionApproval>}
 */
export async function fetchAuthoritativeApproval(
  approvalId,
  fetcher = fetch,
  apiBaseUrl = "http://127.0.0.1:8000",
) {
  const response = await fetcher(
    `${apiBaseUrl.replace(/\/+$/, "")}/api/runtime/action/${encodeURIComponent(approvalId)}`,
  );
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(
      typeof payload?.detail === "string" ? payload.detail : "Could not refresh approval state",
    );
  }
  const approval = /** @type {ActionApproval} */ (payload);
  if (approval.approval_id !== approvalId) {
    throw new Error("Backend returned a different approval identity");
  }
  return approval;
}

/** @template T @param {Response} response @returns {Promise<T>} */
export async function parseExecutionResponse(response) {
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(
      typeof payload?.detail === "string" ? payload.detail : "Approved test execution failed",
    );
  }
  return /** @type {T} */ (payload);
}

/** @template T @param {string} approvalId @param {ActionApproval} approval @param {T} execution */
export function applyAuthoritativeExecution(approvalId, approval, execution) {
  if (approval.approval_id !== approvalId) {
    throw new Error("Backend returned a different approval identity");
  }
  return { approval, execution };
}

/** Prevents duplicate submissions from multiple clicks before React rerenders. */
export class SingleFlightGuard {
  #active = false;

  enter() {
    if (this.#active) return false;
    this.#active = true;
    return true;
  }

  leave() {
    this.#active = false;
  }
}
