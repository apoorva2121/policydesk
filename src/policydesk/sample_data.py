"""Builds the fake company's data: policy PDFs, expense spreadsheets, a database.

Why generate data instead of downloading it?

1. **Reproducible.** Anyone can rebuild the exact same dataset from a clean
   machine with one command. No broken links, no licences, no 200MB download.
2. **We know the right answers.** Later, in the evaluation milestone, we need
   questions with known correct answers. That is only possible if we control
   the data.
3. **We can plant realistic problems on purpose** — a spreadsheet whose header
   is not on the first row, a scanned PDF with no readable text, expenses that
   break policy. Real data is messy, so our practice data should be too.

Everything here is deterministic: no random numbers, no timestamps from "now".
Run it twice and you get byte-identical content. That matters, because an
evaluation score you cannot reproduce is not a measurement.
"""

from __future__ import annotations

import io
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from PIL import Image, ImageDraw
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

COMPANY = "Northwind Labs"

# ---------------------------------------------------------------------------
# 1. The policy text.
#
# These numbers are the "ground truth" of the whole project. The agent must
# read them out of a PDF -- they are deliberately NOT in the database, which is
# what forces the system to combine two sources.
# ---------------------------------------------------------------------------

TRAVEL_POLICY: list[tuple[str, list[str]]] = [
    (
        "1. Purpose and scope",
        [
            f"This policy governs all business travel and expense reimbursement at {COMPANY}. "
            "It applies to every employee, contractor and intern who incurs expenses on behalf "
            "of the company, worldwide.",
            "Effective date: 1 January 2026. This policy supersedes all previous travel and "
            "expense guidance. Questions should be directed to the Finance Operations team.",
        ],
    ),
    (
        "2. Meal allowances (per diem)",
        [
            "Meals are reimbursed up to a daily per-diem cap. The cap covers all meals and "
            "non-alcoholic beverages for a single calendar day and is not transferable between "
            "days.",
            "The domestic meal per-diem limit is USD 60 per day. The international meal "
            "per-diem limit is USD 85 per day.",
            "Alcohol is not reimbursable under the meal per diem. Alcohol purchased during "
            "approved client entertainment is covered by section 5 instead.",
        ],
    ),
    (
        "3. Accommodation",
        [
            "Hotel rooms must be booked at standard room rates. The domestic nightly limit is "
            "USD 220 and the international nightly limit is USD 300, in both cases excluding "
            "local taxes.",
            "A higher nightly limit of USD 350 applies in New York City, San Francisco, London "
            "and Tokyo, in recognition of local market rates.",
            "Room service, minibar charges, in-room entertainment and spa services are not "
            "reimbursable.",
        ],
    ),
    (
        "4. Air travel",
        [
            "Economy class is required for all scheduled flights with a total duration under "
            "six hours. Premium economy is permitted for flights of six hours or more.",
            "Business class travel requires written approval from a Vice President or above, "
            "obtained before booking. The approval reference must be recorded on the expense "
            "claim.",
            "Flights should be booked at least fourteen days in advance where the travel date "
            "is known. Seat upgrades purchased at the airport are not reimbursable.",
        ],
    ),
    (
        "5. Client entertainment",
        [
            "Client entertainment requires pre-approval from the relevant department head. The "
            "approval reference must be recorded on the expense claim.",
            "Client entertainment is capped at USD 150 per attendee, including the employee. "
            "The names and organisations of all attendees must be listed in the claim "
            "description.",
        ],
    ),
    (
        "6. Ground transport and mileage",
        [
            "Standard taxi, ride-hailing and public transport fares are reimbursable. Premium "
            "or luxury vehicle tiers are not.",
            "Use of a personal vehicle is reimbursed at USD 0.67 per mile. Parking and tolls "
            "are reimbursable in addition to the mileage rate.",
        ],
    ),
    (
        "7. Receipts and documentation",
        [
            "An itemised receipt is required for every individual expense of USD 75 or more. "
            "Claims of USD 75 or more submitted without a receipt will be rejected.",
            "Expenses below USD 75 may be claimed without a receipt, but a description of the "
            "business purpose is always required.",
            "Credit card statements are not accepted as a substitute for an itemised receipt.",
        ],
    ),
    (
        "8. Submission deadlines",
        [
            "Expense claims must be submitted within 30 calendar days of the date the expense "
            "was incurred. Claims submitted after 60 days require Finance Director approval "
            "and may be refused.",
            "Reimbursement is normally paid within two payroll cycles of approval.",
        ],
    ),
    (
        "9. Non-reimbursable expenses",
        [
            "The following are never reimbursable: personal travel, traffic and parking fines, "
            "lost or stolen personal property, personal grooming, airline lounge memberships, "
            "and travel insurance purchased independently of the corporate policy.",
        ],
    ),
]

