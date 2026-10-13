
from __future__ import annotations

from datetime import date
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas


def generate_engagement_letter(
    output_path: str,
    name: str,
    company: str,
    signed_date: date,
) -> str:
    """Generate a professional engagement letter PDF."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter
    left = 72
    right = width - 72
    y = height - 65

    c.setTitle("Engagement Letter")

    def paragraph(text, font="Helvetica", size=10, leading=15, gap=9):
        nonlocal y
        lines = simpleSplit(text, font, size, right - left)

        for line in lines:
            if y < 75:
                c.showPage()
                y = height - 65

            c.setFont(font, size)
            c.drawString(left, y, line)
            y -= leading

        y -= gap

    def heading(text):
        nonlocal y
        y -= 4
        paragraph(text, "Helvetica-Bold", 11, 15, 3)

    # Letter title
    c.setFont("Helvetica-Bold", 17)
    c.drawString(left, y, "ENGAGEMENT LETTER")
    y -= 32

    # Client details
    paragraph(f"Date: {signed_date.strftime('%d %B %Y')}", gap=2)
    paragraph(f"To: {name or 'Client'}", "Helvetica-Bold", gap=2)

    if company:
        paragraph(f"Company: {company}", gap=2)

    y -= 8
    paragraph(f"Dear {name or 'Client'},", gap=5)

    paragraph(
        f"We are pleased to set out the basis on which our team will "
        f"work with you and {company or 'your organization'}. "
        "This letter summarizes the general arrangements for the "
        "engagement and should be read together with any separately "
        "agreed proposal, service description, or commercial terms."
    )

    heading("1. Scope of Engagement")
    paragraph(
        "We will provide the professional services and deliverables "
        "mutually agreed with you before work begins. The specific "
        "scope, expected outcomes, milestones, and delivery timelines "
        "will be confirmed separately where applicable. Any material "
        "change to the agreed scope will be discussed and approved "
        "by both parties."
    )

    heading("2. Client Responsibilities")
    paragraph(
        "You agree to provide the information, documents, access, "
        "and cooperation reasonably required for us to perform the "
        "agreed services. You are responsible for ensuring that "
        "information supplied to us is accurate and that you are "
        "authorized to share it for the purposes of this engagement."
    )

    heading("3. Fees and Payment")
    paragraph(
        "The fees, applicable taxes, invoicing arrangements, and "
        "payment deadlines will be set out in the commercial proposal "
        "or other written terms agreed by both parties. No unspecified "
        "fee or additional charge should be treated as agreed merely "
        "by signing this letter."
    )

    heading("4. Confidentiality and Information Handling")
    paragraph(
        "Each party is expected to protect non-public information "
        "received from the other party and use it only for legitimate "
        "purposes connected with the engagement. Any applicable data "
        "protection requirements and separate confidentiality "
        "agreements will continue to apply."
    )

    heading("5. Term and Termination")
    paragraph(
        "The start date and duration of the engagement will be "
        "determined by the agreed service arrangements. Termination, "
        "notice periods, and any outstanding payment or delivery "
        "obligations will be governed by the applicable written terms "
        "agreed by both parties."
    )

    heading("6. Acceptance")
    paragraph(
        "Please review this letter and any accompanying proposal or "
        "terms before signing. By signing, you acknowledge the terms "
        "presented for this engagement and confirm that you are "
        "authorized to accept them on behalf of yourself or the "
        "company identified above, as applicable."
    )

    paragraph(
        "We appreciate the opportunity to work with you and look "
        "forward to establishing a productive professional relationship.",
        gap=5,
    )

    paragraph("Yours sincerely,", gap=2)
    paragraph("Onboarding Team", "Helvetica-Bold", gap=12)

    heading("CLIENT ACKNOWLEDGMENT")
    paragraph(f"Client name: {name or 'Client'}", gap=2)

    if company:
        paragraph(f"Company: {company}", gap=2)

    paragraph(
        "Electronic signature: To be completed through the "
        "designated signing provider.",
        gap=2,
    )
    paragraph("Date signed: Recorded by the signing provider.", gap=2)

    c.save()
    return str(path)
