import { useState, useRef, KeyboardEvent } from "react";
import { Plus, Trash2, Edit2, X, CheckCircle, XCircle, Loader2, Zap } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Basket, BasketConsensus, Settings } from "../lib/types";

// ── Constants ─────────────────────────────────────────────────────────────────

const CATEGORIES = [
  "Politics", "Sports", "Crypto", "Economics", "Science", "Culture", "Geopolitics",
] as const;

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtPct(v: number): string {
  return (v * 100).toFixed(0) + "%";
}

// ── Tag input (wallet addresses) ──────────────────────────────────────────────

function WalletTagInput({
  tags,
  onChange,
}: {
  tags: string[];
  onChange: (tags: string[]) => void;
}) {
  const [input, setInput] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  function commit() {
    const val = input.trim();
    if (val && !tags.includes(val)) {
      onChange([...tags, val]);
    }
    setInput("");
  }

  function handleKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter" || e.key === "," || e.key === " ") {
      e.preventDefault();
      commit();
    } else if (e.key === "Backspace" && !input && tags.length > 0) {
      onChange(tags.slice(0, -1));
    }
  }

  return (
    <div
      className="flex flex-wrap gap-1 min-h-[36px] px-2 py-1 rounded border border-zinc-700 bg-zinc-900 cursor-text"
      onClick={() => inputRef.current?.focus()}
    >
      {tags.map((tag) => (
        <span
          key={tag}
          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-zinc-700 text-zinc-200 text-[11px] font-mono"
        >
          {tag.slice(0, 10)}…
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onChange(tags.filter((t) => t !== tag));
            }}
            className="text-zinc-400 hover:text-zinc-100"
          >
            <X className="w-2.5 h-2.5" />
          </button>
        </span>
      ))}
      <input
        ref={inputRef}
        type="text"
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={handleKey}
        onBlur={commit}
        placeholder={tags.length === 0 ? "0x… press Enter to add" : ""}
        className="flex-1 min-w-[140px] bg-transparent text-zinc-300 text-xs outline-none placeholder-zinc-600"
      />
    </div>
  );
}

// ── Consensus check panel ─────────────────────────────────────────────────────

