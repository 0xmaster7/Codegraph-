interface ImportMetaEnv {
  readonly PROD: boolean;
  readonly VITE_CODEGRAPH_API_BASE_URL?: string;
  readonly VITE_CODEGRAPH_DEMO_SNAPSHOT_ID?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
