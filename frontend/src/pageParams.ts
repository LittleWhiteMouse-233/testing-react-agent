/** URL boundaries shared by the case library and history pages. */
export function parsePage(value: string | null, pageSize: number): number {
  const page = Number(value);
  return Number.isSafeInteger(page) && page > 0 && Number.isSafeInteger((page - 1) * pageSize) ? page : 1;
}

export function isCalendarDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith("0000-")) return false;
  const timestamp = Date.parse(value + "T00:00:00Z");
  return Number.isFinite(timestamp) && new Date(timestamp).toISOString().slice(0, 10) === value;
}
