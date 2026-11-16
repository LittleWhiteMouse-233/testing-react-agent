import { expect, it } from "vitest";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { URL as NodeURL } from "node:url";
import { ModuleKind, transpileModule } from "typescript";
import { isCalendarDate, parsePage, localDateBoundary, localToday } from "../pageParams";

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

it("uses local calendar components and midnight boundaries rather than UTC date slices", () => {
  const now = new Date(2026, 2, 8, 0, 1);
  expect(localToday(now)).toBe("2026-03-08");
  expect(new Date(localDateBoundary("2026-03-08"))).toEqual(new Date(2026, 2, 8));
  expect(new Date(localDateBoundary("2026-03-08", 1))).toEqual(new Date(2026, 2, 9));
  expect(new Date(localDateBoundary("2026-12-31", 1))).toEqual(new Date(2027, 0, 1));
});

it("constructs the next calendar midnight independently when the selected midnight is skipped", () => {
  const source = readFileSync(new NodeURL("../pageParams.ts", import.meta.url), "utf8");
  const javascript = transpileModule(source, { compilerOptions: { module: ModuleKind.ESNext } }).outputText;
  const moduleUrl = "data:text/javascript;base64," + Buffer.from(javascript).toString("base64");
  const result = execFileSync(process.execPath, ["--input-type=module", "-e",
    `const {localDateBoundary} = await import(${JSON.stringify(moduleUrl)}); console.log(JSON.stringify([localDateBoundary("2026-09-06"), localDateBoundary("2026-09-06", 1)]));`
  ], { env: { ...process.env, TZ: "America/Santiago" }, encoding: "utf8" });
  expect(JSON.parse(result)).toEqual(["2026-09-06T04:00:00.000Z", "2026-09-07T03:00:00.000Z"]);
});
