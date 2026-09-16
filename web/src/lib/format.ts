/** Format a number in the Indian numbering system, e.g. 12,34,567. */
export function formatInr(amount: number | null | undefined, decimals = 0): string {
  return formatMoney(amount, "INR", decimals);
}

/** Money in a run's own currency (2026-09-16: a run is tagged `params.currency`; the
 *  second market prices in USD). INR groups the Indian way with ₹; anything else groups
 *  in thousands with its own sign ($ for USD). */
export function formatMoney(amount: number | null | undefined, currency = "INR", decimals = 0): string {
  if (amount === null || amount === undefined || Number.isNaN(amount)) return "—";
  const neg = amount < 0;
  const fixed = Math.abs(amount).toFixed(decimals);
  const [intPart, decPart] = fixed.split(".");
  let grouped: string;
  if (currency === "INR") {
    const last3 = intPart.slice(-3);
    const rest = intPart.slice(0, -3);
    grouped = rest ? rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",") + "," + last3 : last3;
  } else {
    grouped = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  const out = decPart ? `${grouped}.${decPart}` : grouped;
  const sign = currency === "INR" ? "₹" : currency === "USD" ? "$" : `${currency} `;
  return (neg ? "-" : "") + sign + out;
}

export function pct(value: number | null | undefined, decimals = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${value.toFixed(decimals)}%`;
}
