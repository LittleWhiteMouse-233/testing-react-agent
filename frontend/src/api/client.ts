import type { ApiErrorBody } from "./contracts";

const API_ROOT = import.meta.env.VITE_API_ROOT ?? "/api";

export class ApiRequestError extends Error {
  constructor(
    message: string,
    public status: number,
    public body: ApiErrorBody
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers
    }
  });
  const text = await response.text();
  if (!response.ok) {
    let body: ApiErrorBody = {
      code: "http_error",
      message: response.statusText || `Request failed (${response.status})`
    };
    if (text) {
      try {
        body = JSON.parse(text) as ApiErrorBody;
      } catch {
        body = { code: "invalid_error_response", message: text };
      }
    }
    throw new ApiRequestError(body.message, response.status, body);
  }
  return (text ? JSON.parse(text) : undefined) as T;
}

export const apiUrl = (path: string) => `${API_ROOT}${path}`;