function ConsensusPanel({ basketId, onClose }: { basketId: number; onClose: () => void }) {
  const [conditionId, setConditionId] = useState("");
  const [outcome, setOutcome] = useState("Yes");

  const checkMutation = useMutation<BasketConsensus, Error, void>({
    mutationFn: async () => {
      const r = await fetch(
        `/api/baskets/${basketId}/consensus?condition_id=${encodeURIComponent(conditionId)}&outcome=${encodeURIComponent(outcome)}`
      );
      if (!r.ok) throw new Error(await r.text());
      return r.json();
    },
  });

  return (
    <div className="mt-2 p-3 rounded border border-zinc-700 bg-zinc-900 space-y-3 text-xs">
      <div className="flex items-center justify-between">
        <span className="font-semibold text-zinc-300">Check Consensus</span>
        <button onClick={onClose} className="text-zinc-500 hover:text-zinc-300">
          <X className="w-3.5 h-3.5" />
        </button>
      </div>
      <div className="space-y-2">
        <div>
          <label className="block text-zinc-500 mb-0.5">Condition ID</label>
          <input
            type="text"
            value={conditionId}
            onChange={(e) => setConditionId(e.target.value)}
            placeholder="0x..."
            className="w-full px-2 py-1 rounded border border-zinc-700 bg-zinc-800 text-zinc-200 placeholder-zinc-600 text-xs outline-none focus:border-zinc-500"
          />
        </div>
        <div>
          <label className="block text-zinc-500 mb-0.5">Outcome</label>
          <input
            type="text"
            value={outcome}
            onChange={(e) => setOutcome(e.target.value)}
            placeholder="Yes / No / Team A ..."
            className="w-full px-2 py-1 rounded border border-zinc-700 bg-zinc-800 text-zinc-200 placeholder-zinc-600 text-xs outline-none focus:border-zinc-500"
          />
        </div>
        <button
          type="button"
          onClick={() => checkMutation.mutate()}
          disabled={!conditionId || !outcome || checkMutation.isPending}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded border border-zinc-600 bg-zinc-800 text-zinc-200 hover:border-zinc-400 disabled:opacity-40 text-xs font-medium transition-colors"
        >
          {checkMutation.isPending && <Loader2 className="w-3 h-3 animate-spin" />}
          Check
        </button>
      </div>

      {checkMutation.isError && (
        <div className="text-red-400 text-[11px]">{checkMutation.error?.message}</div>
      )}

      {checkMutation.data && (
        <div
          className={`rounded p-2 border text-[11px] ${
            checkMutation.data.should_copy
              ? "border-emerald-700/50 bg-emerald-900/20"
              : "border-red-700/50 bg-red-900/20"
          }`}
        >
          <div className="flex items-center gap-1.5 mb-1">
            {checkMutation.data.should_copy ? (
              <CheckCircle className="w-3.5 h-3.5 text-emerald-400" />
            ) : (
              <XCircle className="w-3.5 h-3.5 text-red-400" />
            )}
            <span className={`font-semibold ${checkMutation.data.should_copy ? "text-emerald-400" : "text-red-400"}`}>
              {checkMutation.data.should_copy ? "Consensus reached" : "No consensus"}
            </span>
          </div>
          <div className="text-zinc-400 leading-relaxed">{checkMutation.data.reason}</div>
          {checkMutation.data.price_spread > 0 && (
            <div className="text-zinc-500 mt-0.5">
              Price spread: {checkMutation.data.price_spread.toFixed(3)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Create / Edit modal ───────────────────────────────────────────────────────

interface BasketFormData {
  name: string;
  category: string;
  wallet_addresses: string[];
  consensus_threshold: number;
}

function BasketModal({
  initial,
  onSave,
  onClose,
  isSaving,
}: {
  initial: BasketFormData;
  onSave: (data: BasketFormData) => void;
  onClose: () => void;
  isSaving: boolean;
}) {
  const [form, setForm] = useState<BasketFormData>(initial);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="w-full max-w-lg bg-zinc-900 border border-zinc-700 rounded-lg shadow-2xl p-5 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-zinc-100">
            {initial.name ? "Edit Basket" : "New Basket"}
          </h2>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-200">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Name */}
        <div>
          <label className="block text-xs text-zinc-500 mb-1">Name</label>
          <input
            type="text"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="Crypto Alpha"
            className="w-full px-3 py-1.5 rounded border border-zinc-700 bg-zinc-800 text-zinc-200 text-xs outline-none focus:border-zinc-500"
          />
        </div>

        {/* Category */}
        <div>
          <label className="block text-xs text-zinc-500 mb-1">Topic category</label>
          <select
            value={form.category}
            onChange={(e) => setForm({ ...form, category: e.target.value })}
            className="w-full px-3 py-1.5 rounded border border-zinc-700 bg-zinc-800 text-zinc-200 text-xs outline-none focus:border-zinc-500"
          >
            <option value="">— None —</option>
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </div>

        {/* Wallet addresses */}
        <div>
          <label className="block text-xs text-zinc-500 mb-1">
            Wallet addresses ({form.wallet_addresses.length})
          </label>
          <WalletTagInput
            tags={form.wallet_addresses}
            onChange={(tags) => setForm({ ...form, wallet_addresses: tags })}
          />
        </div>

        {/* Consensus threshold */}
        <div>
          <div className="flex items-center justify-between mb-1">
            <label className="text-xs text-zinc-500">Consensus threshold</label>
            <span className="text-xs font-semibold text-zinc-300">
              {fmtPct(form.consensus_threshold)}
            </span>
          </div>
          <input
            type="range"
            min={50}
            max={100}
            step={5}
            value={Math.round(form.consensus_threshold * 100)}
            onChange={(e) =>
              setForm({ ...form, consensus_threshold: Number(e.target.value) / 100 })
            }
            className="w-full accent-emerald-500"
          />
          <div className="flex justify-between text-[10px] text-zinc-600 mt-0.5">
            <span>50%</span>
            <span>100%</span>
          </div>
        </div>

        {/* Actions */}
        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-1.5 text-xs rounded border border-zinc-700 text-zinc-400 hover:text-zinc-200 hover:border-zinc-500 transition-colors"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => onSave(form)}
            disabled={!form.name || isSaving}
            className="px-3 py-1.5 text-xs rounded border border-emerald-700 bg-emerald-700/20 text-emerald-300 hover:bg-emerald-700/40 disabled:opacity-40 font-semibold transition-colors"
          >
            {isSaving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export function BasketManager() {
  const qc = useQueryClient();
  const [editBasket, setEditBasket] = useState<Basket | null | "new">(null);
  const [consensusFor, setConsensusFor] = useState<number | null>(null);

  const { data: baskets = [], isLoading } = useQuery<Basket[]>({
    queryKey: ["baskets"],
    queryFn: () => fetch("/api/baskets").then((r) => r.json()),
    staleTime: 30_000,
  });

  // Current basket_ids from settings
  const { data: settings } = useQuery<Settings>({
    queryKey: ["settings"],
    queryFn: () => fetch("/api/settings").then((r) => r.json()),
    staleTime: 30_000,
  });
  const activeBasketIds: number[] = settings?.copy_trading?.basket_ids ?? [];

  const toggleCopyMutation = useMutation({
    mutationFn: async (basketId: number) => {
      const current = activeBasketIds;
      const next = current.includes(basketId)
        ? current.filter((id) => id !== basketId)
        : [...current, basketId];
      const r = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ copy_trading: { ...(settings?.copy_trading ?? {}), basket_ids: next } }),
      });
      if (!r.ok) throw new Error(await r.text());
      return r.json();
    },
    onSuccess: (data) => qc.setQueryData(["settings"], data),
  });

  const createMutation = useMutation({
    mutationFn: async (data: BasketFormData) => {
      const r = await fetch("/api/baskets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
      if (!r.ok) throw new Error(await r.text());
      return r.json();
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["baskets"] });
      setEditBasket(null);
    },
  });

  const updateMutation = useMutation({
    mutationFn: async ({ id, data }: { id: number; data: BasketFormData }) => {
      const r = await fetch(`/api/baskets/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
      if (!r.ok) throw new Error(await r.text());
      return r.json();
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["baskets"] });
      setEditBasket(null);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: async (id: number) => {
      const r = await fetch(`/api/baskets/${id}`, { method: "DELETE" });
      if (!r.ok) throw new Error(await r.text());
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["baskets"] }),
  });

  function handleSave(data: BasketFormData) {
    if (editBasket === "new") {
      createMutation.mutate(data);
    } else if (editBasket) {
      updateMutation.mutate({ id: editBasket.id, data });
    }
  }

  const isSaving = createMutation.isPending || updateMutation.isPending;

  return (
    <div className="p-4 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-zinc-100">Baskets</h2>
          <p className="text-xs text-zinc-500 mt-0.5">
            Group wallets by topic. Copy only when ≥ threshold agree on the same outcome.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setEditBasket("new")}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded border border-zinc-700 bg-zinc-900 text-zinc-200 text-xs font-semibold hover:border-zinc-500 transition-colors"
        >
          <Plus className="w-3.5 h-3.5" />
          New Basket
        </button>
      </div>

      {/* Loading */}
      {isLoading && (
        <div className="text-zinc-600 text-sm py-8 text-center">Loading baskets…</div>
      )}

      {/* Empty state */}
      {!isLoading && baskets.length === 0 && (
        <div className="text-zinc-600 text-sm py-12 text-center border border-dashed border-zinc-800 rounded">
          No baskets yet. Create one to start grouping wallets by topic.
        </div>
      )}

      {/* Basket list */}
      <div className="space-y-3">
        {baskets.map((basket) => {
          const isCopyActive = activeBasketIds.includes(basket.id);
          return (
          <div
            key={basket.id}
            className={`rounded border p-4 space-y-2 ${isCopyActive ? "border-emerald-700/50 bg-emerald-950/20" : "border-zinc-800 bg-zinc-900/50"}`}
          >
            {/* Row header */}
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2 min-w-0">
                <span className="text-sm font-semibold text-zinc-100 truncate">
                  {basket.name}
                </span>
                {basket.category && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded border border-zinc-700 text-zinc-400 shrink-0">
                    {basket.category}
                  </span>
                )}
                {isCopyActive && (
                  <span className="inline-flex items-center gap-0.5 text-[10px] px-1.5 py-0.5 rounded border border-emerald-600/40 bg-emerald-500/10 text-emerald-400 font-semibold shrink-0">
                    <Zap className="w-2.5 h-2.5" />
                    Active
                  </span>
                )}
              </div>
              <div className="flex items-center gap-1.5 shrink-0 ml-2">
                {/* Copy trading toggle */}
                <button
                  type="button"
                  title={isCopyActive ? "Disable for copy trading" : "Enable for copy trading"}
                  onClick={() => toggleCopyMutation.mutate(basket.id)}
                  disabled={toggleCopyMutation.isPending}
                  className={`px-2 py-1 text-[10px] rounded border font-semibold transition-colors ${
                    isCopyActive
                      ? "border-emerald-600/40 bg-emerald-500/10 text-emerald-400 hover:bg-red-500/10 hover:text-red-400 hover:border-red-600/40"
                      : "border-zinc-700 text-zinc-500 hover:text-emerald-400 hover:border-emerald-600/40"
                  }`}
                >
                  {isCopyActive ? "Disable" : "Enable copy"}
                </button>
                <button
                  type="button"
                  onClick={() =>
                    setConsensusFor(consensusFor === basket.id ? null : basket.id)
                  }
                  className="px-2 py-1 text-[10px] rounded border border-zinc-700 text-zinc-400 hover:text-zinc-200 hover:border-zinc-500 transition-colors"
                >
                  Check Consensus
                </button>
                <button
                  type="button"
                  onClick={() => setEditBasket(basket)}
                  className="p-1.5 rounded hover:bg-zinc-700 text-zinc-500 hover:text-zinc-200 transition-colors"
                >
                  <Edit2 className="w-3.5 h-3.5" />
                </button>
                <button
                  type="button"
                  onClick={() => {
                    if (confirm(`Delete basket "${basket.name}"?`)) {
                      deleteMutation.mutate(basket.id);
                    }
                  }}
                  className="p-1.5 rounded hover:bg-zinc-700 text-zinc-500 hover:text-red-400 transition-colors"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>

            {/* Stats row */}
            <div className="flex items-center gap-4 text-[11px] text-zinc-500">
              <span>{basket.wallet_addresses.length} wallets</span>
              <span>Threshold: {fmtPct(basket.consensus_threshold)}</span>
            </div>

            {/* Wallet address pills */}
            {basket.wallet_addresses.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {basket.wallet_addresses.map((addr) => (
                  <span
                    key={addr}
                    className="text-[10px] px-1.5 py-0.5 rounded bg-zinc-800 text-zinc-400 font-mono"
                  >
                    {addr.slice(0, 10)}…
                  </span>
                ))}
              </div>
            )}

            {/* Consensus panel */}
            {consensusFor === basket.id && (
              <ConsensusPanel
                basketId={basket.id}
                onClose={() => setConsensusFor(null)}
              />
            )}
          </div>
          );
        })}
      </div>

      {/* Create/Edit modal */}
      {editBasket !== null && (
        <BasketModal
          initial={
            editBasket === "new"
              ? { name: "", category: "", wallet_addresses: [], consensus_threshold: 0.8 }
              : {
                  name: editBasket.name,
                  category: editBasket.category,
                  wallet_addresses: editBasket.wallet_addresses,
                  consensus_threshold: editBasket.consensus_threshold,
                }
          }
          onSave={handleSave}
          onClose={() => setEditBasket(null)}
          isSaving={isSaving}
        />
      )}
    </div>
  );
}