PER_DIEM_TABLE: list[list[str]] = [
    ["Category", "Domestic (USD)", "International (USD)", "Basis"],
    ["Meals", "60", "85", "per day"],
    ["Hotel (standard)", "220", "300", "per night"],
    ["Hotel (high-cost city)", "350", "350", "per night"],
    ["Client entertainment", "150", "150", "per attendee"],
    ["Receipt threshold", "75", "75", "per expense"],
    ["Mileage", "0.67", "0.67", "per mile"],
]

APPROVAL_POLICY: list[tuple[str, list[str]]] = [
    (
        "1. Approval authority",
        [
            "Expense claims are approved by the claimant's direct manager. Claims above "
            "USD 2,000 additionally require department head approval.",
            "No employee may approve their own claim, or a claim from which they personally "
            "benefit.",
        ],
    ),
    (
        "2. Escalation triggers",
        [
            "A claim is escalated to Finance Operations when it exceeds a policy limit, is "
            "missing a required receipt, is submitted more than 60 days late, or involves "
            "business class travel without a recorded approval reference.",
            "Escalated claims are reviewed by a human. Automated systems may flag a claim for "
            "escalation but may never approve or reject one.",
        ],
    ),
    (
        "3. Audit sampling",
        [
            "Finance Operations samples at least 5 percent of approved claims each quarter for "
            "audit, weighted towards high-value and previously flagged claimants.",
        ],
    ),
]


# ---------------------------------------------------------------------------
# 2. The expense records.
#
# Each row is written out by hand so the dataset is stable and we know exactly
# which rows break which rule. The `breaks` comment on a row is for US, the
# humans -- it is NOT stored in the database, otherwise the agent could cheat.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Expense:
    id: int
    employee_id: int
    expense_date: str  # ISO date
    category: str
    merchant: str
    amount_usd: float
    country: str
    city: str
    description: str
    receipt_attached: bool
    approval_ref: str | None
    status: str
    submitted_at: str


DEPARTMENTS: list[tuple[int, str, str]] = [
    (1, "Engineering", "CC-1001"),
    (2, "Sales", "CC-2001"),
    (3, "Marketing", "CC-3001"),
    (4, "Finance", "CC-4001"),
]

EMPLOYEES: list[tuple[int, str, str, int, str, str]] = [
    (1, "Priya Raman", "priya.raman@northwindlabs.example", 1, "L5", "USA"),
    (2, "Daniel Okafor", "daniel.okafor@northwindlabs.example", 2, "L6", "USA"),
    (3, "Mei Lin", "mei.lin@northwindlabs.example", 1, "L4", "USA"),
    (4, "Tomas Berg", "tomas.berg@northwindlabs.example", 3, "L5", "USA"),
    (5, "Sarah Whitfield", "sarah.whitfield@northwindlabs.example", 4, "L7", "USA"),
]

