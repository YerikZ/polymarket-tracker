import { useState, useEffect, useRef, KeyboardEvent } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Save, Eye, EyeOff, AlertTriangle, X } from "lucide-react";
import type { Settings } from "../lib/types";

async function fetchSettings(): Promise<Settings> {
  return fetch("/api/settings").then((r) => r.json());
}

async function saveSettings(updates: Partial<Settings>): Promise<Settings> {
  const r = await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: React.ReactNode;
  hint?: string;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label className="text-xs text-zinc-400">{label}</label>
      {children}
      {hint && <p className="text-[10px] text-zinc-600">{hint}</p>}
    </div>
  );
}

function NumInput({
  value,
  onChange,
  step,
}: {
  value: number | undefined;
  onChange: (v: number) => void;
  step?: number | "any";
}) {
  return (
    <input
      type="number"
      value={value ?? ""}
      step={step ?? "any"}
      onChange={(e) => onChange(Number(e.target.value))}
      className="bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-xs text-zinc-100 w-full focus:outline-none focus:border-zinc-500"
    />
  );
}

function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label?: string;
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
        checked ? "bg-emerald-600" : "bg-zinc-700"
      }`}
    >
      <span
        className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform ${
          checked ? "translate-x-4" : "translate-x-0.5"
        }`}
      />
      {label && (
        <span className="sr-only">{label}</span>
      )}
    </button>
  );
}

function SecretInput({
  value,
  onChange,
  placeholder,
}: {
  value: string | undefined;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  const [show, setShow] = useState(false);
  const isConfigured = value === "***";
  return (
    <div className="relative">
      <input
        type={show ? "text" : "password"}
        value={isConfigured ? "" : (value ?? "")}
        placeholder={isConfigured ? "●●●●●●●● (configured — leave blank to keep)" : placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-xs text-zinc-100 w-full pr-8 focus:outline-none focus:border-zinc-500 placeholder:text-zinc-600"
      />
      <button
        type="button"
        onClick={() => setShow(!show)}
        className="absolute right-2 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300"
      >
        {show ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
      </button>
    </div>
  );
}

function TagInput({
  tags,
  onChange,
  placeholder = "Type and press Enter",
}: {
  tags: string[];
  onChange: (tags: string[]) => void;
  placeholder?: string;
}) {
  const [input, setInput] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  function addTag(raw: string) {
    const trimmed = raw.trim().toLowerCase();
    if (trimmed && !tags.includes(trimmed)) {
      onChange([...tags, trimmed]);
    }
    setInput("");
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      addTag(input);
    } else if (e.key === "Backspace" && input === "" && tags.length > 0) {
      onChange(tags.slice(0, -1));
    }
  }

  function removeTag(tag: string) {
    onChange(tags.filter((t) => t !== tag));
  }

  return (
    <div
      className="flex flex-wrap gap-1.5 bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 min-h-[34px] cursor-text focus-within:border-zinc-500 transition-colors"
      onClick={() => inputRef.current?.focus()}
    >
      {tags.map((tag) => (
        <span
          key={tag}
          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-zinc-700 text-zinc-200 text-[11px]"
        >
          {tag}
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); removeTag(tag); }}
            className="text-zinc-400 hover:text-zinc-100 transition-colors"
          >
            <X className="w-2.5 h-2.5" />
          </button>
        </span>
      ))}
      <input
        ref={inputRef}
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={onKeyDown}
        onBlur={() => { if (input) addTag(input); }}
        placeholder={tags.length === 0 ? placeholder : ""}
        className="flex-1 min-w-[120px] bg-transparent text-xs text-zinc-100 outline-none placeholder:text-zinc-600"
      />
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border border-zinc-800 rounded-lg overflow-hidden">
      <div className="px-4 py-2.5 bg-zinc-900 border-b border-zinc-800">
        <h3 className="text-xs font-semibold text-zinc-300 uppercase tracking-wider">{title}</h3>
      </div>
      <div className="p-4 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {children}
      </div>
    </div>
  );
}

