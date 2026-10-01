// Authentication modes:
//  * entra — MSAL (auth code + PKCE). Requests an ACCESS token for the API scope; ID tokens are never sent.
//  * dev   — local development IdP with synthetic users (server refuses this outside local/test).
import { InteractionRequiredAuthError, PublicClientApplication, type AccountInfo } from "@azure/msal-browser";
import { api, type TokenProvider } from "./api";

export type AuthMode = "dev" | "entra";
export const AUTH_MODE: AuthMode = (import.meta.env.VITE_AUTH_MODE as AuthMode | undefined) ?? "dev";

let msal: PublicClientApplication | null = null;

function msalApp(): PublicClientApplication {
  if (!msal) {
    msal = new PublicClientApplication({
      auth: {
        clientId: import.meta.env.VITE_ENTRA_CLIENT_ID as string,
        authority: `https://login.microsoftonline.com/${import.meta.env.VITE_ENTRA_TENANT_ID as string}`,
        redirectUri: window.location.origin,
      },
      cache: { cacheLocation: "sessionStorage" },
    });
  }
  return msal;
}

const apiScopes = (): string[] => [import.meta.env.VITE_API_SCOPE as string];

export async function entraSignIn(): Promise<AccountInfo> {
  const app = msalApp();
  await app.initialize();
  const result = await app.loginPopup({ scopes: apiScopes() });
  app.setActiveAccount(result.account);
  return result.account;
}

export function entraTokenProvider(): TokenProvider {
  return async () => {
    const app = msalApp();
    const account = app.getActiveAccount();
    if (!account) throw new Error("not signed in");
    try {
      const r = await app.acquireTokenSilent({ scopes: apiScopes(), account });
      return r.accessToken; // access token for the API audience — never the ID token
    } catch (e) {
      if (e instanceof InteractionRequiredAuthError) {
        const r = await app.acquireTokenPopup({ scopes: apiScopes(), account });
        return r.accessToken;
      }
      throw e;
    }
  };
}

export function devTokenProvider(username: string): TokenProvider {
  let cached: { token: string; at: number } | null = null;
  return async () => {
    if (!cached || Date.now() - cached.at > 45 * 60 * 1000) {
      const { access_token } = await api.devToken(username);
      cached = { token: access_token, at: Date.now() };
    }
    return cached.token;
  };
}
