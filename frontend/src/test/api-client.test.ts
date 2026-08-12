import createFetchClient from "openapi-fetch";
import { describe, expect, it, vi } from "vitest";
import { artifactUrl, runEventStreamUrl } from "../api/client";
import type { paths } from "../api/schema";

describe("OpenAPI client boundary", () => {
  it("encodes path and query parameters in browser resource URLs", () => {
    expect(artifactUrl("artifact/id with spaces")).toBe(
      "/api/artifacts/artifact%2Fid%20with%20spaces"
    );
    expect(runEventStreamUrl("run/id with spaces", 7)).toBe(
      "/api/runs/run%2Fid%20with%20spaces/stream?after=7"
    );
  });

  it("serializes typed path, query and body values through openapi-fetch", async () => {
    const fetchSpy = vi.fn(async (request: Request) =>
      Response.json({ id: "created" }, { status: 201 })
    );
    const client = createFetchClient<paths>({
      baseUrl: "https://example.test",
      fetch: fetchSpy
    });

    await client.GET("/api/runs/{test_run_id}/events", {
      params: {
        path: { test_run_id: "run/id" },
        query: { after: 3 }
      }
    });
    await client.POST("/api/test-cases", {
      body: { name: "Case", source_text: "Check screen" }
    });

    const getRequest = fetchSpy.mock.calls[0]?.[0];
    const postRequest = fetchSpy.mock.calls[1]?.[0];
    expect(getRequest?.url).toBe(
      "https://example.test/api/runs/run%2Fid/events?after=3"
    );
    expect(postRequest?.url).toBe("https://example.test/api/test-cases");
    expect(await postRequest?.json()).toEqual({
      name: "Case",
      source_text: "Check screen"
    });
  });
});