EXPENSES: list[Expense] = [
    # --- Priya Raman, Q3: a clean trip to Austin (domestic) ---
    Expense(
        1001,
        1,
        "2026-07-14",
        "airfare",
        "Unified Air",
        412.00,
        "USA",
        "Austin",
        "Economy flight SFO-AUS for platform summit",
        True,
        None,
        "reimbursed",
        "2026-07-20",
    ),
    Expense(
        1002,
        1,
        "2026-07-14",
        "hotel",
        "Lamar Hotel Austin",
        198.00,
        "USA",
        "Austin",
        "1 night, standard room",
        True,
        None,
        "reimbursed",
        "2026-07-20",
    ),
    Expense(
        1003,
        1,
        "2026-07-14",
        "meals",
        "Franklin Cafe",
        54.20,
        "USA",
        "Austin",
        "Dinner, self",
        False,
        None,
        "reimbursed",
        "2026-07-20",
    ),
    Expense(
        1004,
        1,
        "2026-07-15",
        "ground_transport",
        "RideNow",
        38.60,
        "USA",
        "Austin",
        "Airport to hotel and return",
        False,
        None,
        "reimbursed",
        "2026-07-20",
    ),
    # --- Priya Raman, Q4 October: the trip our flagship question asks about ---
    Expense(
        1101,
        1,
        "2026-10-03",
        "meals",
        "Tsukiji Kitchen",
        112.40,
        "Japan",
        "Tokyo",
        "Dinner, self, during partner workshop",
        True,
        None,
        "submitted",
        "2026-10-09",
    ),
    # ^ breaks section 2: international meal per diem is 85
    Expense(
        1102,
        1,
        "2026-10-03",
        "hotel",
        "Shinagawa Tower Hotel",
        289.00,
        "Japan",
        "Tokyo",
        "1 night, standard room",
        True,
        None,
        "submitted",
        "2026-10-09",
    ),
    Expense(
        1103,
        1,
        "2026-10-04",
        "hotel",
        "Shinagawa Tower Hotel",
        289.00,
        "Japan",
        "Tokyo",
        "1 night, standard room",
        True,
        None,
        "submitted",
        "2026-10-09",
    ),
    Expense(
        1104,
        1,
        "2026-10-05",
        "ground_transport",
        "Tokyo Metro",
        42.10,
        "Japan",
        "Tokyo",
        "Local transit, 3 days",
        False,
        None,
        "submitted",
        "2026-10-09",
    ),
    Expense(
        1105,
        1,
        "2026-10-06",
        "airfare",
        "Unified Air",
        1845.00,
        "Japan",
        "Tokyo",
        "Business class HND-SFO, 10h flight",
        True,
        None,
        "submitted",
        "2026-10-09",
    ),
    # ^ breaks section 4: business class with no VP approval reference
    Expense(
        1106,
        1,
        "2026-10-12",
        "software",
        "DiagramCloud",
        89.00,
        "USA",
        "San Francisco",
        "Annual licence for architecture diagrams",
        False,
        None,
        "submitted",
        "2026-10-15",
    ),
    # ^ breaks section 7: 75 or more and no receipt
    Expense(
        1107,
        1,
        "2026-10-15",
        "meals",
        "Blue Bottle",
        48.00,
        "USA",
        "San Francisco",
        "Team breakfast, 1 attendee",
        False,
        None,
        "submitted",
        "2026-10-15",
    ),
    # --- Daniel Okafor, Sales: client entertainment, one breach ---
    Expense(
        2001,
        2,
        "2026-07-22",
        "client_entertainment",
        "The Ledbury",
        980.00,
        "UK",
        "London",
        "Dinner with Arcadia Group: 4 attendees plus self",
        True,
        "AP-2026-0471",
        "approved",
        "2026-07-29",
    ),
    # 980 / 5 attendees = 196 per head -> breaks section 5 cap of 150
    Expense(
        2002,
        2,
        "2026-07-22",
        "hotel",
        "Kings Cross Suites",
        342.00,
        "UK",
        "London",
        "1 night, standard room, high-cost city",
        True,
        None,
        "approved",
        "2026-07-29",
    ),
    Expense(
        2003,
        2,
        "2026-07-23",
        "meals",
        "Pret",
        31.80,
        "UK",
        "London",
        "Lunch, self",
        False,
        None,
        "approved",
        "2026-07-29",
    ),
    Expense(
        2004,
        2,
        "2026-08-11",
        "airfare",
        "Transcontinental",
        1120.00,
        "UK",
        "London",
        "Premium economy JFK-LHR, 7h flight",
        True,
        None,
        "approved",
        "2026-08-14",
    ),
    Expense(
        2005,
        2,
        "2026-08-12",
        "client_entertainment",
        "Sketch",
        420.00,
        "UK",
        "London",
        "Dinner with Meridian Partners: 3 attendees plus self",
        True,
        "AP-2026-0588",
        "approved",
        "2026-08-14",
    ),
    Expense(
        2006,
        2,
        "2026-09-02",
        "ground_transport",
        "Black Cab",
        96.00,
        "UK",
        "London",
        "Airport transfer",
        True,
        None,
        "approved",
        "2026-09-05",
    ),
    Expense(
        2007,
        2,
        "2026-09-28",
        "meals",
        "Dishoom",
        88.50,
        "UK",
        "London",
        "Dinner, self",
        True,
        None,
        "approved",
        "2026-10-02",
    ),
    # ^ breaks section 2: international meal per diem is 85
    # --- Mei Lin, Engineering: conference, all clean ---
    Expense(
        3001,
        3,
        "2026-08-04",
        "conference",
        "PyData",
        650.00,
        "USA",
        "Seattle",
        "Conference registration",
        True,
        None,
        "reimbursed",
        "2026-08-08",
    ),
    Expense(
        3002,
        3,
        "2026-08-04",
        "airfare",
        "Unified Air",
        288.00,
        "USA",
        "Seattle",
        "Economy SFO-SEA, 2h flight",
        True,
        None,
        "reimbursed",
        "2026-08-08",
    ),
    Expense(
        3003,
        3,
        "2026-08-04",
        "hotel",
        "Pike Place Inn",
        215.00,
        "USA",
        "Seattle",
        "1 night, standard room",
        True,
        None,
        "reimbursed",
        "2026-08-08",
    ),
    Expense(
        3004,
        3,
        "2026-08-05",
        "hotel",
        "Pike Place Inn",
        215.00,
        "USA",
        "Seattle",
        "1 night, standard room",
        True,
        None,
        "reimbursed",
        "2026-08-08",
    ),
    Expense(
        3005,
        3,
        "2026-08-05",
        "meals",
        "Serious Biscuit",
        44.00,
        "USA",
        "Seattle",
        "Breakfast and lunch, self",
        False,
        None,
        "reimbursed",
        "2026-08-08",
    ),
    Expense(
        3006,
        3,
        "2026-09-19",
        "software",
        "ProfilerPro",
        72.00,
        "USA",
        "San Francisco",
        "Monthly licence",
        False,
        None,
        "reimbursed",
        "2026-09-22",
    ),
    Expense(
        3007,
        3,
        "2026-10-02",
        "ground_transport",
        "Personal vehicle",
        40.20,
        "USA",
        "San Jose",
        "60 miles at 0.67 per mile, customer visit",
        False,
        None,
        "approved",
        "2026-10-06",
    ),
    # --- Tomas Berg, Marketing: a late submission and a hotel breach ---
    Expense(
        4001,
        4,
        "2026-07-08",
        "hotel",
        "Gran Via Palace",
        268.00,
        "Spain",
        "Madrid",
        "1 night, standard room",
        True,
        None,
        "approved",
        "2026-09-30",
    ),
    # ^ breaks section 8: submitted 84 days after the expense date
    Expense(
        4002,
        4,
        "2026-07-08",
        "meals",
        "Mercado",
        62.00,
        "Spain",
        "Madrid",
        "Dinner, self",
        False,
        None,
        "approved",
        "2026-09-30",
    ),
    Expense(
        4003,
        4,
        "2026-08-19",
        "hotel",
        "Harbour Lodge",
        318.00,
        "Australia",
        "Sydney",
        "1 night, standard room",
        True,
        None,
        "flagged",
        "2026-08-25",
    ),
    # ^ breaks section 3: international hotel limit is 300, Sydney is not high-cost
    Expense(
        4004,
        4,
        "2026-08-19",
        "airfare",
        "Pacific Air",
        1680.00,
        "Australia",
        "Sydney",
        "Premium economy LAX-SYD, 15h flight",
        True,
        None,
        "approved",
        "2026-08-25",
    ),
    Expense(
        4005,
        4,
        "2026-09-11",
        "conference",
        "BrandWorld",
        1250.00,
        "USA",
        "Chicago",
        "Conference registration and workshop",
        True,
        "AP-2026-0612",
        "approved",
        "2026-09-15",
    ),
    Expense(
        4006,
        4,
        "2026-09-11",
        "meals",
        "Lou Malnati",
        58.00,
        "USA",
        "Chicago",
        "Dinner, self",
        False,
        None,
        "approved",
        "2026-09-15",
    ),
    Expense(
        4007,
        4,
        "2026-10-07",
        "ground_transport",
        "LuxeBlack",
        210.00,
        "USA",
        "Chicago",
        "Executive car service to venue",
        True,
        None,
        "flagged",
        "2026-10-10",
    ),
    # ^ breaks section 6: premium vehicle tier
    # --- Sarah Whitfield, Finance: small and clean ---
    Expense(
        5001,
        5,
        "2026-07-30",
        "software",
        "LedgerLink",
        149.00,
        "USA",
        "Boston",
        "Quarterly licence",
        True,
        None,
        "reimbursed",
        "2026-08-02",
    ),
    Expense(
        5002,
        5,
        "2026-08-21",
        "meals",
        "Tatte",
        22.40,
        "USA",
        "Boston",
        "Breakfast, self",
        False,
        None,
        "reimbursed",
        "2026-08-24",
    ),
    Expense(
        5003,
        5,
        "2026-09-09",
        "airfare",
        "Unified Air",
        356.00,
        "USA",
        "New York",
        "Economy BOS-JFK, 1h flight",
        True,
        None,
        "reimbursed",
        "2026-09-12",
    ),
    Expense(
        5004,
        5,
        "2026-09-09",
        "hotel",
        "Midtown Rooms",
        331.00,
        "USA",
        "New York",
        "1 night, standard room, high-cost city",
        True,
        None,
        "reimbursed",
        "2026-09-12",
    ),
    Expense(
        5005,
        5,
        "2026-10-01",
        "conference",
        "FinOps Summit",
        880.00,
        "USA",
        "New York",
        "Conference registration",
        True,
        None,
        "submitted",
        "2026-10-04",
    ),
]

