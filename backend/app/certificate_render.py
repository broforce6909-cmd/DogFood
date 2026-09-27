"""Visual certificates: a PDF and a PNG rendered from the same signed record.

Deliberately a thin layer on top of what already exists. `app/signing.py` and
the `certificates` table remain the source of truth -- a record's `payload`,
`signature` and `key_id` are exactly what `GET /api/certificates/{code}`
already returns, and this module signs nothing and stores nothing new. It only
draws the bytes that are already signed, the same way `/verify` already draws
them into an HTML page.

Two consequences of that:

* **Revocation and a broken signature still have to show.** A revoked or
  tampered record renders with that stamped across it rather than looking like
  a genuine one with a prettier layout -- the whole reason `/verify` shows
  `detail` instead of hiding a bad verdict applies here unchanged.
* **The QR code encodes the human verification page**
  (`{WEB_BASE_URL}/verify?code=...`), not the raw JSON endpoint. Someone
  scanning a printed certificate is a person, not a script; the JSON is one
  click away from that page for the person who wants it.
"""

from __future__ import annotations

import base64
import html
import io

import qrcode

from .config import settings
from .models import Certificate

# Lazy imports: WeasyPrint and PyMuPDF both link native libraries (Pango/GObject,
# MuPDF) that this module's own import must not require just to define
# functions -- the test suite imports this module on machines that never render
# a certificate. Importing inside each function keeps `import app.main` cheap
# and keeps the missing-native-lib error, if any, at the one call site that
# actually needs the library rather than at process startup.


def _qr_data_uri(url: str) -> str:
    qr = qrcode.QRCode(border=1, box_size=8)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#e8e8ec", back_color="#131316")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"


def _team_display(name: str) -> str:
    """The team's name for a "Team" row, without saying "team" twice.

    A row labelled "Team" whose value is "Team Switchyard" reads as a stutter. A
    leading "Team " is dropped for display only -- the signed payload keeps the
    name exactly as the team wrote it -- and only when something remains after it.
    """
    stripped = name.strip()
    head, sep, rest = stripped.partition(" ")
    if sep and head.lower() == "team" and rest.strip():
        return rest.strip()
    return stripped


def _extra_rows(cert: Certificate, payload: dict) -> list[tuple[str, str]]:
    """The one or two facts specific to this certificate's kind.

    Mirrors exactly what `app/routers/certificates.py`'s `issue()` puts in the
    payload for each `CertificateKind` -- this does not invent a field (a rank,
    a placement) that the issuing code never actually recorded.
    """
    kind = cert.kind.value
    if kind == "participation" and payload.get("team"):
        rows = []
        # Absent on records issued before the project name was signed.
        if payload.get("project"):
            rows.append(("Project", payload["project"]))
        rows.append(("Team", _team_display(payload["team"])))
        return rows
    if kind == "judging" and "reviews_completed" in payload:
        n = payload["reviews_completed"]
        return [("Ballots completed", f"{n} review{'s' if n != 1 else ''}")]
    if kind == "placement" and payload.get("project"):
        return [("Project", payload["project"])]
    return []


