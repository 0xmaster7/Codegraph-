import { FormEvent, ReactNode, useEffect, useState } from "react";
import { API_BASE_URL } from "../apiConfig";

type Session = { enabled: boolean; authenticated: boolean; public_browsing: boolean };

export default function AccessGate({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showLogin, setShowLogin] = useState(false);
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
    const requireOwner = () => { setShowLogin(true); void check(); };
    window.addEventListener("codegraph:unauthorized", requireOwner);
    const timer = window.setInterval(refresh, 60000);
    return () => { window.removeEventListener("codegraph:unauthorized", requireOwner); clearInterval(timer); };
  }, []);
  async function login(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const response = await fetch(`${API_BASE_URL}/api/auth/session`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Could not sign in.");
      setPassword(""); setSession(result); setShowLogin(false);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not sign in."); }
    finally { setBusy(false); }
  }
  async function logout() {
    try {
      const response = await fetch(`${API_BASE_URL}/api/auth/session`, { method: "DELETE" });
      if (!response.ok) throw new Error();
      setSession(previous => previous && ({ ...previous, authenticated: false }));
    } catch { setError("Could not sign out. Please retry."); }
  }
  const loginForm = <form onSubmit={login}><label htmlFor="workspace-password">Owner password</label><input id="workspace-password" type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} required maxLength={256}/><button type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button></form>;
  if (session && (session.authenticated || session.public_browsing)) return <>
    {session.enabled && <div className="access-toolbar">
      {!session.authenticated && <span>Explore CodeGraph · no sign-in needed</span>}
      <button type="button" onClick={() => session.authenticated ? void logout() : setShowLogin(value => !value)}>{session.authenticated ? "Sign out" : showLogin ? "Close sign-in" : "Owner sign-in"}</button>
      {error && <span role="alert">{error}</span>}
    </div>}
    {!session.authenticated && showLogin && <section className="access-card access-owner-panel" aria-label="Owner sign-in"><p>Sign in to add repositories, manage snapshots, or run code.</p>{loginForm}</section>}
    {children}
  </>;
  return <main className="access-screen"><section className="access-card"><span className="section-label">CODEGRAPH</span><h1>Explore your code.</h1><p>Sign in to your private workspace.</p>{session ? loginForm : <button onClick={() => void check()}>{error ? "Retry connection" : "Connecting…"}</button>}{error && <p role="alert" className="access-error">{error}</p>}</section></main>;
}