# Department budgets, so aggregate questions have something to compare against.
BUDGETS: list[tuple[int, str, str, float]] = [
    (1, "2026-Q3", "travel", 6000.00),
    (1, "2026-Q4", "travel", 6000.00),
    (2, "2026-Q3", "travel", 18000.00),
    (2, "2026-Q4", "travel", 18000.00),
    (3, "2026-Q3", "travel", 12000.00),
    (3, "2026-Q4", "travel", 9000.00),
    (4, "2026-Q3", "travel", 4000.00),
    (4, "2026-Q4", "travel", 4000.00),
]


# ---------------------------------------------------------------------------
# 3. PDF builders
# ---------------------------------------------------------------------------


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "DocTitle", parent=base["Title"], fontSize=20, spaceAfter=6, leading=24
        ),
        "subtitle": ParagraphStyle(
            "DocSubtitle",
            parent=base["Normal"],
            fontSize=10,
            textColor=colors.grey,
            spaceAfter=18,
        ),
        "heading": ParagraphStyle(
            "SectionHeading", parent=base["Heading2"], fontSize=13, spaceBefore=14, spaceAfter=6
        ),
        "body": ParagraphStyle(
            "BodyText2", parent=base["BodyText"], fontSize=10.5, leading=15, spaceAfter=8
        ),
    }


