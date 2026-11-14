import { useState } from "react";
import { LivePill } from "./components/LivePill";
import { Placeholder } from "./components/Placeholder";
import { LapAnalysis } from "./pages/LapAnalysis";

type PageKey = "dashboard" | "live" | "analysis" | "benchmark" | "engineer" | "history";

const PAGES: { key: PageKey; label: string; stage?: number; blurb?: string }[] = [
  { key: "dashboard", label: "Dashboard", stage: 7, blurb: "Your driving statistics, recent sessions and progress over time." },
  { key: "live", label: "Live Telemetry", stage: 6, blurb: "Inputs, tyres, fuel, ERS and a live delta while you drive." },
  { key: "analysis", label: "Lap Analysis" },
  { key: "benchmark", label: "Driver Benchmark", stage: 4, blurb: "Your lap against real Formula 1 drivers (FastF1, 2025 season)." },
  { key: "engineer", label: "Race Engineer", stage: 5, blurb: "Evidence-based reports and next-lap instructions." },
  { key: "history", label: "Driver History", stage: 7, blurb: "Recurring patterns, strongest and weakest corners, recommendation tracking." },
];

type Theme = "dark" | "light";

function storedTheme(): Theme {
  try {
    return localStorage.getItem("pitwall-theme") === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

/** Apply the theme to the page *before* React re-renders, so charts read the new colours. */
function applyTheme(theme: Theme): Theme {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("pitwall-theme", theme);
  } catch {
    /* storage unavailable: theme still applies for this visit */
  }
  return theme;
}

export function App() {
  const [page, setPage] = useState<PageKey>("analysis");
  const [theme, setTheme] = useState<Theme>(() => applyTheme(storedTheme()));

  const current = PAGES.find((p) => p.key === page)!;
  return (
    <>
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden />
          PITWALL <small>race engineer</small>
        </div>
        <nav className="nav" aria-label="Sections">
          {PAGES.map((p) => (
            <button
              key={p.key}
              aria-current={p.key === page ? "page" : undefined}
              onClick={() => setPage(p.key)}
            >
              {p.label}
            </button>
          ))}
        </nav>
        <div className="topbar-right">
          <LivePill />
          <button
            className="icon-btn"
            onClick={() => setTheme(applyTheme(theme === "dark" ? "light" : "dark"))}
            aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
            title="Theme"
          >
            {theme === "dark" ? "☀" : "☾"}
          </button>
        </div>
      </header>
      <main>
        {page === "analysis" ? (
          <LapAnalysis theme={theme} />
        ) : (
          <Placeholder title={current.label} stage={current.stage!} blurb={current.blurb!} />
        )}
      </main>
    </>
  );
}