export function SettingsForm() {
  const qc = useQueryClient();
  const { data: remote, isLoading } = useQuery<Settings>({
    queryKey: ["settings"],
    queryFn: fetchSettings,
    staleTime: 30_000,
  });

  const [local, setLocal] = useState<Settings>({});
  const [dirty, setDirty] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (remote) {
      setLocal(remote);
      setDirty(false);
    }
  }, [remote]);

  const mutation = useMutation({
    mutationFn: saveSettings,
    onSuccess: (data) => {
      qc.setQueryData(["settings"], data);
      setLocal(data);
      setDirty(false);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    },
  });

  function set<K extends keyof Settings>(key: K, value: Settings[K]) {
    setLocal((prev) => ({ ...prev, [key]: value }));
    setDirty(true);
  }

  function setCt<K extends keyof NonNullable<Settings["copy_trading"]>>(
    key: K,
    value: NonNullable<Settings["copy_trading"]>[K]
  ) {
    setLocal((prev) => ({
      ...prev,
      copy_trading: { ...(prev.copy_trading ?? {}), [key]: value },
    }));
    setDirty(true);
  }

  const ct = local.copy_trading ?? {};
  const sizingMode = ct.sizing_mode ?? "fixed";

  if (isLoading) {
    return <div className="text-zinc-600 text-sm p-8">Loading settings…</div>;
  }

  return (
    <form
      className="flex flex-col gap-4 p-4 max-w-4xl"
      onSubmit={(e) => {
        e.preventDefault();
        mutation.mutate(local);
      }}
    >
      {/* Save bar */}
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-zinc-200">Configuration</h2>
        <div className="flex items-center gap-3">
          {mutation.isError && (
            <span className="text-xs text-red-400">Save failed</span>
          )}
          {saved && <span className="text-xs text-emerald-400">Saved ✓</span>}
          <button
            type="submit"
            disabled={!dirty || mutation.isPending}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30 hover:bg-emerald-500/25 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            <Save className="w-3.5 h-3.5" />
            {mutation.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </div>

      {mutation.isError && (
        <div className="text-xs text-amber-400 bg-amber-500/10 border border-amber-500/20 rounded px-3 py-2">
          If the watcher was running, it will restart automatically with the new config.
        </div>
      )}

      {/* General */}
      <Section title="General">
        <Field label="Wallets to score" hint="Leaderboard wallets scanned and scored before target selection">
          <NumInput value={local.top_n} onChange={(v) => set("top_n", v)} />
        </Field>
        <Field label="Poll interval (s)" hint="Seconds between polling cycles">
          <NumInput value={local.poll_interval} onChange={(v) => set("poll_interval", v)} />
        </Field>
        <Field label="Min position size (USDC)" hint="Ignore signals below this size">
          <NumInput value={local.min_position_usdc} onChange={(v) => set("min_position_usdc", v)} step={0.1} />
        </Field>
        <Field label="Wallet refresh interval (s)">
          <NumInput value={local.wallet_refresh_interval} onChange={(v) => set("wallet_refresh_interval", v)} />
        </Field>
        <Field label="Max signal age (s)" hint="Ignore signals older than this">
          <NumInput value={local.max_signal_age} onChange={(v) => set("max_signal_age", v)} />
        </Field>
        <Field label="Log level">
          <select
            value={local.log_level ?? "INFO"}
            onChange={(e) => set("log_level", e.target.value)}
            className="bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-xs text-zinc-100 w-full focus:outline-none focus:border-zinc-500"
          >
            {["DEBUG", "INFO", "WARNING", "ERROR"].map((l) => (
              <option key={l} value={l}>{l}</option>
            ))}
          </select>
        </Field>
      </Section>

      {/* Copy Trading */}
      <Section title="Copy Trading">
        <Field label="Simulation mode" hint="When ON, no real orders are placed">
          <div className="flex items-center gap-2 pt-1">
            <Toggle
              checked={ct.dry_run ?? true}
              onChange={(v) => setCt("dry_run", v)}
            />
            <span className={`text-xs ${ct.dry_run !== false ? "text-amber-400" : "text-emerald-400"}`}>
              {ct.dry_run !== false ? "ON (simulated)" : "OFF (live orders)"}
            </span>
          </div>
        </Field>

        <Field label="Sizing mode">
          <select
            value={sizingMode}
            onChange={(e) => setCt("sizing_mode", e.target.value)}
            className="bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-xs text-zinc-100 w-full focus:outline-none focus:border-zinc-500"
          >
            <option value="fixed">Fixed USDC</option>
            <option value="pct_balance">% of balance</option>
            <option value="mirror_pct">% of signal size</option>
          </select>
        </Field>

        {sizingMode === "fixed" && (
          <Field label="Fixed USDC" hint="Baseline spend per trade">
            <NumInput value={ct.fixed_usdc} onChange={(v) => setCt("fixed_usdc", v)} step={0.5} />
          </Field>
        )}
        {sizingMode === "fixed" && (
          <Field label="Reference trade USDC" hint="Reference signal size for scaling">
            <NumInput value={ct.reference_trade_usdc} onChange={(v) => setCt("reference_trade_usdc", v)} step={1} />
          </Field>
        )}
        {sizingMode === "pct_balance" && (
          <Field label="% of balance (0–1)" hint="e.g. 0.02 = 2%">
            <NumInput value={ct.pct_balance} onChange={(v) => setCt("pct_balance", v)} step={0.005} />
          </Field>
        )}
        {sizingMode === "mirror_pct" && (
          <Field label="Mirror % (0–1)" hint="e.g. 0.01 = 1% of original trade">
            <NumInput value={ct.mirror_pct} onChange={(v) => setCt("mirror_pct", v)} step={0.005} />
          </Field>
        )}

        <Field label="Max trade (USDC)" hint="Hard cap per order">
          <NumInput value={ct.max_trade_usdc} onChange={(v) => setCt("max_trade_usdc", v)} />
        </Field>
        <Field label="Daily limit (USDC)" hint="Total cap for today">
          <NumInput value={ct.daily_limit_usdc} onChange={(v) => setCt("daily_limit_usdc", v)} />
        </Field>
        <Field label="Slippage" hint="Added to price for better fill">
          <NumInput value={ct.slippage} onChange={(v) => setCt("slippage", v)} step={0.005} />
        </Field>
        <Field label="Max price" hint="Skip BUY if price ≥ this value (0 = disabled). Default 0.85 skips markets already at ≥85¢.">
          <NumInput value={ct.max_price} onChange={(v) => setCt("max_price", v)} step={0.01} />
        </Field>
        <Field label="Min score (0–100)" hint="Targets still need to pass this score gate">
          <NumInput value={ct.min_score} onChange={(v) => setCt("min_score", v)} />
        </Field>
        <Field label="Manual target wallets" hint="Wallet addresses to copy. Leave empty to disable copy trading (scanning still runs).">
          <TagInput
            tags={ct.manual_target_wallets ?? []}
            onChange={(tags) => setCt("manual_target_wallets", tags)}
            placeholder="0xabc..., 0xdef..."
          />
        </Field>
        <Field label="Scale by wallet tier">
          <div className="flex items-center gap-2 pt-1">
            <Toggle
              checked={ct.score_scale_size ?? true}
              onChange={(v) => setCt("score_scale_size", v)}
            />
            <span className="text-xs text-zinc-400">
              {ct.score_scale_size !== false ? "Enabled" : "Disabled"}
            </span>
          </div>
        </Field>
        <Field label="Enable top-up" hint="When an active target buys the same market again, add to your existing position">
          <div className="flex items-center gap-2 pt-1">
            <Toggle
              checked={ct.enable_topup ?? false}
              onChange={(v) => setCt("enable_topup", v)}
            />
            <span className="text-xs text-zinc-400">
              {ct.enable_topup ? "Enabled" : "Disabled"}
            </span>
          </div>
        </Field>
        {ct.enable_topup && (
          <>
            <Field label="Max top-ups per market" hint="0 = unlimited repeated buys for the same market">
              <NumInput value={ct.max_topups ?? 2} onChange={(v) => setCt("max_topups", v)} step={1} />
            </Field>
            <Field label="Top-up size multiplier" hint="1.0 = same size each round · 0.5 = halve each additional buy">
              <NumInput value={ct.topup_size_multiplier ?? 1.0} onChange={(v) => setCt("topup_size_multiplier", v)} step={0.1} />
            </Field>
          </>
        )}
        <Field label="Blocked keywords" hint="Enter to add · Backspace to remove last">
          <TagInput
            tags={ct.blocked_keywords ?? []}
            onChange={(tags) => setCt("blocked_keywords", tags)}
            placeholder="bitcoin, ethereum…"
          />
        </Field>
        <Field
          label="Stop-loss % (0–1)"
          hint="Exit if price drops this % below entry price. 0.40 = sell when price falls 40% from entry. 0 = disabled."
        >
          <NumInput value={ct.stop_loss_pct} onChange={(v) => setCt("stop_loss_pct", v)} step={0.05} />
        </Field>
        <Field
          label="Trailing stop % (0–1)"
          hint="Exit if price retreats this % from its peak. 0.30 = sell when price falls 30% below its all-time high. 0 = disabled."
        >
          <NumInput value={ct.trailing_stop_pct} onChange={(v) => setCt("trailing_stop_pct", v)} step={0.05} />
        </Field>
        <Field
          label="Trailing stop min gain (×)"
          hint="Only arm trailing stop once price reaches this multiple of entry. 2.0 = only trail after price doubles. Prevents noise triggers near entry."
        >
          <NumInput value={ct.trailing_stop_min_gain} onChange={(v) => setCt("trailing_stop_min_gain", v)} step={0.5} />
        </Field>
        <Field label="Position check interval (s)" hint="How often to scan open positions for stop-loss / trailing-stop conditions">
          <NumInput value={ct.position_check_interval} onChange={(v) => setCt("position_check_interval", v)} step={10} />
        </Field>
      </Section>

      {/* Credentials */}
      <Section title="Credentials">
        <div className="sm:col-span-2 lg:col-span-3">
          <div className="flex items-start gap-2 text-xs text-amber-400 bg-amber-500/10 border border-amber-500/20 rounded px-3 py-2 mb-4">
            <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
            <span>Credentials are stored locally in your PostgreSQL database. Keep this instance private.</span>
          </div>
        </div>
        <Field label="Private key" hint="Leave blank to keep existing value">
          <SecretInput
            value={ct.private_key}
            onChange={(v) => setCt("private_key", v)}
            placeholder="0x…"
          />
        </Field>
        <Field label="Funder (proxy wallet address)">
          <input
            type="text"
            value={ct.funder ?? ""}
            onChange={(e) => setCt("funder", e.target.value)}
            placeholder="0x…"
            className="bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-xs text-zinc-100 w-full focus:outline-none focus:border-zinc-500 placeholder:text-zinc-600"
          />
        </Field>
        <Field
          label="Signature type"
          hint="1 = POLY_PROXY (standard polymarket.com accounts) · 0 = EOA · 2 = POLY_GNOSIS_SAFE"
        >
          <select
            value={ct.signature_type ?? 1}
            onChange={(e) => setCt("signature_type", Number(e.target.value))}
            className="bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-xs text-zinc-100 w-full focus:outline-none focus:border-zinc-500"
          >
            <option value={0}>0 — EOA (direct wallet)</option>
            <option value={1}>1 — POLY_PROXY (polymarket.com default)</option>
            <option value={2}>2 — POLY_GNOSIS_SAFE</option>
          </select>
        </Field>
        <Field label="Polygon WSS (Alchemy)" hint="Leave blank to keep existing">
          <SecretInput
            value={local.polygon_wss}
            onChange={(v) => set("polygon_wss", v)}
            placeholder="wss://polygon-mainnet.g.alchemy.com/v2/…"
          />
        </Field>
        <Field label="Proxy server" hint="SOCKS5 or HTTP proxy — e.g. socks5h://us6354.nordvpn.com:1080">
          <input
            type="text"
            value={local.proxy_url ?? ""}
            onChange={(e) => set("proxy_url", e.target.value)}
            placeholder="socks5h://hostname:1080"
            className="bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-xs text-zinc-100 w-full focus:outline-none focus:border-zinc-500 placeholder:text-zinc-600"
          />
        </Field>
        <Field label="Proxy username" hint="NordVPN service username (e.g. ab12345678)">
          <input
            type="text"
            value={local.proxy_username ?? ""}
            onChange={(e) => set("proxy_username", e.target.value)}
            placeholder="ab12345678"
            className="bg-zinc-800 border border-zinc-700 rounded px-2 py-1.5 text-xs text-zinc-100 w-full focus:outline-none focus:border-zinc-500 placeholder:text-zinc-600"
          />
        </Field>
        <Field label="Proxy password" hint="Leave blank to keep existing value">
          <SecretInput
            value={local.proxy_password}
            onChange={(v) => set("proxy_password", v)}
            placeholder="NordVPN service password"
          />
        </Field>
      </Section>
    </form>
  );
}
