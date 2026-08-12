import createFetchClient, {
  createFinalURL,
  createQuerySerializer,
  defaultPathSerializer
} from "openapi-fetch";
import createQueryClient from "openapi-react-query";
import type { ApiError } from "./contracts";
import type { paths } from "./schema";

export const API_ORIGIN = (import.meta.env.VITE_API_ORIGIN ?? "").replace(
  /\/+$/,
  ""
);

export const apiFetch = createFetchClient<paths>({ baseUrl: API_ORIGIN });
export const $api = createQueryClient(apiFetch);

const querySerializer = createQuerySerializer();

export function artifactUrl(artifactId: string): string {
  return createFinalURL("/api/artifacts/{artifact_id}", {
    baseUrl: API_ORIGIN,
    params: { path: { artifact_id: artifactId } },
    querySerializer,
    pathSerializer: defaultPathSerializer
  });
}

export function runEventStreamUrl(testRunId: string, after: number): string {
  return createFinalURL("/api/runs/{test_run_id}/stream", {
    baseUrl: API_ORIGIN,
    params: {
      path: { test_run_id: testRunId },
      query: { after }
    },
    querySerializer,
    pathSerializer: defaultPathSerializer
  });
}

export function apiErrorMessage(
  error: ApiError | null | undefined,
  fallback: string
): string {
  return error?.message || fallback;
}
