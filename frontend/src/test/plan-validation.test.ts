import { describe, expect, it } from "vitest";

describe("plan confirmation rules", () => {
  it("requires every assumption to be confirmed", () => {
    const assumptions = ["电视已登录", "网络可用"];
    const confirmed = ["电视已登录"];
    expect(confirmed.length === assumptions.length).toBe(false);
    expect([...confirmed].sort()).not.toEqual([...assumptions].sort());
  });

  it("requires non-blank observable criteria", () => {
    const criteria = ["页面显示版本号", ""];
    expect(criteria.length > 0 && criteria.every((item) => item.trim())).toBe(false);
  });
});

