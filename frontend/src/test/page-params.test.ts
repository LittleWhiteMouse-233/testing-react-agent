import { expect, it } from "vitest";
import { isCalendarDate, parsePage } from "../pageParams";

it.each(["2026-02-30", "2026-02-29", "1900-02-29", "2026-04-31", "0000-01-01", "2026-13-01", "2026-1-01", ""])("rejects invalid calendar date %s", (value) => {
  expect(isCalendarDate(value)).toBe(false);
});
it.each(["2024-02-29", "2000-02-29", "2026-09-15"])("accepts real calendar date %s", (value) => {
  expect(isCalendarDate(value)).toBe(true);
});
it.each([null, "", "Infinity", "1.5", "-2", "0", "NaN", "9007199254740991"])("defaults invalid page %s to the first page", (value) => {
  expect(parsePage(value, 12)).toBe(1);
});
it("restores a valid integer page", () => { expect(parsePage("3", 12)).toBe(3); });
