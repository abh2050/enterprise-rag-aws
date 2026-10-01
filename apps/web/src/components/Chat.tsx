import { useRef, useState, type FormEvent } from "react";
import { api, ApiError, type AnswerResponse, type CitationDetail, type ResponseMode, type TokenProvider } from "../api";
import { Answer } from "./Answer";
import { CitationPanel } from "./CitationPanel";

interface Turn {
  question: string;
  answer: AnswerResponse | null;
  error: string | null;
}

export function Chat({ tokens }: { tokens: TokenProvider }) {
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState<ResponseMode>("standard");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [citation, setCitation] = useState<CitationDetail | null>(null);
  const [citationError, setCitationError] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q || busy) return;
    setBusy(true);
    setQuestion("");
    setTurns((t) => [...t, { question: q, answer: null, error: null }]);
    try {
      const answer = await api.ask(tokens, q, mode, conversationId);
      setConversationId(answer.conversation_id);
      setTurns((t) => t.map((turn, i) => (i === t.length - 1 ? { ...turn, answer } : turn)));
    } catch (err) {
      const msg = err instanceof ApiError ? `${err.message}${err.traceId ? ` (trace ${err.traceId})` : ""}` : "Request failed.";
      setTurns((t) => t.map((turn, i) => (i === t.length - 1 ? { ...turn, error: msg } : turn)));
    } finally {
      setBusy(false);
      inputRef.current?.focus();
    }
  }

  async function openCitation(openUrl: string) {
    setCitationError(null);
    try {
      setCitation(await api.citation(tokens, openUrl));
    } catch (err) {
      setCitation(null);
      setCitationError(err instanceof ApiError ? err.message : "Could not open citation.");
    }
  }

  return (
    <div className="layout">
      <section className="conversation" aria-label="Conversation">
        <ol className="turns">
          {turns.map((turn, i) => (
            <li key={i} className="turn">
              <p className="question">
                <span className="sr-only">Question: </span>
                {turn.question}
              </p>
              {turn.answer ? (
                <Answer answer={turn.answer} tokens={tokens} onOpenCitation={openCitation} />
              ) : turn.error ? (
                <p role="alert" className="error">
                  {turn.error}
                </p>
              ) : (
                <p role="status" aria-live="polite" className="muted">
                  {mode === "high_assurance"
                    ? "Retrieving, validating and reviewing the answer before showing it…"
                    : "Searching authorized documents…"}
                </p>
              )}
            </li>
          ))}
        </ol>
        <form onSubmit={submit} className="ask">
          <label htmlFor="question">Ask a question about documents you have access to</label>
          <textarea
            id="question"
            ref={inputRef}
            value={question}
            maxLength={2000}
            rows={3}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) void submit(e);
            }}
          />
          <div className="row">
            <fieldset className="modes">
              <legend className="sr-only">Response mode</legend>
              <label>
                <input type="radio" name="mode" checked={mode === "standard"} onChange={() => setMode("standard")} />{" "}
                Standard
              </label>
              <label>
                <input
                  type="radio"
                  name="mode"
                  checked={mode === "high_assurance"}
                  onChange={() => setMode("high_assurance")}
                />{" "}
                High assurance (reviewed before display)
              </label>
            </fieldset>
            <button type="submit" className="primary" disabled={busy || !question.trim()} aria-busy={busy}>
              {busy ? "Working…" : "Ask"}
            </button>
          </div>
        </form>
      </section>
      <CitationPanel citation={citation} error={citationError} onClose={() => setCitation(null)} tokens={tokens} />
    </div>
  );
}
