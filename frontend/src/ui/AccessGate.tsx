import { FormEvent, ReactNode, useEffect, useState } from "react";
import { API_BASE_URL } from "../apiConfig";

type Session = { enabled: boolean; authenticated: boolean };

export default function AccessGate({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function check() {
    try {
      const response = await fetch(`${API_BASE_URL}/api/auth/session`, { cache: "no-store" });
      if (!response.ok) throw new Error("Cannot connect to CodeGraph. Please retry.");
      setSession(await response.json());
      setError("");
    } catch { setError("Cannot connect to CodeGraph. Please retry."); }
  }
  useEffect(() => {
    void check();
    const refresh = () => { void check(); };
    window.addEventListener("codegraph:unauthorized", refresh);
    const timer = window.setInterval(refresh, 60000);
    return () => { window.removeEventListener("codegraph:unauthorized", refresh); clearInterval(timer); };
  }, []);
  async function login(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const response = await fetch(`${API_BASE_URL}/api/auth/session`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Could not sign in.");
      setPassword(""); setSession(result);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not sign in."); }
    finally { setBusy(false); }
  }
  async function logout() {
    try {
      const response = await fetch(`${API_BASE_URL}/api/auth/session`, { method: "DELETE" });
      if (!response.ok) throw new Error();
      setSession({ enabled: true, authenticated: false });
    } catch { setError("Could not sign out. Please retry."); }
  }
  if (session?.authenticated) return <>{session.enabled && <div className="access-toolbar"><button type="button" onClick={() => void logout()}>Sign out</button>{error && <span role="alert">{error}</span>}</div>}{children}</>;
  return <main className="access-screen"><section className="access-card"><span className="section-label">CODEGRAPH</span><h1>Explore your code.</h1><p>Sign in to your private workspace.</p>{session ? <form onSubmit={login}><label htmlFor="workspace-password">Workspace password</label><input id="workspace-password" type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} required autoFocus maxLength={256}/><button type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button></form> : <button onClick={() => void check()}>{error ? "Retry connection" : "Connecting…"}</button>}{error && <p role="alert" className="access-error">{error}</p>}</section></main>;
}
