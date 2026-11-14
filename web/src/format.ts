export function fmtLap(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "–";
  const minutes = Math.floor(ms / 60000);
  const seconds = (ms % 60000) / 1000;
  return minutes > 0 ? `${minutes}:${seconds.toFixed(3).padStart(6, "0")}` : seconds.toFixed(3);
}

/** Signed delta in seconds, e.g. "+0.284" / "−0.051"; positive = slower. */
export function fmtDelta(s: number | null | undefined, digits = 3): string {
  if (s === null || s === undefined || !Number.isFinite(s)) return "–";
  if (Math.abs(s) < 0.5 * 10 ** -digits) return (0).toFixed(digits);
  return `${s > 0 ? "+" : "−"}${Math.abs(s).toFixed(digits)}`;
}

export function deltaClass(s: number | null | undefined): string {
  if (s === null || s === undefined || Math.abs(s) < 0.0005) return "";
  return s > 0 ? "loss" : "gain";
}

export function fmtNum(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "–";
  return v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function fmtDate(utc: string | null | undefined): string {
  if (!utc) return "";
  const d = new Date(utc);
  return Number.isNaN(d.getTime()) ? utc : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
