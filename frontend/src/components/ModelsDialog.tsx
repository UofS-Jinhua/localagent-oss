import { useEffect, useState } from "react";
import { api, type ModelSpec, type Preset, type ProbeResult, type ProviderView, type RegistryView,
  type SkillList, type WebView } from "../api";
import { Icon } from "./Icon";

// Manage the model registry: providers (with their API keys), models, roles, quick tests.
// Keys typed here go to ~/.localagent/secrets.env; the server never sends them back.

interface Props {
  onClose: () => void;
  onChanged: () => void;
  workDir: string;
}

const ROLE_HELP: Record<string, string> = {
  main: "does the tasks",
  commit_message: "writes commit messages",
  subagent: "reserved for sub-tasks",
  summarizer: "reserved for summaries",
};

function kilo(n: number): string {
  return n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${Math.round(n / 1000)}K` : String(n);
}

export function ModelsDialog({ onClose, onChanged, workDir }: Props) {
  const [view, setView] = useState<RegistryView | null>(null);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState(false);

  useEffect(() => {
    api.registry().then(setView).catch((e) => setError(String(e.message ?? e)));
  }, []);

  const apply = async (action: () => Promise<RegistryView>): Promise<boolean> => {
    setError("");
    try {
      setView(await action());
      onChanged();
      return true;
    } catch (e: any) {
      setError(String(e.message ?? e));
      return false;
    }
  };

  return (
    <div className="modal-backdrop">
      <div className="modal models-dialog" role="dialog" aria-labelledby="models-title">
        <div className="modal-head">
          <strong id="models-title">Models</strong>
          <span className="dim small">{view?.path}</span>
          <div className="spacer" />
          <button className="icon-button" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
        </div>
        {error && <div className="info tone-error">{error}</div>}
        {!view ? <div className="dim">loading…</div> : (
          <div className="models-body">
            <Roles view={view} apply={apply} />
            <div className="section-title">Providers</div>
            {view.providers.map((p) => (
              <ProviderCard key={p.name} provider={p} view={view} apply={apply} setError={setError} />
            ))}
            {adding ? (
              <ProviderForm presets={view.presets} onCancel={() => setAdding(false)}
                onSave={async (name, body) => (await apply(() => api.putProvider(name, body))) && setAdding(false)} />
            ) : (
              <button onClick={() => setAdding(true)}><Icon name="plus" size={14} /> Add a provider</button>
            )}
            <Web />
            <Skills workDir={workDir} />
          </div>
        )}
      </div>
    </div>
  );
}

// What the model can open for this folder. Read-only on purpose: a skill is a folder of files the
// user (or a repository) owns, and only its one-line description is ever in the prompt.
function Skills({ workDir }: { workDir: string }) {
  const [list, setList] = useState<SkillList | null>(null);

  useEffect(() => {
    if (!workDir) return;
    api.skills(workDir).then(setList).catch(() => setList(null));
  }, [workDir]);

  if (!list) return null;
  return (
    <>
      <div className="section-title">Skills</div>
      {list.skills.length === 0 ? (
        <div className="dim small">
          None yet. A skill is a folder with a SKILL.md in <span className="mono">{list.user_dir}</span> (every
          project) or <span className="mono">{list.project_dir}</span> (this one). Claude Code's format works.
        </div>
      ) : (
        <table className="model-table">
          <thead><tr><th>name</th><th>from</th><th>what it is for</th></tr></thead>
          <tbody>
            {list.skills.map((s) => (
              <tr key={s.name}>
                <td className="mono" title={s.folder}>{s.name}</td>
                <td className="dim small">{s.source}</td>
                <td className="dim small">{s.description || "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="dim small">
        The model sees only these lines; it reads one with the Skill tool when the task needs it. Skills in{" "}
        <span className="mono">{list.user_dir}</span> are copied to the machines you connect to (Machines), so
        tasks there have them too.
      </div>
    </>
  );
}

const SEARCH_HELP: Record<string, string> = {
  duckduckgo: "no key needed; DuckDuckGo may block a machine it takes for a bot",
  searxng: "your own SearXNG instance (enable format json in its settings.yml)",
  brave: "Brave Search API key",
  tavily: "Tavily API key",
  off: "no WebSearch; WebFetch still reads URLs",
};

// WebSearch and WebFetch: whether the agent has them and where search results come from.
function Web() {
  const [view, setView] = useState<WebView | null>(null);
  const [form, setForm] = useState({ search: "duckduckgo", url: "", api_key: "", enabled: true });
  const [status, setStatus] = useState<{ tone: string; text: string } | null>(null);

  const load = (v: WebView) => {
    setView(v);
    setForm({ search: v.search, url: v.url, api_key: "", enabled: v.enabled });
  };
  useEffect(() => { api.web().then(load).catch(() => setView(null)); }, []);
  if (!view) return null;

  const save = async () => {
    setStatus(null);
    try {
      load(await api.putWeb({ ...form, api_key: form.api_key.trim() || undefined }));
      setStatus({ tone: "tone-ok", text: "saved; applies from the next task" });
    } catch (e: any) {
      setStatus({ tone: "tone-error", text: String(e.message ?? e) });
    }
  };
  const test = async () => {
    setStatus({ tone: "", text: "searching…" });
    try {
      const { results } = await api.tryWebSearch("speculative decoding");
      setStatus({ tone: "tone-ok", text: results.length ? `works: ${results[0].title} — ${results[0].url}` : "works, but found nothing" });
    } catch (e: any) {
      setStatus({ tone: "tone-error", text: String(e.message ?? e) });
    }
  };
  const keyed = form.search === "brave" || form.search === "tavily";

  return (
    <>
      <div className="section-title">Web</div>
      <div className="form-grid">
        <label>web tools
          <select value={form.enabled ? "on" : "off"} onChange={(e) => setForm({ ...form, enabled: e.target.value === "on" })}>
            <option value="on">WebSearch and WebFetch</option>
            <option value="off">none (no internet here)</option>
          </select>
        </label>
        <label>search with
          <select value={form.search} disabled={!form.enabled} onChange={(e) => setForm({ ...form, search: e.target.value })}>
            {view.backends.map((b) => <option key={b} value={b}>{b}</option>)}
          </select>
        </label>
        {form.search === "searxng" && (
          <label>SearXNG address<input value={form.url} placeholder="http://127.0.0.1:8888"
            onChange={(e) => setForm({ ...form, url: e.target.value })} /></label>
        )}
        {keyed && (
          <label>API key
            <input type="password" autoComplete="off" value={form.api_key}
              placeholder={view.search === form.search && view.has_key ? "stored — leave empty to keep" : `stored as ${form.search === "brave" ? "BRAVE_API_KEY" : "TAVILY_API_KEY"}`}
              onChange={(e) => setForm({ ...form, api_key: e.target.value })} />
          </label>
        )}
        <div className="dim small wide">{SEARCH_HELP[form.search]}</div>
        <div className="wide row-actions">
          <button onClick={save}>Save</button>
          {view.enabled && view.search !== "off" && <button onClick={test}>Try a search</button>}
        </div>
      </div>
      {status && <div className={`info small ${status.tone}`}>{status.text}</div>}
    </>
  );
}

function Roles({ view, apply }: { view: RegistryView; apply: (a: () => Promise<RegistryView>) => Promise<boolean> }) {
  const names = Array.from(new Set([...view.role_names, ...Object.keys(view.roles)]));
  const set = (role: string, model: string, thinking: boolean | null) =>
    apply(() => api.putRoles({ [role]: model ? { model, thinking } : null }));
  return (
    <div className="roles">
      <div className="section-title">Roles</div>
      {names.map((role) => {
        const current = view.roles[role];
        return (
          <div key={role} className="role-row">
            <span className="role-name">{role}<span className="dim small"> {ROLE_HELP[role] ?? ""}</span></span>
            <select value={current?.model ?? ""} onChange={(e) => set(role, e.target.value, current?.thinking ?? null)}>
              {role !== "main" && <option value="">(not set: uses main)</option>}
              <option value={view.auto_label}>
                whatever the server has loaded{view.auto_model ? ` → ${view.auto_model}` : " (nothing loaded)"}
              </option>
              {view.models.map((m) => <option key={m.name} value={m.name}>{m.name}{m.local ? "" : " · cloud"}</option>)}
            </select>
            <select value={current?.thinking == null ? "" : current.thinking ? "on" : "off"} disabled={!current}
              onChange={(e) => set(role, current!.model, e.target.value === "" ? null : e.target.value === "on")}>
              <option value="">thinking: default</option>
              <option value="on">thinking: on</option>
              <option value="off">thinking: off</option>
            </select>
          </div>
        );
      })}
    </div>
  );
}

function ProviderCard({ provider, view, apply, setError }: {
  provider: ProviderView;
  view: RegistryView;
  apply: (a: () => Promise<RegistryView>) => Promise<boolean>;
  setError: (e: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [found, setFound] = useState<ModelSpec[] | null>(null);
  const [finding, setFinding] = useState(false);
  const [editingModel, setEditingModel] = useState<ModelSpec | null>(null);
  const models = view.models.filter((m) => m.provider === provider.name);

  const discover = async (quiet = false) => {
    setFinding(true);
    if (!quiet) setError("");
    try {
      setFound((await api.discover(provider.name)).models);
    } catch (e: any) {
      if (!quiet) setError(String(e.message ?? e)); // a local server that is off should not shout
    } finally {
      setFinding(false);
    }
  };

  // Listing models is a plain GET on every provider we support (Gemini's native
  // list does not touch the generation quota either), so the list is refreshed as
  // soon as the dialog opens: a newly downloaded local model, or the catalogue of
  // a cloud service you just pasted a key for, is there without clicking anything.
  // A cloud provider with no key yet is not asked -- it could only fail.
  useEffect(() => {
    if (provider.local || provider.has_key) discover(true);
  }, [provider.name, provider.has_key]); // eslint-disable-line react-hooks/exhaustive-deps

  const remove = async () => {
    if (!window.confirm(`Remove provider ${provider.name} and its ${models.length} model(s)?`)) return;
    const forget = provider.has_key && window.confirm(`Also delete the stored API key ${provider.api_key_env}?`);
    await apply(() => api.deleteProvider(provider.name, forget));
  };

  return (
    <div className="provider-card">
      <div className="provider-head">
        <strong>{provider.name}</strong>
        <span className={`chip ${provider.local ? "muted" : "cloud"}`}>{provider.local ? "local" : "cloud"}</span>
        <span className="dim small provider-url" title={provider.base_url}>{provider.base_url}</span>
        {provider.api_key_env && (
          <span className={`chip ${provider.has_key ? "ok" : "warn"}`} title={`key variable ${provider.api_key_env}`}>
            {provider.has_key ? "key set" : "no key"}
          </span>
        )}
        <div className="spacer" />
        <button onClick={() => discover()} disabled={finding}><Icon name="refresh" size={14} /> {finding ? "Looking…" : "Find models"}</button>
        <button onClick={() => setEditing(!editing)}>Edit</button>
        <button className="danger" onClick={remove}><Icon name="trash" size={14} /></button>
      </div>
      {editing && (
        <ProviderForm presets={view.presets} provider={provider} onCancel={() => setEditing(false)}
          onSave={async (name, body) => (await apply(() => api.putProvider(name, body))) && setEditing(false)} />
      )}
      {models.length > 0 && (
        <table className="model-table">
          <thead>
            <tr><th>model</th><th>context</th><th>sees images</th><th title="the agent may move a task to this model with set_model">agent may switch</th><th>thinking</th><th /></tr>
          </thead>
          <tbody>
            {models.map((m) => (
              <ModelRow key={m.name} model={m} apply={apply} onEdit={() => setEditingModel(editingModel?.name === m.name ? null : m)}
                editing={editingModel?.name === m.name} onCancelEdit={() => setEditingModel(null)} view={view}
                loaded={found?.some((f) => f.model_id === m.model_id && f.loaded) ?? false} />
            ))}
          </tbody>
        </table>
      )}
      {found && (
        <div className="discovered">
          <div className="dim small">{found.length} chat model(s) at this provider{provider.local ? "" : " (listing costs no quota)"}</div>
          {found.map((m) => {
            const known = models.find((x) => x.model_id === m.model_id);
            // What the endpoint knows better than the registry: vision, and the context it is loaded with.
            const stale = known && ((m.vision && !known.vision) || (m.context_length && m.context_length !== known.context_length));
            if (known && !stale) return null;
            const save = { ...m, registered: undefined, loaded: undefined, local: undefined };
            return (
              <div key={m.model_id} className="discovered-row">
                <span>{m.model_id}</span>
                <span className="dim small">{kilo(m.context_length)} context{m.vision ? " · sees images" : ""}{m.loaded ? " · loaded" : ""}</span>
                <div className="spacer" />
                {known ? (
                  <button onClick={() => apply(() => api.putModel(known.name, { context_length: m.context_length, vision: m.vision || known.vision }))}>
                    <Icon name="refresh" size={14} /> Update {known.context_length !== m.context_length ? `context to ${kilo(m.context_length)}` : "to sees images"}
                  </button>
                ) : (
                  <button onClick={() => apply(() => api.putModel(m.name, save))}><Icon name="plus" size={14} /> Add</button>
                )}
              </div>
            );
          })}
          {found.every((m) => m.registered) && <div className="dim small">all of them are registered</div>}
        </div>
      )}
    </div>
  );
}

function ModelRow({ model, apply, onEdit, editing, onCancelEdit, view, loaded }: {
  model: ModelSpec;
  apply: (a: () => Promise<RegistryView>) => Promise<boolean>;
  onEdit: () => void;
  editing: boolean;
  onCancelEdit: () => void;
  view: RegistryView;
  /** The server has it in memory right now (asked when the dialog opened). */
  loaded: boolean;
}) {
  const [results, setResults] = useState<ProbeResult[] | null>(null);
  const [testing, setTesting] = useState(false);
  const [testError, setTestError] = useState("");
  const usedBy = Object.entries(view.roles).filter(([, r]) => r.model === model.name).map(([name]) => name);

  const test = async () => {
    const cost = model.vision ? "2 requests" : "1 request";
    if (!model.local && !window.confirm(`Testing ${model.name} sends ${cost} to a cloud service (counts against its quota). Continue?`)) return;
    setTesting(true);
    setTestError("");
    setResults(null);
    try {
      setResults((await api.testModel(model.name)).results);
    } catch (e: any) {
      setTestError(String(e.message ?? e));
    } finally {
      setTesting(false);
    }
  };

  const toggle = (field: "vision" | "switchable") => apply(() => api.putModel(model.name, { [field]: !model[field] }));

  return (
    <>
      <tr>
        <td>
          <div className="model-name">{model.name}{loaded && <span className="chip ok" title="the server has this model loaded">loaded</span>}</div>
          {model.model_id !== model.name && <div className="dim small">{model.model_id}</div>}
          {usedBy.length > 0 && <div className="dim small">role: {usedBy.join(", ")}</div>}
        </td>
        <td>{kilo(model.context_length)}</td>
        <td><input type="checkbox" checked={model.vision} onChange={() => toggle("vision")} aria-label="sees images" /></td>
        <td><input type="checkbox" checked={model.switchable} onChange={() => toggle("switchable")} aria-label="agent may switch" /></td>
        <td>{model.thinking}</td>
        <td className="row-actions">
          <button onClick={test} disabled={testing}>{testing ? "Testing…" : "Test"}</button>
          <button onClick={onEdit}>Edit</button>
          <button className="danger" disabled={usedBy.length > 0} title={usedBy.length ? "used by a role" : "remove"}
            onClick={() => window.confirm(`Remove ${model.name}?`) && apply(() => api.deleteModel(model.name))}>
            <Icon name="trash" size={14} />
          </button>
        </td>
      </tr>
      {(results || testError) && (
        <tr><td colSpan={6}>
          <div className="probe-results">
            {testError && <span className="info tone-error">{testError}</span>}
            {results?.map((r) => (
              <span key={r.name} className={`info ${r.ok ? "tone-ok" : "tone-error"}`}>{r.ok ? "✓" : "✗"} {r.name}: {r.detail}</span>
            ))}
          </div>
        </td></tr>
      )}
      {editing && (
        <tr><td colSpan={6}>
          <ModelForm model={model} onCancel={onCancelEdit}
            onSave={async (body) => (await apply(() => api.putModel(model.name, body))) && onCancelEdit()} />
        </td></tr>
      )}
    </>
  );
}

function ModelForm({ model, onSave, onCancel }: { model: ModelSpec; onSave: (body: Partial<ModelSpec>) => void; onCancel: () => void }) {
  const [form, setForm] = useState({
    model_id: model.model_id, context_length: String(model.context_length), max_output_tokens: String(model.max_output_tokens), max_output_tokens_first: String(model.max_output_tokens_first ?? 0), max_thinking_tokens: String(model.max_thinking_tokens ?? 0), max_subtask_context: String(model.max_subtask_context ?? 0), max_parallel_subtasks: String(model.max_parallel_subtasks ?? 1),
    continue_at: String(model.continue_at), thinking: model.thinking, thinking_off: JSON.stringify(model.thinking_off),
    extra_params: JSON.stringify(model.extra_params ?? {}),
    price_input: String(model.price_input ?? 0), price_output: String(model.price_output ?? 0),
    notes: model.notes,
  });
  const [error, setError] = useState("");
  const field = (key: keyof typeof form) => ({ value: form[key], onChange: (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value }) });

  const save = () => {
    let thinkingOff: Record<string, unknown>;
    let extraParams: Record<string, unknown>;
    try {
      thinkingOff = form.thinking_off.trim() ? JSON.parse(form.thinking_off) : {};
    } catch {
      setError("thinking off must be JSON, e.g. {\"reasoning_effort\": \"none\"}");
      return;
    }
    try {
      extraParams = form.extra_params.trim() ? JSON.parse(form.extra_params) : {};
    } catch {
      setError("request params must be JSON, e.g. {\"temperature\": 1.0, \"top_k\": 20}");
      return;
    }
    onSave({
      model_id: form.model_id.trim(), context_length: Number(form.context_length), max_output_tokens: Number(form.max_output_tokens), max_output_tokens_first: Number(form.max_output_tokens_first) || 0, max_thinking_tokens: Number(form.max_thinking_tokens) || 0, max_subtask_context: Number(form.max_subtask_context) || 0, max_parallel_subtasks: Math.max(1, Number(form.max_parallel_subtasks) || 1),
      continue_at: Number(form.continue_at), thinking: form.thinking as ModelSpec["thinking"], thinking_off: thinkingOff,
      extra_params: extraParams,
      price_input: Number(form.price_input) || 0, price_output: Number(form.price_output) || 0, notes: form.notes,
    });
  };

  return (
    <div className="form-grid">
      <label>model id<input {...field("model_id")} /></label>
      <label>context window (tokens)<input type="number" {...field("context_length")} /></label>
      <label>max output tokens<input type="number" {...field("max_output_tokens")} /></label>
      <label title="Cap for the first step of a session, where the model reads the task and plans the whole job; a thinking model needs more room there. 0 = the same as max output tokens">first step output tokens<input type="number" step="1024" {...field("max_output_tokens_first")} /></label>
      <label title="How far the model may think in one reply before the reply is stopped and the step retried, counted while it streams. The output budget above has to hold the largest file the model writes in one call, which is far more room than thinking needs. 0 = only the output budget bounds it">thinking limit (tokens)<input type="number" step="1000" {...field("max_thinking_tokens")} /></label>
      <label title="Largest window a sub-agent of a task on this model gets. 0 = 32,768, right for a server whose memory holds about one conversation; raise it where the KV cache is much larger">sub-agent context<input type="number" step="1024" {...field("max_subtask_context")} /></label>
      <label title="Read-only sub-agents that may run at the same time. 1 = one after another; more only helps a server that gains throughput from parallel requests, and stay below its slot count">parallel sub-agents<input type="number" min="1" {...field("max_parallel_subtasks")} /></label>
      <label title="share of the window after which a fresh session continues">hand off at<input type="number" step="0.05" {...field("continue_at")} /></label>
      <label>thinking
        <select {...field("thinking")}>
          <option value="toggle">can be switched off</option>
          <option value="always">always thinks</option>
          <option value="never">never thinks</option>
        </select>
      </label>
      <label className="wide" title='Sent with every request to this model: sampling above all. Left empty, the server decides -- and a server that defaults to greedy decoding makes a thinking model repeat itself until it runs out of budget. Qwen3.8 asks for {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0} while thinking. What you leave out is not sent.'>request params (sampling; empty = the server&apos;s own)<input {...field("extra_params")} /></label>
      <label className="wide" title='Merged on top of the params above when thinking is off, so each mode can have its own sampling. Qwen3.8 without thinking: {"reasoning_effort": "none", "temperature": 0.7, "top_p": 0.8, "presence_penalty": 1.5}'>request params to switch thinking off<input {...field("thinking_off")} /></label>
      <label title="USD per million input tokens; leave 0 for a local model">price in ($/M tokens)<input type="number" step="0.01" {...field("price_input")} /></label>
      <label title="USD per million output tokens">price out ($/M tokens)<input type="number" step="0.01" {...field("price_output")} /></label>
      <label className="wide">notes (shown to the agent in set_model: say when to use it)<input {...field("notes")} /></label>
      {error && <div className="info tone-error wide">{error}</div>}
      <div className="modal-actions wide">
        <div className="spacer" />
        <button onClick={onCancel}>Cancel</button>
        <button className="primary" onClick={save}>Save</button>
      </div>
    </div>
  );
}

function ProviderForm({ presets, provider, onSave, onCancel }: {
  presets: Preset[];
  provider?: ProviderView;
  onSave: (name: string, body: Record<string, unknown>) => void;
  onCancel: () => void;
}) {
  const [preset, setPreset] = useState<Preset | null>(null);
  const [form, setForm] = useState({
    name: provider?.name ?? "", base_url: provider?.base_url ?? "", api_key: "", api_key_env: provider?.api_key_env ?? "",
    max_concurrency: String(provider?.max_concurrency ?? 2), where: provider ? (provider.local ? "local" : "cloud") : "auto",
  });
  const field = (key: keyof typeof form) => ({ value: form[key], onChange: (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value }) });

  const choose = (id: string) => {
    const p = presets.find((x) => x.id === id) ?? null;
    setPreset(p);
    if (p) {
      setForm({
        ...form, name: form.name || p.id, base_url: p.base_url, api_key_env: p.api_key_env,
        max_concurrency: String(p.max_concurrency),
      });
    }
  };

  const save = () => {
    const body: Record<string, unknown> = {
      base_url: form.base_url.trim(), api_key_env: form.api_key_env.trim(), max_concurrency: Number(form.max_concurrency) || 1,
      discovery: preset?.discovery ?? provider?.discovery ?? "auto",
    };
    if (form.api_key.trim()) body.api_key = form.api_key.trim();
    if (form.where !== "auto") body.local = form.where === "local";
    else if (provider) body.local = null;
    onSave(form.name.trim(), body);
  };

  return (
    <div className="form-grid provider-form">
      {!provider && (
        <label className="wide">service
          <select value={preset?.id ?? ""} onChange={(e) => choose(e.target.value)}>
            <option value="">choose…</option>
            {presets.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
          </select>
        </label>
      )}
      {preset?.note && <div className="dim small wide">{preset.note}</div>}
      <label>name<input {...field("name")} disabled={!!provider} placeholder="e.g. gemini" /></label>
      <label>base URL<input {...field("base_url")} placeholder="http://host:1234/v1" /></label>
      <label>API key
        <input type="password" autoComplete="off" {...field("api_key")}
          placeholder={provider?.has_key ? "stored — leave empty to keep" : "not needed for local servers"} />
      </label>
      <label>key variable<input {...field("api_key_env")} placeholder="e.g. GEMINI_API_KEY" /></label>
      <label>parallel requests<input type="number" min={1} {...field("max_concurrency")} /></label>
      <label>where it runs
        <select {...field("where")}>
          <option value="auto">guess from the URL</option>
          <option value="local">local (this computer or network)</option>
          <option value="cloud">cloud</option>
        </select>
      </label>
      <div className="dim small wide">
        Keys are saved in ~/.localagent/secrets.env on this computer (models.yaml only names the variable).
        {preset?.key_url && <> Get a key at <span className="mono">{preset.key_url}</span>.</>}
      </div>
      <div className="modal-actions wide">
        <div className="spacer" />
        <button onClick={onCancel}>Cancel</button>
        <button className="primary" onClick={save} disabled={!form.name.trim() || !form.base_url.trim()}>Save</button>
      </div>
    </div>
  );
}