def _write_policy_pdf(
    path: Path,
    title: str,
    subtitle: str,
    sections: list[tuple[str, list[str]]],
    table: list[list[str]] | None = None,
) -> None:
    """Render a policy document as a real, text-based PDF."""
    path.parent.mkdir(parents=True, exist_ok=True)
    style = _styles()
    doc = SimpleDocTemplate(
        str(path),
        pagesize=LETTER,
        title=title,
        author=COMPANY,
        leftMargin=0.9 * inch,
        rightMargin=0.9 * inch,
        topMargin=0.9 * inch,
        bottomMargin=0.9 * inch,
    )

    flow: list[object] = [Paragraph(title, style["title"]), Paragraph(subtitle, style["subtitle"])]

    for heading, paragraphs in sections:
        flow.append(Paragraph(heading, style["heading"]))
        for text in paragraphs:
            flow.append(Paragraph(text, style["body"]))

    if table:
        flow.append(PageBreak())
        flow.append(Paragraph("Appendix A: Limits at a glance", style["heading"]))
        flow.append(
            Paragraph(
                "This appendix is a summary. Where the appendix and the body of the policy "
                "disagree, the body of the policy applies.",
                style["body"],
            )
        )
        flow.append(Spacer(1, 10))
        grid = Table(table, hAlign="LEFT", colWidths=[160, 100, 110, 90])
        grid.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3b57")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9aa7b4")),
                    ("ALIGN", (1, 1), (2, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.HexColor("#eef2f6")],
                    ),
                ]
            )
        )
        flow.append(grid)

    doc.build(flow)


