const defaultApiBaseUrl = "http://127.0.0.1:8000";
const configuredApiBaseUrl = import.meta.env.VITE_CODEGRAPH_API_BASE_URL?.trim();

export const API_BASE_URL = (configuredApiBaseUrl || defaultApiBaseUrl).replace(/\/+$/, "");
