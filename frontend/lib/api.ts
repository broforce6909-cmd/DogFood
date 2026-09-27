/**
 * Backend access.
 *
 * Two base URLs, deliberately:
 *   API_INTERNAL_BASE     server-side fetches inside the compose network
 *   NEXT_PUBLIC_API_BASE  the address a browser would use, shown in the UI
 *
 * Every call in this app goes through the server. The session token is read
 * from an HttpOnly cookie and forwarded as a bearer header, so it is never
 * readable from client-side JavaScript -- which is the whole reason the pages
 * that need a session are server components.
 *
 * Nothing here makes an authorization decision. The API is the only place that
 * does; this module just carries the credential and reports what came back.
 */

import { cookies } from 'next/headers';

export const INTERNAL_BASE = process.env.API_INTERNAL_BASE ?? 'http://localhost:8000';
export const PUBLIC_BASE = process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000';

export const SESSION_COOKIE = 'dogfood_session';

/**
 * The ballot identity for a voter with no account.
 *
 * Held on *this* origin and forwarded as `X-Voter-Token`, because the browser
 * never talks to the API directly and so never receives the API's own cookie.
 * Opaque either way: the value is a random token whose HMAC is what the database
 * stores.
 */
export const VOTER_COOKIE = 'dogfood_voter';

export type Health = { api: string; database?: string; detail?: string };

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: unknown,
  ) {
    super(typeof detail === 'string' ? detail : `HTTP ${status}`);
  }

  /** A 422 from the submit endpoint carries the whole list of what is missing. */
  get missing(): string[] {
    const detail = this.detail as { missing?: string[] } | undefined;
    return Array.isArray(detail?.missing) ? detail!.missing! : [];
  }

  get text(): string {
    const d = this.detail as string | { message?: string } | { msg?: string }[] | undefined;
    if (typeof d === 'string') return d;
    if (d && !Array.isArray(d) && d.message) return d.message;
    if (Array.isArray(d) && d[0]?.msg) return d.map((e) => e.msg).join('; ');
    return `Request failed (${this.status})`;
  }
}

async function authHeaders(): Promise<Record<string, string>> {
  const jar = await cookies();
  const headers: Record<string, string> = {};
  const session = jar.get(SESSION_COOKIE)?.value;
  if (session) headers.Authorization = `Bearer ${session}`;
  // An anonymous voter has a ballot token and no session. Both may be present at
  // once -- an account holder can also hold a token-identified ballot from before
  // they signed in -- and the API prefers the account.
  const ballot = jar.get(VOTER_COOKIE)?.value;
  if (ballot) headers['X-Voter-Token'] = ballot;
  return headers;
}

type Options = { method?: string; body?: unknown; cache?: RequestCache };

/** Throws `ApiError` on any non-2xx. */
export async function api<T>(path: string, options: Options = {}): Promise<T> {
  const { method = 'GET', body, cache = 'no-store' } = options;
  const response = await fetch(`${INTERNAL_BASE}${path}`, {
    method,
    cache,
    headers: {
      ...(await authHeaders()),
      ...(body === undefined ? {} : { 'content-type': 'application/json' }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (response.status === 204) return undefined as T;

  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(response.status, (payload as { detail?: unknown })?.detail ?? payload);
  }
  return payload as T;
}

/**
 * `null` instead of a throw when the caller is not entitled to the resource.
 *
 * Used by pages that render differently for a visitor than for a member -- the
 * 404 the API returns for somebody else's draft is an answer, not a failure.
 */
export async function apiOrNull<T>(path: string, options: Options = {}): Promise<T | null> {
  try {
    return await api<T>(path, options);
  } catch (error) {
    if (error instanceof ApiError && [401, 403, 404].includes(error.status)) return null;
    throw error;
  }
}

/**
 * POST a raw body with an explicit content type.
 *
 * The CSV importer takes `text/csv` rather than a multipart wrapper, so that the
 * same endpoint works from `curl --data-binary @file.csv`. `api()` always sends
 * JSON, so this is its sibling rather than another parameter on it.
 */
export async function apiRaw<T>(
  path: string,
  body: string,
  contentType = 'text/csv',
): Promise<T> {
  const response = await fetch(`${INTERNAL_BASE}${path}`, {
    method: 'POST',
    cache: 'no-store',
    headers: { ...(await authHeaders()), 'content-type': contentType },
    body,
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(response.status, (payload as { detail?: unknown })?.detail ?? payload);
  }
  return payload as T;
}

export async function apiHealth(): Promise<Health> {
  try {
    const response = await fetch(`${INTERNAL_BASE}/health`, { cache: 'no-store' });
    if (!response.ok) return { api: 'error', database: 'unknown', detail: `HTTP ${response.status}` };
    return (await response.json()) as Health;
  } catch (error) {
    return {
      api: 'unreachable',
      database: 'unknown',
      detail: error instanceof Error ? error.message : String(error),
    };
  }
}
