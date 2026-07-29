const API_ROOT = import.meta.env.VITE_API_ROOT ?? "/api";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public detail: unknown
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers
    }
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => response.statusText);
    const message =
      typeof detail?.detail === "string"
        ? detail.detail
        : detail?.detail?.message ?? `Request failed (${response.status})`;
    throw new ApiError(message, response.status, detail);
  }
  return response.json();
}

export const apiUrl = (path: string) => `${API_ROOT}${path}`;

