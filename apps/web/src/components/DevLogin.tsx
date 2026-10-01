import { useEffect, useState } from "react";
import { api, type DevUser } from "../api";

export function DevLogin({ onSelect }: { onSelect: (username: string) => void }) {
  const [users, setUsers] = useState<DevUser[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    api
      .devUsers()
      .then((r) => setUsers(r.users))
      .catch(() => setFailed(true));
  }, []);

  if (failed) {
    return (
      <p role="alert" className="error">
        Development sign-in is unavailable. Is the API running locally?
      </p>
    );
  }
  if (!users) {
    return (
      <p role="status" aria-live="polite">
        Loading development users…
      </p>
    );
  }
  return (
    <section className="panel" aria-labelledby="devlogin-title">
      <h2 id="devlogin-title">Sign in as a synthetic user</h2>
      <p className="muted">
        Local development identity provider. These users and tenants are synthetic. This sign-in is disabled outside
        local development.
      </p>
      <ul className="userlist">
        {users.map((u) => (
          <li key={u.username}>
            <button type="button" onClick={() => onSelect(u.username)}>
              <strong>{u.display_name}</strong>
              <span className="muted">{u.description}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
