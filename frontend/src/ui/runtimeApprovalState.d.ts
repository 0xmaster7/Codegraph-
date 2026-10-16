export type ActionApproval = {
  approval_id: string;
  snapshot_id: string;
  runtime_environment_id: string;
  run_id: string | null;
  test_ids: string[];
  pytest_command: string[];
  exact_command_hash: string;
  expires_at: number;
  status: "PENDING" | "APPROVED" | "CONSUMED" | "EXPIRED" | "REVOKED";
};

export function canExecuteApproval(
  approval: ActionApproval | null,
  actionBusy: boolean,
): boolean;
export function fetchAuthoritativeApproval(
  approvalId: string,
  fetcher?: typeof fetch,
  apiBaseUrl?: string,
): Promise<ActionApproval>;
export function parseExecutionResponse<T>(response: Response): Promise<T>;
export function applyAuthoritativeExecution<T>(
  approvalId: string,
  approval: ActionApproval,
  execution: T,
): { approval: ActionApproval; execution: T };

export class SingleFlightGuard {
  enter(): boolean;
  leave(): void;
}
