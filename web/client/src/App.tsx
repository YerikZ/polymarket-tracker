import { useState } from "react";
import { StatusBar } from "./components/StatusBar";
import { SignalFeed } from "./components/SignalFeed";
import { WalletTable } from "./components/WalletTable";
import { PositionsTable } from "./components/PositionsTable";
import { SettingsForm } from "./components/SettingsForm";

type Tab = "signals" | "wallets" | "pnl" | "settings";

const TABS: { id: Tab; label: string }[] = [
  { id: "signals", label: "Signals" },
  { id: "wallets", label: "Wallets" },
  { id: "pnl", label: "PnL" },
  { id: "settings", label: "Settings" },
];

export default function App() {
  const [tab, setTab] = useState<Tab>("signals");

  return (
    <div className="flex flex-col h-screen overflow-hidden">
      <StatusBar />

      {/* Tab nav */}
      <nav className="flex border-b border-zinc-800 bg-zinc-950 px-4">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-4 py-2.5 text-xs font-semibold transition-colors border-b-2 -mb-px ${
              tab === t.id
                ? "border-emerald-400 text-emerald-400"
                : "border-transparent text-zinc-500 hover:text-zinc-300"
            }`}
          >
            {t.label}
          </button>
        ))}
      </nav>

      {/* Tab content */}
      <main className="flex-1 overflow-auto">
        {tab === "signals" && <SignalFeed />}
        {tab === "wallets" && <WalletTable />}
        {tab === "pnl" && <PositionsTable />}
        {tab === "settings" && <SettingsForm />}
      </main>
    </div>
  );
}