def _write_scanned_receipt_pdf(path: Path) -> None:
    """Make a PDF that LOOKS like text but contains only a picture.

    This is on purpose. A scanned document has no text layer, so ordinary PDF
    extraction returns nothing at all. Our extractor must notice and say so,
    rather than silently indexing an empty document. That failure mode is one
    of the most common causes of "the chatbot cannot find my document".
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    width, height = 1000, 620
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    lines = [
        "SHINAGAWA TOWER HOTEL",
        "3-13-1 Takanawa, Minato-ku, Tokyo",
        "",
        "GUEST:   P. RAMAN",
        "FOLIO:   884102",
        "NIGHT OF 2026-10-03",
        "",
        "ROOM (STANDARD)          JPY 43,100",
        "LOCAL TAX                JPY  2,155",
        "TOTAL                    JPY 45,255",
        "CHARGED USD 289.00 @ 156.6",
        "",
        "THANK YOU FOR YOUR STAY",
    ]
    y = 40
    for line in lines:
        draw.text((60, y), line, fill="black")
        y += 42
    draw.rectangle([(30, 20), (width - 30, height - 20)], outline="black", width=3)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)

    canvas = pdf_canvas.Canvas(str(path), pagesize=LETTER)
    page_width, page_height = LETTER
    draw_width = page_width - 2 * inch
    draw_height = draw_width * height / width
    canvas.drawImage(
        ImageReader(io.BytesIO(buffer.getvalue())),
        inch,
        page_height - inch - draw_height,
        width=draw_width,
        height=draw_height,
    )
    canvas.setTitle("Scanned receipt - Shinagawa Tower Hotel")
    canvas.showPage()
    canvas.save()


# ---------------------------------------------------------------------------
# 4. Excel builder
# ---------------------------------------------------------------------------


def _write_expense_workbook(path: Path) -> None:
    """Write a realistically MESSY expense workbook.

    Real spreadsheets almost never have the header on row 1. This one has a
    title block first, then a blank row, then the header on row 5. Our extractor
    has to find the header rather than assume it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()

    sheet = workbook.active
    sheet.title = "October 2026"

    sheet["A1"] = f"{COMPANY} - Expense Report"
    sheet["A1"].font = Font(size=14, bold=True)
    sheet["A2"] = "Claimant: Priya Raman (Engineering, CC-1001)"
    sheet["A3"] = "Period: 1 October 2026 to 31 October 2026"
    # row 4 deliberately blank

    header = [
        "Expense ID",
        "Date",
        "Category",
        "Merchant",
        "Amount (USD)",
        "City",
        "Country",
        "Receipt?",
        "Business purpose",
    ]
    sheet.append([])  # row 4
    sheet.append(header)  # row 5
    for cell in sheet[5]:
        cell.font = Font(bold=True)

    for expense in EXPENSES:
        if expense.employee_id == 1 and expense.expense_date.startswith("2026-10"):
            sheet.append(
                [
                    expense.id,
                    expense.expense_date,
                    expense.category,
                    expense.merchant,
                    expense.amount_usd,
                    expense.city,
                    expense.country,
                    "Y" if expense.receipt_attached else "N",
                    expense.description,
                ]
            )

    sheet.append([])
    sheet.append(["", "", "", "TOTAL", "=SUM(E6:E12)"])  # a formula, not a value

    notes = workbook.create_sheet("Notes")
    notes["A1"] = "Submitter notes"
    notes["A1"].font = Font(bold=True)
    notes["A2"] = "Business class flight approved verbally by VP Engineering - ref to follow."
    notes["A3"] = "DiagramCloud receipt was emailed separately."
    notes["A4"] = "Tokyo dinner included two colleagues from the partner team."

    workbook.save(path)


# ---------------------------------------------------------------------------
# 5. SQLite builder
# ---------------------------------------------------------------------------

