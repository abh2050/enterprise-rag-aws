import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type Me, type TokenProvider } from "./api";
import { AUTH_MODE, devTokenProvider, entraSignIn, entraTokenProvider } from "./auth";
import { Chat } from "./components/Chat";
import { DevLogin } from "./components/DevLogin";

export function App() {
  const [tokens, setTokens] = useState<TokenProvider | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);

  const signIn = useCallback(async (provider: TokenProvider) => {
    setError(null);
    try {
      setMe(await api.me(provider));
      setTokens(() => provider);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Sign-in failed.");
    }
  }, []);

  useEffect(() => {
    document.title = me ? `Document Q&A — ${me.display_name ?? "signed in"}` : "Enterprise Document Q&A";
  }, [me]);

  return (
    <div className="app">
      <header className="topbar">
        <h1>Enterprise Document Q&amp;A</h1>
        {me && (
          <div className="whoami" aria-label="Signed-in user">
            <span>{me.display_name}</span>
            <span className="pill">clearance: {me.clearance}</span>
            {me.inference_mode === "fixture" && (
              <span className="pill warn" title="Deterministic test models — answers are not evidence of model quality">
                FIXTURE models
              </span>
            )}
            <button
              type="button"
              className="link"
              onClick={() => {
                setMe(null);
                setTokens(null);
              }}
            >
              Sign out
            </button>
          </div>
        )}
      </header>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <main>
        {!tokens || !me ? (
          AUTH_MODE === "dev" ? (
            <DevLogin onSelect={(u) => void signIn(devTokenProvider(u))} />
          ) : (
            <div className="panel">
              <button
                type="button"
                className="primary"
                onClick={() =>
                  void entraSignIn()
                    .then(() => signIn(entraTokenProvider()))
                    .catch(() => setError("Microsoft sign-in failed."))
                }
              >
                Sign in with Microsoft
              </button>
            </div>
          )
        ) : (
          <Chat key={me.tenant_id + (me.display_name ?? "")} tokens={tokens} />
        )}
      </main>
    </div>
  );
}
