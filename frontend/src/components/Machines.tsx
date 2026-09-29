import { useEffect, useRef, useState } from "react";
import { api, type RemoteInfo, type RemoteRun, type RemotesView, type SshPrompt } from "../api";
import { Icon } from "./Icon";

// Other machines, reached over ssh. This computer's app runs ssh and keeps the tunnel up;
// picking a machine logs in (ssh's password questions come up here) and then shows that
// machine's own LocalAgent UI in this window. A password goes from the dialog to the waiting
// ssh process and nowhere else: it is not stored, logged or kept in the page.

export const STATE_LABEL: Record<RemoteInfo["state"], string> = {
  disconnected: "not connected",
  connecting: "connecting…",
  connected: "connected",
  failed: "could not connect",
};

/** ssh is waiting for an answer while logging in. The answer is sent once and the field cleared. */
export function SshPromptDialog({ prompt, onDone }: { prompt: SshPrompt; onDone: (answer: string | null) => void }) {
  const field = useRef<HTMLInputElement>(null);
  const [sending, setSending] = useState(false);

  const send = (answer: string | null) => {
    setSending(true);
    if (field.current) field.current.value = ""; // the page keeps no copy
    onDone(answer);
  };

  return (
    <div className="modal-backdrop">
      <div className="modal question" role="dialog" aria-labelledby="ssh-title">
        <div className="modal-head">
          <Icon name="server" />
          <strong id="ssh-title">Logging in to {prompt.remote}</strong>
        </div>
        {prompt.kind === "confirm" ? (
          <>
            <pre className="command ssh-question">{prompt.prompt}</pre>
            <p className="dim small">
              ssh has not seen this machine before. Only continue if the fingerprint matches the one you expect;
              a key that has <em>changed</em> is refused outright, without a question.
            </p>
            <div className="modal-actions">
              <div className="spacer" />
              <button onClick={() => send("no")} disabled={sending} autoFocus>No</button>
              <button className="primary" onClick={() => send("yes")} disabled={sending}>Yes, connect</button>
            </div>
          </>
        ) : (
          <form onSubmit={(e) => { e.preventDefault(); send(field.current?.value ?? ""); }}>
            <label className="ssh-secret">
              <span>{prompt.prompt}</span>
              <input ref={field} type="password" autoComplete="off" spellCheck={false} autoFocus disabled={sending} />
            </label>
            <p className="dim small">
              Sent to ssh on this computer, which carries it encrypted to the machine. Not stored or logged.
            </p>
            <div className="modal-actions">
              <div className="spacer" />
              <button type="button" onClick={() => send(null)} disabled={sending}>Cancel</button>
              <button type="submit" className="primary" disabled={sending}>Log in</button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

/** Add, edit, connect and remove machines. */
export function MachinesDialog({ view, onView, onClose }: {
  view: RemotesView;
  onView: (view: RemotesView) => void;
  onClose: () => void;
}) {
  const [editing, setEditing] = useState<RemoteInfo | "new" | null>(view.remotes.length ? null : "new");
  const [error, setError] = useState("");
  // What the last named command printed. Its ssh may ask for a password: the dialog appears as usual.
  const [ran, setRan] = useState<{ machine: string; name: string; pending?: string; result?: RemoteRun } | null>(null);

  const runCommand = async (machine: string, name: string) => {
    setError("");
    setRan({ machine, name, pending: `${machine}/${name}` });
    try {
      setRan({ machine, name, result: await api.runRemote(machine, name) });
    } catch (e: any) {
      setRan(null);
      setError(String(e.message ?? e));
    }
  };

  const apply = async (action: () => Promise<RemotesView>): Promise<boolean> => {
    setError("");
    try {
      onView(await action());
      return true;
    } catch (e: any) {
      setError(String(e.message ?? e));
      return false;
    }
  };

  return (
    <div className="modal-backdrop">
      <div className="modal models-dialog" role="dialog" aria-labelledby="machines-title">
        <div className="modal-head">
          <strong id="machines-title">Machines</strong>
          <span className="dim small">reached with your own ssh and ~/.ssh/config (ProxyJump and keys work as usual)</span>
          <div className="spacer" />
          <button className="icon-button" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
        </div>
        {error && <div className="info tone-error">{error}</div>}
        {!view.askpass_installed && (
          <div className="info tone-error">
            la-askpass is missing, so ssh cannot ask for a password here. Run setup again (setup.cmd or scripts/setup.sh).
          </div>
        )}
        <div className="models-body">
          {view.remotes.length > 0 && (
            <table className="model-table">
              <thead><tr><th>machine</th><th>ssh host</th><th>ports</th><th>status</th><th>commands</th><th /></tr></thead>
              <tbody>
                {view.remotes.map((m) => (
                  <tr key={m.name}>
                    <td className="mono">{m.name}</td>
                    <td className="mono">{m.host}</td>
                    <td className="mono small">{m.forwards.join(", ")}</td>
                    <td className={`small ${m.state === "failed" ? "warn-text" : "dim"}`} title={m.detail}>
                      {STATE_LABEL[m.state]}{m.detail ? ` — ${m.detail}` : ""}
                      {m.state === "connected" && m.skills && (
                        <span className={m.skills.startsWith("skills not") ? "warn-text" : ""}> · {m.skills}</span>
                      )}
                    </td>
                    <td className="row-actions">
                      {Object.keys(m.commands ?? {}).map((name) => (
                        <button key={name} disabled={ran?.pending === `${m.name}/${name}`} title={m.commands[name]}
                          onClick={() => runCommand(m.name, name)}>
                          {name}
                        </button>
                      ))}
                    </td>
                    <td className="row-actions">
                      {m.state === "connected" || m.state === "connecting" ? (
                        <button onClick={() => apply(() => api.disconnectRemote(m.name))}>Disconnect</button>
                      ) : (
                        <button onClick={() => apply(() => api.connectRemote(m.name))}>Connect</button>
                      )}
                      <button onClick={() => setEditing(m)}>Edit</button>
                      <button className="danger" onClick={() => window.confirm(`Remove ${m.name}?`) && apply(() => api.deleteRemote(m.name))}>
                        <Icon name="trash" size={14} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {ran && (
            <div className="info small">
              <strong>{ran.machine} · {ran.name}</strong>
              {ran.pending ? " — running…" : ran.result?.exit_code ? ` — exited ${ran.result.exit_code}` : " — done"}
              {ran.result?.output && <pre className="tool-output">{ran.result.output}</pre>}
            </div>
          )}
          {editing ? (
            <MachineForm hosts={view.ssh_hosts} machine={editing === "new" ? null : editing} onCancel={() => setEditing(null)}
              onSave={async (name, body) => (await apply(() => api.putRemote(name, body))) && setEditing(null)} />
          ) : (
            <button onClick={() => setEditing("new")}><Icon name="plus" size={14} /> Add a machine</button>
          )}
        </div>
      </div>
    </div>
  );
}

function MachineForm({ hosts, machine, onSave, onCancel }: {
  hosts: string[];
  machine: RemoteInfo | null;
  onSave: (name: string, body: { host: string; forwards: string[]; ui: string; commands: Record<string, string> }) => void;
  onCancel: () => void;
}) {
  const [host, setHost] = useState(machine?.host ?? hosts[0] ?? "");
  const [name, setName] = useState(machine?.name ?? "");
  const [ports, setPorts] = useState(machine?.forwards.join(", ") ?? "8765");
  const [ui, setUi] = useState(machine?.ui ?? "");
  const [commands, setCommands] = useState(
    Object.entries(machine?.commands ?? {}).map(([k, v]) => `${k} = ${v}`).join("\n"));
  useEffect(() => { if (!machine && !name) setName(host); }, [host]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="provider-form">
      <label>ssh host
        <input list="ssh-hosts" value={host} onChange={(e) => setHost(e.target.value)} placeholder="an alias from ~/.ssh/config" />
        <datalist id="ssh-hosts">{hosts.map((h) => <option key={h} value={h} />)}</datalist>
      </label>
      <label>name<input value={name} onChange={(e) => setName(e.target.value)} disabled={!!machine} /></label>
      <label className="wide" title="Each port here reaches the same port on 127.0.0.1 there; write local:remote to use another port here">
        ports to forward<input value={ports} onChange={(e) => setPorts(e.target.value)} placeholder="8765, 18080" />
      </label>
      <label className="wide" title="Starts LocalAgent's UI there if needed and prints its address, e.g. a script around `la ui --browser --no-open --port 8765`">
        UI command there (optional)<input value={ui} onChange={(e) => setUi(e.target.value)} placeholder="/path/to/bin/la-ui" />
      </label>
      <label className="wide" title="One per line: a name, '=', and the command to run there over ssh">
        commands (optional)
        <textarea rows={3} value={commands} onChange={(e) => setCommands(e.target.value)}
          placeholder={"start = bin/la-services start\nstop = bin/la-services stop\nstatus = bin/la-services status"} />
      </label>
      <div className="dim small wide">
        Each command becomes a button here, run over its own ssh. Handy for starting and stopping what the
        machine runs (model server, search, its UI) without opening a terminal.
      </div>
      <div className="dim small wide">
        With a UI command, choosing the machine opens its LocalAgent in this window. Without one, the ports are forwarded
        only — for example a model server there that this computer's models use.
      </div>
      <div className="modal-actions wide">
        <div className="spacer" />
        <button onClick={onCancel}>Cancel</button>
        <button className="primary" disabled={!host.trim() || !name.trim()}
          onClick={() => onSave(name.trim(), { host: host.trim(), ui: ui.trim(),
            forwards: ports.split(/[,\s]+/).map((p) => p.trim()).filter(Boolean),
            commands: Object.fromEntries(commands.split("\n").map((line) => {
              const at = line.indexOf("=");
              return at === -1 ? ["", ""] : [line.slice(0, at).trim(), line.slice(at + 1).trim()];
            }).filter(([k, v]) => k && v)) })}>
          Save
        </button>
      </div>
    </div>
  );
}