SCHEMA = """
PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS expenses;
DROP TABLE IF EXISTS budgets;
DROP TABLE IF EXISTS employees;
DROP TABLE IF EXISTS departments;

CREATE TABLE departments (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    cost_center  TEXT NOT NULL
);

CREATE TABLE employees (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    email          TEXT NOT NULL UNIQUE,
    department_id  INTEGER NOT NULL REFERENCES departments(id),
    grade          TEXT NOT NULL,
    home_country   TEXT NOT NULL
);

CREATE TABLE expenses (
    id                INTEGER PRIMARY KEY,
    employee_id       INTEGER NOT NULL REFERENCES employees(id),
    expense_date      TEXT NOT NULL,          -- ISO date, YYYY-MM-DD
    category          TEXT NOT NULL,
    merchant          TEXT NOT NULL,
    amount_usd        REAL NOT NULL CHECK (amount_usd >= 0),
    country           TEXT NOT NULL,
    city              TEXT NOT NULL,
    description       TEXT NOT NULL,
    receipt_attached  INTEGER NOT NULL CHECK (receipt_attached IN (0, 1)),
    approval_ref      TEXT,
    status            TEXT NOT NULL CHECK (
                          status IN ('submitted', 'approved', 'reimbursed', 'flagged')),
    submitted_at      TEXT NOT NULL
);

CREATE TABLE budgets (
    department_id    INTEGER NOT NULL REFERENCES departments(id),
    fiscal_quarter   TEXT NOT NULL,
    category         TEXT NOT NULL,
    budget_usd       REAL NOT NULL CHECK (budget_usd >= 0),
    PRIMARY KEY (department_id, fiscal_quarter, category)
);

CREATE INDEX idx_expenses_employee ON expenses(employee_id);
CREATE INDEX idx_expenses_date     ON expenses(expense_date);
CREATE INDEX idx_expenses_category ON expenses(category);
"""


def _write_sqlite_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()  # rebuild from scratch, so the result is always identical

    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT INTO departments (id, name, cost_center) VALUES (?, ?, ?)", DEPARTMENTS
        )
        connection.executemany(
            "INSERT INTO employees (id, name, email, department_id, grade, home_country) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            EMPLOYEES,
        )
        connection.executemany(
            "INSERT INTO expenses (id, employee_id, expense_date, category, merchant, "
            "amount_usd, country, city, description, receipt_attached, approval_ref, status, "
            "submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    e.id,
                    e.employee_id,
                    e.expense_date,
                    e.category,
                    e.merchant,
                    e.amount_usd,
                    e.country,
                    e.city,
                    e.description,
                    int(e.receipt_attached),
                    e.approval_ref,
                    e.status,
                    e.submitted_at,
                )
                for e in EXPENSES
            ],
        )
        connection.executemany(
            "INSERT INTO budgets (department_id, fiscal_quarter, category, budget_usd) "
            "VALUES (?, ?, ?, ?)",
            BUDGETS,
        )
        connection.commit()
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 6. The one entry point
# ---------------------------------------------------------------------------


def generate_all(data_dir: Path, sqlite_path: Path) -> dict[str, Path]:
    """Build every sample artefact. Safe to run repeatedly (it overwrites)."""
    policies = data_dir / "raw" / "policies"
    spreadsheets = data_dir / "raw" / "expenses"

    travel_pdf = policies / "travel-and-expense-policy.pdf"
    approval_pdf = policies / "approval-and-escalation-policy.pdf"
    scanned_pdf = policies / "scanned-receipt-tokyo.pdf"
    workbook = spreadsheets / "expense-report-2026-10-raman.xlsx"

    _write_policy_pdf(
        travel_pdf,
        f"{COMPANY} Travel and Expense Policy",
        "Document TEP-2026-01 | Effective 1 January 2026 | Owner: Finance Operations",
        TRAVEL_POLICY,
        table=PER_DIEM_TABLE,
    )
    _write_policy_pdf(
        approval_pdf,
        f"{COMPANY} Approval and Escalation Policy",
        "Document AEP-2026-01 | Effective 1 January 2026 | Owner: Finance Operations",
        APPROVAL_POLICY,
    )
    _write_scanned_receipt_pdf(scanned_pdf)
    _write_expense_workbook(workbook)
    _write_sqlite_db(sqlite_path)

    return {
        "travel_policy_pdf": travel_pdf,
        "approval_policy_pdf": approval_pdf,
        "scanned_receipt_pdf": scanned_pdf,
        "expense_workbook": workbook,
        "sqlite_db": sqlite_path,
    }
