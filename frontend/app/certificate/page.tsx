import Link from 'next/link';

import { Empty } from '../components';
import { api, ApiError, PUBLIC_BASE } from '@/lib/api';
import { getMe } from '@/lib/session';
import type { CertificateLookupRow, GalleryPage } from '@/lib/types';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Get your certificate -- Dogfood' };

/**
 * A three-step wizard, entirely GET-driven query params -- the same pattern
 * every other multi-step flow in this app uses (the gallery's own search and
 * filter, the pairwise judging screen): no client-side state, so a step is a
 * URL a person can bookmark, refresh, or send to someone else mid-flow.
 *
 *   1. find your project    -- ?q=...
 *   2. confirm your name    -- ?submission=<id>          (a real name, never free text)
 *   3. preview and download -- ?submission=<id>&code=<code>
 *
 * Step 2 exists at all because a certificate names a person, and this page
 * has no free-text way to read that name safely. `GET /api/submissions/{id}/certificates`
 * is what makes "confirm your name" safe rather than "type your name": it only
 * ever lists names that already have an issued certificate for this exact
 * project, so nothing here can be used to mint a claim about someone nobody
 * actually issued a record to.
 *
 * That lookup requires a signed-in team member (or staff) -- it is a directory
 * of certificate codes, and a code is what lets anyone download the record, so
 * step 2 checks team membership before showing any. Step 1 (searching for a
 * project) and step 3 (a code you already have, via /verify) both stay fully
 * public by design; only the "find my own code" step needs an account.
 */
export default async function CertificateWizardPage({
  searchParams,
}: {
  searchParams: Promise<{ q?: string; submission?: string; code?: string }>;
}) {
  const { q, submission, code } = await searchParams;

  return (
    <main className="shell narrow">
      <section style={{ padding: '28px 0 8px' }}>
        <p className="eyebrow">Get your certificate</p>
        <h1 style={{ fontSize: 30 }}>Find your project, confirm your name, download it</h1>
        <p className="muted">
          Searching is open to everyone. Confirming your name requires signing in as a
          member of that team, so nobody else can look up your certificate code — see{' '}
          <Link href="/verify">/verify</Link> if you already have a code.
        </p>
      </section>

      <ol className="wizard-steps">
        <li className={!submission ? 'on' : 'done'}>1. Find your project</li>
        <li className={submission && !code ? 'on' : submission && code ? 'done' : ''}>
          2. Confirm your name
        </li>
        <li className={code ? 'on' : ''}>3. Preview and download</li>
      </ol>

      {code && submission ? (
        <StepThree code={code} submissionId={submission} />
      ) : submission ? (
        <StepTwo submissionId={submission} />
      ) : (
        <StepOne q={q} />
      )}
    </main>
  );
}

async function StepOne({ q }: { q?: string }) {
  const page = q
    ? await api<GalleryPage>(`/api/gallery?q=${encodeURIComponent(q)}&sort=name`)
    : null;

  return (
    <>
      <form method="GET" className="panel">
        <label htmlFor="q">Project or team name</label>
        <input id="q" name="q" type="search" defaultValue={q ?? ''} placeholder="e.g. Switchyard" autoFocus />
        <button type="submit" className="button primary" style={{ marginTop: 10 }}>
          Search
        </button>
      </form>

      {page && (
        page.items.length === 0 ? (
          <Empty>No project matches that. Check the spelling, or ask your team lead for the exact name.</Empty>
        ) : (
          <div className="panel">
            {page.items.map((project) => (
              <p key={project.id} style={{ margin: '10px 0' }}>
                <Link href={`/certificate?submission=${project.id}`} className="button">
                  {project.name}
                </Link>{' '}
                <span className="muted small">by {project.team_name}</span>
              </p>
            ))}
          </div>
        )
      )}
    </>
  );
}

async function StepTwo({ submissionId }: { submissionId: string }) {
  const me = await getMe();
  const next = `/certificate?submission=${submissionId}`;

  if (!me.authenticated) {
    return (
      <Empty>
        Sign in as a member of that team to see the names a certificate has
        been issued for.{' '}
        <Link href={`/login?next=${encodeURIComponent(next)}`}>Sign in</Link>, or{' '}
        <Link href="/certificate">search a different project</Link>.
      </Empty>
    );
  }

  let rows: CertificateLookupRow[];
  try {
    rows = await api<CertificateLookupRow[]>(`/api/submissions/${submissionId}/certificates`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 403) {
      return (
        <Empty>
          You are not on this project&apos;s team, so its certificates aren&apos;t
          shown here. If you have your own code already, use{' '}
          <Link href="/verify">/verify</Link> instead.
        </Empty>
      );
    }
    return (
      <Empty>
        That project could not be found. <Link href="/certificate">Search again</Link>.
      </Empty>
    );
  }

  if (rows.length === 0) {
    return (
      <Empty>
        No certificates have been issued for this project yet. The organizer issues
        them after judging closes — check back later, or ask them directly.
      </Empty>
    );
  }

  return (
    <div className="panel">
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        Pick the name and record that is yours.
      </p>
      {rows.map((row) => (
        <p key={row.code} style={{ margin: '10px 0' }}>
          <Link
            href={`/certificate?submission=${submissionId}&code=${row.code}`}
            className="button"
          >
            {row.subject_name}
          </Link>{' '}
          <span className="muted small">
            {row.title} ({row.kind})
          </span>
        </p>
      ))}
      <p className="muted" style={{ fontSize: 13 }}>
        <Link href="/certificate">← Search a different project</Link>
      </p>
    </div>
  );
}

async function StepThree({ code, submissionId }: { code: string; submissionId: string }) {
  return (
    <div className="panel">
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        Preview — this is the exact document the PDF and PNG below contain, including a
        stamp if the record has been withdrawn or altered.
      </p>
      {/* Server-rendered PNG from the API, not a static asset -- next/image
          would need PUBLIC_BASE added to its remote-pattern allowlist for a
          host this project does not otherwise care to have next/image trust. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={`${PUBLIC_BASE}/api/certificates/${code}/png`}
        alt={`Certificate preview, code ${code}`}
        style={{ width: '100%', borderRadius: 8, border: '1px solid var(--line)' }}
      />
      <div className="row" style={{ marginTop: 16, gap: 10 }}>
        <a className="button primary" href={`${PUBLIC_BASE}/api/certificates/${code}/pdf`}>
          Download PDF
        </a>
        <a className="button" href={`${PUBLIC_BASE}/api/certificates/${code}/png`}>
          Download PNG
        </a>
        <Link className="button" href={`/verify?code=${code}`}>
          View the raw verification page
        </Link>
      </div>
      <p className="muted" style={{ fontSize: 13 }}>
        <Link href={`/certificate?submission=${submissionId}`}>← Not you? Pick a different name</Link>
      </p>
    </div>
  );
}
