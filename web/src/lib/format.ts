/** Format a number in the Indian numbering system, e.g. 12,34,567. */
export function formatInr(amount: number | null | undefined, decimals = 0): string {
  if (amount === null || amount === undefined || Number.isNaN(amount)) return "—";
  const neg = amount < 0;
  const fixed = Math.abs(amount).toFixed(decimals);
  const [intPart, decPart] = fixed.split(".");
  const last3 = intPart.slice(-3);
  const rest = intPart.slice(0, -3);
  const grouped = rest ? rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",") + "," + last3 : last3;
  const out = decPart ? `${grouped}.${decPart}` : grouped;
  return (neg ? "-₹" : "₹") + out;
}

export function pct(value: number | null | undefined, decimals = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${value.toFixed(decimals)}%`;
}
