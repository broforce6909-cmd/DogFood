/**
 * Who is looking at this page.
 *
 * `getMe()` asks the API, because the API is the only thing that knows. The
 * cookie is opaque to the frontend: there is no claim in it to read, and no way
 * to decide from it what a person may do.
 */

import { cookies } from 'next/headers';

import { SESSION_COOKIE, apiOrNull } from './api';
import type { Me } from './types';

const ANONYMOUS: Me = { authenticated: false, user: null, role: 'visitor' };

export async function getMe(): Promise<Me> {
  return (await apiOrNull<Me>('/api/auth/me')) ?? ANONYMOUS;
}

/** Called from the login and register actions once the API has issued a token. */
export async function setSessionCookie(token: string, expiresAt: string): Promise<void> {
  (await cookies()).set({
    name: SESSION_COOKIE,
    value: token,
    httpOnly: true,
    sameSite: 'lax',
    path: '/',
    expires: new Date(expiresAt),
  });
}

export async function clearSessionCookie(): Promise<void> {
  (await cookies()).delete(SESSION_COOKIE);
}