def render_html(
    cert: Certificate,
    *,
    payload: dict,
    signature_valid: bool,
    revoked: bool,
    verify_url: str,
) -> str:
    """Build the certificate as a standalone HTML document.

    A plain string template rather than a template engine: this project has no
    other server-rendered HTML (the frontend is Next.js), so pulling in Jinja2
    for one document would be a dependency to justify rather than a tool
    already earning its place.
    """
    stamp = None
    if not signature_valid:
        stamp = "SIGNATURE INVALID -- ALTERED SINCE ISSUE"
    elif revoked:
        stamp = "REVOKED"

    extra = _extra_rows(cert, payload)
    extra_html = "".join(
        f'<div class="row"><span class="label">{html.escape(k)}</span>'
        f'<span class="value">{html.escape(str(v))}</span></div>'
        for k, v in extra
    )
    qr_uri = _qr_data_uri(verify_url)
    issued = cert.issued_at.strftime("%d %B %Y")

    # Only ever a positive statement about a signature that was just checked; an
    # invalid or withdrawn record gets the stamp below instead, never both.
    sig_html = ""
    if signature_valid and not revoked:
        sig_html = (
            '<div class="sig">Ed25519 signature verified &middot; key '
            f"{html.escape(cert.key_id)}</div>"
        )

    stamp_html = ""
    if stamp:
        stamp_html = f'<div class="stamp">{html.escape(stamp)}</div>'

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  @page {{ size: 297mm 210mm; margin: 0; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; width: 297mm; height: 210mm;
    background: #131316; color: #ececf0;
    font-family: 'DejaVu Sans', sans-serif;
    position: relative;
  }}
  .frame {{
    position: absolute; top: 14mm; right: 14mm; bottom: 14mm; left: 14mm;
    border: 1px solid #34343c; border-radius: 4mm;
    padding: 24mm 20mm 16mm;
  }}
  .eyebrow {{
    text-transform: uppercase; letter-spacing: 0.25em; font-size: 11pt;
    color: #8a8a96; margin: 0 0 4mm;
  }}
  h1 {{ font-size: 28pt; margin: 0 0 6mm; color: #ffffff; }}
  .rule {{ width: 34mm; height: 1.2mm; background: #7dd3c0; margin: 0 0 12mm; }}
  .subject {{ font-size: 30pt; margin: 0 0 3mm; color: #7dd3c0; }}
  .event {{ font-size: 14pt; color: #b6b6c0; margin: 0 0 8mm; }}
  .rows {{
    margin-top: 12mm; padding-top: 8mm; max-width: 170mm;
    border-top: 1px solid #34343c;
  }}
  .row {{ display: flex; gap: 6mm; font-size: 13pt; margin-bottom: 3mm; }}
  .label {{ color: #8a8a96; min-width: 40mm; }}
  .value {{ color: #ececf0; font-family: 'DejaVu Sans Mono', monospace; }}
  /* Positioned relative to .frame's own padding box, not the flex flow above
     -- WeasyPrint's flexbox does not reliably distribute `margin: auto` the
     way a browser does, so pinning the footer directly is the more portable
     way to anchor it to the card's bottom edge regardless of how much
     content sits above it. */
  .footer {{
    position: absolute; left: 20mm; right: 20mm; bottom: 16mm;
    display: flex; align-items: flex-end; justify-content: space-between;
  }}
  /* In the footer's own flow, not pinned to a fixed offset: a long event name
     that wraps the title pushes the details down, and a separately positioned
     line would then collide with them. */
  .sig {{
    display: block; margin-bottom: 5mm;
    font-size: 10pt; color: #7dd3c0; letter-spacing: 0.05em;
  }}
  .code {{
    font-family: 'DejaVu Sans Mono', monospace; letter-spacing: 0.15em;
    font-size: 10pt; color: #8a8a96;
  }}
  .qr {{ width: 22mm; height: 22mm; }}
  .stamp {{
    position: absolute; top: 45%; left: 50%; transform: translate(-50%, -50%) rotate(-18deg);
    border: 3mm solid #b4351f; color: #b4351f; font-size: 22pt; font-weight: bold;
    padding: 4mm 10mm; letter-spacing: 0.1em; opacity: 0.85; white-space: nowrap;
  }}
</style></head>
<body>
  <div class="frame">
    <p class="eyebrow">{html.escape(payload.get('event_name', cert.title))}</p>
    <h1>{html.escape(cert.title)}</h1>
    <div class="rule"></div>
    <p class="subject">{html.escape(cert.subject_name)}</p>
    <p class="event">Issued {html.escape(issued)}</p>
    <div class="rows">{extra_html}</div>
    <div class="footer">
      <div>
        {sig_html}
        <span class="code">Verify: {html.escape(verify_url)}<br>Code {html.escape(cert.code)}</span>
      </div>
      <img class="qr" src="{qr_uri}">
    </div>
  </div>
  {stamp_html}
</body></html>"""


def render_pdf(cert: Certificate, *, payload: dict, signature_valid: bool, revoked: bool) -> bytes:
    import weasyprint  # noqa: PLC0415 -- see module docstring

    verify_url = f"{settings.web_base_url.rstrip('/')}/verify?code={cert.code}"
    doc_html = render_html(
        cert,
        payload=payload,
        signature_valid=signature_valid,
        revoked=revoked,
        verify_url=verify_url,
    )
    return weasyprint.HTML(string=doc_html).write_pdf()


def render_png(
    cert: Certificate, *, payload: dict, signature_valid: bool, revoked: bool, dpi: int = 200
) -> bytes:
    import fitz  # PyMuPDF  # noqa: PLC0415 -- see module docstring

    pdf_bytes = render_pdf(cert, payload=payload, signature_valid=signature_valid, revoked=revoked)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc.load_page(0)
    pix = page.get_pixmap(dpi=dpi)
    return pix.tobytes("png")
