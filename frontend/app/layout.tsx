import type { Metadata } from 'next';
import Link from 'next/link';

import './globals.css';
import { CommandPalette } from './command-palette';
import { logoutAction } from './actions';
import { apiOrNull } from '@/lib/api';
import { getMe } from '@/lib/session';
import { atLeast, type EventSummary } from '@/lib/types';

export const metadata: Metadata = {
  title: 'Dogfood -- Hackathon Portal',
  description: 'Self-hostable hackathon submission and judging platform.',
};

/**
 * The nav shows fewer links to a visitor. That is a courtesy, not a control:
 * typing the URL of an organizer page still lands on whatever the API decides,
 * which for a participant is a 404.
 */
export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const me = await getMe();

  // Exactly the links the nav below renders, under the same conditions --
  // the palette is a faster way to reach a page already reachable by click,
  // never a second opinion on whether a role may reach it.
  const pages: { href: string; label: string; hint: string }[] = [
    { href: '/gallery', label: 'Gallery', hint: 'Page' },
    { href: '/verify', label: 'Verify a certificate', hint: 'Page' },
    ...(me.role !== 'admin'
      ? [{ href: '/certificate', label: 'Get your certificate', hint: 'Page' }]
      : []),
    ...(me.authenticated ? [{ href: '/dashboard', label: 'Dashboard', hint: 'Page' }] : []),
    ...(atLeast(me.role, 'judge') ? [{ href: '/judging', label: 'Judge console', hint: 'Page' }] : []),
    ...(atLeast(me.role, 'organizer')
      ? [
          { href: '/organizer', label: 'Organize', hint: 'Page' },
          { href: '/organizer/new', label: 'Create event', hint: 'Action' },
        ]
      : []),
    ...(atLeast(me.role, 'admin') ? [{ href: '/admin', label: 'Admin', hint: 'Page' }] : []),
    ...(!me.authenticated
      ? [
          { href: '/login', label: 'Sign in', hint: 'Page' },
          { href: '/register', label: 'Register', hint: 'Page' },
        ]
      : []),
  ];
  const events = (await apiOrNull<EventSummary[]>('/api/events')) ?? [];

  return (
    <html lang="en">
      <body>
        {/* Off-screen until focused. A judge tabbing through the same nav on
            every one of thirty ballot pages should not have to do it again
            each time just to reach the ballot. */}
        <a href="#main-content" className="skip-link">
          Skip to content
        </a>
        <header className="masthead">
          <div className="masthead-inner">
            <Link href="/" className="brand">
              Dog<span>food</span>
            </Link>
            <nav aria-label="Main">
              <Link href="/gallery">Gallery</Link>
              {/* Admin has no self-service certificate of its own -- judging and
                  organizing records for that role are issued the same
                  admin-only way as everything else an admin gets, not
                  self-served through the participant wizard. */}
              {me.role !== 'admin' && <Link href="/certificate">Get your certificate</Link>}
              {me.authenticated && <Link href="/dashboard">Dashboard</Link>}
              {/* A judge on no events still sees the link and an empty queue,
                  which is a better answer than a missing nav item. */}
              {atLeast(me.role, 'judge') && <Link href="/judging">Judge</Link>}
              {atLeast(me.role, 'organizer') && <Link href="/organizer">Organize</Link>}
              {atLeast(me.role, 'admin') && <Link href="/admin">Admin</Link>}
              {me.authenticated ? (
                <form action={logoutAction}>
                  <span className="muted small" style={{ marginRight: 10 }}>
                    {me.user?.display_name} · {me.role}
                  </span>
                  <button className="quiet small" type="submit">
                    Sign out
                  </button>
                </form>
              ) : (
                <>
                  <Link href="/login">Sign in</Link>
                  <Link className="button primary" href="/register">
                    Register
                  </Link>
                </>
              )}
              <CommandPalette pages={pages} events={events} canSignOut={me.authenticated} />
            </nav>
          </div>
        </header>
        <div id="main-content">{children}</div>
      </body>
    </html>
  );
}
