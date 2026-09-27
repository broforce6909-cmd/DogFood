import Link from 'next/link';

import { PUBLIC_BASE } from '@/lib/api';
import { formatDateTime } from '@/lib/format';

export const dynamic = 'force-dynamic';

export const metadata = { title: 'Verify a record -- Dogfood' };

type Verified = {
  code: string;
  kind: string;
  title: string;
  subject_name: string;
  issued_at: string;
  payload: string;
  signature: string;
  key_id: string;
  signature_valid: boolean;
  revoked: boolean;
  revoked_reason: string | null;
  valid: boolean;
  detail: string | null;
};

/**
 * Public certificate verification.
 *
 * Deliberately **not** authenticated and deliberately not inside the organizer
 * area: the point of a signed record is that somebody with no relationship to this
 * install can check it. A verification page behind a login would defeat the feature.
 *
 * It fetches the API directly rather than through `lib/api`, because `lib/api`
 * attaches the caller's session and this page must behave identically for a stranger.
 */
export default async function VerifyPage({
  searchParams,
}: {
  searchParams: Promise<{ code?: string }>;
}) {
  const { code } = await searchParams;
  const trimmed = (code ?? '').trim().toUpperCase();

  let record: Verified | null = null;
  let notFound = false;

  if (trimmed) {
    const response = await fetch(
      `${process.env.API_INTERNAL_BASE ?? PUBLIC_BASE}/api/certificates/${encodeURIComponent(trimmed)}`,
      { cache: 'no-store' },
    );
    if (response.ok) record = (await response.json()) as Verified;
    else notFound = true;
  }

  return (
    <main className="shell narrow">
      <section style={{ padding: '28px 0 8px' }}>
        <p className="eyebrow">Verify a record</p>
        <h1 style={{ fontSize: 30 }}>Check a certificate</h1>
        <p className="muted">
          Every record this platform issues carries an Ed25519 signature over its own
          contents, checkable against a published public key — not a secret only we
          hold. Paste the code from a certificate and this page will tell you whether
          it is genuine, and whether it has been withdrawn.
        </p>
      </section>

      <form method="GET" className="panel">
        <label htmlFor="code">Verification code</label>
        <input
          id="code"
          name="code"
          defaultValue={trimmed}
          placeholder="e.g. H7KQ9R2MXT4B"
          autoCapitalize="characters"
          style={{ textTransform: 'uppercase', letterSpacing: '0.08em' }}
        />
        <button type="submit" className="button primary" style={{ marginTop: 10 }}>
          Verify
        </button>
      </form>

      {notFound && (
        <p className="notice bad">
          No record with that code. Check for a mistyped character — the codes avoid
          O/0 and I/1 precisely because they get read aloud.
        </p>
      )}

      {record && (
        <>
          <section className={record.valid ? 'panel' : 'panel'}>
            <div className="row" style={{ marginBottom: 10 }}>
              <span className={record.valid ? 'pill ok' : 'pill warn'}>
                {record.valid ? 'Genuine' : record.revoked ? 'Revoked' : 'Not genuine'}
              </span>
              <span className="muted" style={{ fontSize: 13 }}>
                {record.code}
              </span>
            </div>

            <h2 style={{ fontSize: 22, marginTop: 0 }}>{record.title}</h2>
            <dl className="kv">
              <dt>Issued to</dt>
              <dd>
                <strong>{record.subject_name}</strong>
              </dd>
              <dt>Kind</dt>
              <dd>{record.kind}</dd>
              <dt>Issued</dt>
              <dd>{formatDateTime(record.issued_at)}</dd>
              <dt>Signature</dt>
              <dd>{record.signature_valid ? 'matches the contents' : 'does NOT match'}</dd>
              <dt>Withdrawn</dt>
              <dd>{record.revoked ? (record.revoked_reason ?? 'yes') : 'no'}</dd>
            </dl>

            {record.detail && (
              <p className={record.valid ? 'notice' : 'notice bad'}>{record.detail}</p>
            )}
          </section>

          <section className="panel">
            <h3 style={{ marginTop: 0, fontSize: 16 }}>Check it yourself</h3>
            <p className="muted" style={{ fontSize: 13 }}>
              You do not have to trust this page, or us. These are the exact bytes
              that were signed, the Ed25519 signature over them, and the id of the
              key that signed it — fetch that key&rsquo;s public half from{' '}
              <a href={`${PUBLIC_BASE}/api/signing/public-keys`}>
                /api/signing/public-keys
              </a>{' '}
              and verify the signature yourself with nothing else from us.
            </p>
            <label htmlFor="payload">Signed payload</label>
            <textarea id="payload" readOnly rows={6} value={record.payload} />
            <label htmlFor="signature">Signature (Ed25519, hex)</label>
            <textarea id="signature" readOnly rows={2} value={record.signature} />
            <label htmlFor="key_id">Signing key id</label>
            <textarea id="key_id" readOnly rows={1} value={record.key_id} />
            <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
              Raw JSON:{' '}
              <a href={`${PUBLIC_BASE}/api/certificates/${record.code}`}>
                /api/certificates/{record.code}
              </a>
            </p>
          </section>
        </>
      )}

      <p className="muted" style={{ fontSize: 13 }}>
        <Link href="/">← Back to the portal</Link>
      </p>
    </main>
  );
}
