from __future__ import annotations

import json
import re
import tempfile
from datetime import date

import anthropic
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ── Claude prompt & parsing ───────────────────────────────────────────────────

_PROMPT = """Jesteś ekspertem SEO. Otrzymujesz frazy kategorii sklepu internetowego.
Twoim zadaniem NIE jest pisanie o tych produktach — \
twoim zadaniem jest znalezienie PYTAŃ jakie zadają użytkownicy \
PRZED zakupem produktów z tych kategorii.

Frazy kategorii: {frazy}
Język: {jezyk}

Dla każdej frazy kategorii wygeneruj plan w formacie JSON:

{{
  "categories": [
    {{
      "category_phrase": "kosiarki spalinowe",
      "clusters": [
        {{
          "pillar_question": "Jaką kosiarkę spalinową kupić?",
          "pillar_phrase": "jaką kosiarkę spalinową kupić",
          "intent": "zakupowa",
          "supporting": [
            {{
              "phrase": "jaka kosiarka spalinowa do małego ogrodu",
              "intent": "zakupowa"
            }}
          ]
        }}
      ]
    }}
  ]
}}

Zasady:
- Frazy pillar to PYTANIA użytkowników, nie nazwy produktów
- Frazy supporting to zawężenia, warianty i porównania frazy pillar
- NIE twórz wpisów o samych produktach — tylko poradniki i pytania
- Każda kategoria ma 3-5 klastrów (pytań pillar)
- Każdy klaster ma 4-6 fraz wspierających
- Intencje: informacyjna, zakupowa, porównawcza, posprzedażowa
- Zwróć TYLKO JSON, bez tekstu przed ani po"""

_RETRY_SUFFIX = (
    "\n\nUWAGA: Poprzednia odpowiedź zawierała niepoprawny JSON. "
    "Zwróć TYLKO poprawny JSON, nic więcej."
)

INTENT_LABELS = {
    "informacyjna":  "ℹ️ Informacyjna",
    "zakupowa":      "🛒 Zakupowa",
    "porównawcza":   "⚖️ Porównawcza",
    "posprzedażowa": "⭐ Posprzedażowa",
}


def _parse_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if fence:
        text = fence.group(1).strip()
    return json.loads(text)


def generate_blog_plan(
    client: anthropic.Anthropic,
    frazy: str,
    jezyk: str,
) -> dict:
    """
    Sends category phrases to Claude and returns a parsed JSON plan.
    Retries once with a stricter instruction if the response isn't valid JSON.
    """
    prompt = _PROMPT.format(frazy=frazy, jezyk=jezyk)

    for attempt in range(2):
        msg = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt + (_RETRY_SUFFIX if attempt else "")}],
        )
        raw = msg.content[0].text
        try:
            return _parse_json(raw)
        except (json.JSONDecodeError, ValueError):
            if attempt == 1:
                raise ValueError(
                    f"Claude zwrócił niepoprawny JSON po 2 próbach.\n\nOdpowiedź:\n{raw[:600]}"
                )

    raise RuntimeError("unreachable")


# ── Excel export ──────────────────────────────────────────────────────────────

_CAT_FILL    = PatternFill("solid", fgColor="2E2E2E")   # dark grey  — category header
_PILLAR_FILL = PatternFill("solid", fgColor="FFF9C4")   # light yellow — pillar
_WHITE_FILL  = PatternFill("solid", fgColor="FFFFFF")
_HDR_FILL    = PatternFill("solid", fgColor="1F3864")   # nav dark blue — column headers

_CAT_FONT    = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
_HDR_FONT    = Font(name="Calibri", bold=True, color="FFFFFF", size=10)
_PILLAR_FONT = Font(name="Calibri", bold=True, size=10)
_BODY_FONT   = Font(name="Calibri", size=10)

_THIN   = Side(style="thin",   color="CCCCCC")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

COLUMNS = [
    ("Fraza kategorii",    30),
    ("Pytanie pillar",     42),
    ("Fraza wspierająca",  46),
    ("Intencja",           22),
    ("Wyszukania",         14),
    ("Status",             16),
]
N_COLS = len(COLUMNS)


def _c(ws, row: int, col: int, value="", *, font=None, fill=None, align=None, merge_end_row=None):
    cell = ws.cell(row=row, column=col, value=value)
    cell.font      = font  or _BODY_FONT
    cell.fill      = fill  or _WHITE_FILL
    cell.border    = _BORDER
    cell.alignment = align or Alignment(vertical="top", wrap_text=True)
    return cell


def export_excel(plan: dict, frazy: str) -> str:
    """Write the blog plan to a temp .xlsx file and return its path."""
    wb  = Workbook()
    ws  = wb.active
    ws.title = "Plan bloga"

    # ── Column headers ────────────────────────────────────────────────────────
    center = Alignment(horizontal="center", vertical="center")
    for ci, (label, width) in enumerate(COLUMNS, 1):
        c = ws.cell(row=1, column=ci, value=label)
        c.font      = _HDR_FONT
        c.fill      = _HDR_FILL
        c.border    = _BORDER
        c.alignment = center
        ws.column_dimensions[get_column_letter(ci)].width = width
    ws.row_dimensions[1].height = 22
    ws.freeze_panes = "A2"

    row = 2
    for cat in plan.get("categories", []):
        cat_phrase = cat.get("category_phrase", "")
        clusters   = cat.get("clusters", [])

        # Count rows this category spans: one pillar row + supporting rows per cluster
        cat_span = sum(1 + len(cl.get("supporting", [])) for cl in clusters)
        cat_first_row = row

        for cl in clusters:
            pillar_q      = cl.get("pillar_question", "")
            pillar_phrase = cl.get("pillar_phrase", "")
            pillar_intent = cl.get("intent", "")
            supporting    = cl.get("supporting", [])
            cluster_span  = 1 + len(supporting)
            pillar_row    = row

            # ── Pillar row ────────────────────────────────────────────────────
            _c(ws, row, 1, cat_phrase,
               font=_CAT_FONT, fill=_CAT_FILL,
               align=Alignment(horizontal="center", vertical="center", wrap_text=True))
            _c(ws, row, 2, pillar_q,
               font=_PILLAR_FONT, fill=_PILLAR_FILL,
               align=Alignment(vertical="center", wrap_text=True))
            _c(ws, row, 3, pillar_phrase,
               font=_PILLAR_FONT, fill=_PILLAR_FILL)
            _c(ws, row, 4,
               INTENT_LABELS.get(pillar_intent, pillar_intent),
               font=_PILLAR_FONT, fill=_PILLAR_FILL,
               align=Alignment(vertical="top", horizontal="center"))
            _c(ws, row, 5, "", fill=_PILLAR_FILL)
            _c(ws, row, 6, "", fill=_PILLAR_FILL)
            ws.row_dimensions[row].height = 32
            row += 1

            # ── Supporting rows ───────────────────────────────────────────────
            for sup in supporting:
                _c(ws, row, 1, "",  font=_CAT_FONT, fill=_CAT_FILL)  # col 1: cat colour stripe
                _c(ws, row, 2, "",  fill=_WHITE_FILL)                 # col 2: empty (pillar merged above)
                _c(ws, row, 3, sup.get("phrase", ""))
                intent_raw = sup.get("intent", "")
                _c(ws, row, 4,
                   INTENT_LABELS.get(intent_raw, intent_raw),
                   align=Alignment(vertical="top", horizontal="center", wrap_text=True))
                _c(ws, row, 5, "")
                _c(ws, row, 6, "")
                ws.row_dimensions[row].height = 24
                row += 1

            # Merge pillar question cell vertically across its supporting rows
            if cluster_span > 1:
                ws.merge_cells(
                    start_row=pillar_row,   start_column=2,
                    end_row=pillar_row + cluster_span - 1, end_column=2,
                )

        # Merge category phrase cell vertically across all its clusters
        if cat_span > 1:
            ws.merge_cells(
                start_row=cat_first_row, start_column=1,
                end_row=cat_first_row + cat_span - 1, end_column=1,
            )

    # ── Summary sheet ─────────────────────────────────────────────────────────
    ws2 = wb.create_sheet("Podsumowanie")
    sum_cols = [("Fraza kategorii", 32), ("Klastrów (pillar)", 18), ("Fraz supporting", 18), ("Razem fraz", 14)]
    for ci, (label, width) in enumerate(sum_cols, 1):
        c = ws2.cell(row=1, column=ci, value=label)
        c.font = _HDR_FONT; c.fill = _HDR_FILL; c.border = _BORDER
        c.alignment = Alignment(horizontal="center", vertical="center")
        ws2.column_dimensions[get_column_letter(ci)].width = width
    ws2.row_dimensions[1].height = 22
    ws2.freeze_panes = "A2"

    total_pillars = total_sup = 0
    for ri, cat in enumerate(plan.get("categories", []), 2):
        clusters   = cat.get("clusters", [])
        n_pillars  = len(clusters)
        n_sup      = sum(len(cl.get("supporting", [])) for cl in clusters)
        total_pillars += n_pillars
        total_sup     += n_sup

        for ci, val in enumerate([cat.get("category_phrase", ""), n_pillars, n_sup, n_pillars + n_sup], 1):
            c = ws2.cell(row=ri, column=ci, value=val)
            c.font = _BODY_FONT; c.border = _BORDER
            c.alignment = Alignment(horizontal="center" if ci > 1 else "left", vertical="top", wrap_text=True)

    tr = len(plan.get("categories", [])) + 2
    for ci, val in enumerate(["RAZEM", total_pillars, total_sup, total_pillars + total_sup], 1):
        c = ws2.cell(row=tr, column=ci, value=val)
        c.font = _PILLAR_FONT; c.fill = _PILLAR_FILL; c.border = _BORDER
        c.alignment = Alignment(horizontal="center" if ci > 1 else "left")

    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    wb.save(tmp.name)
    tmp.close()
    return tmp.name


def make_filename(frazy: str) -> str:
    """Generate a human-readable filename from the category phrases."""
    first_phrase = frazy.split(",")[0].strip()
    slug = re.sub(r"[^\w]", "_", first_phrase)[:35].strip("_").lower()
    return f"plan_bloga_{slug}_{date.today().isoformat()}.xlsx"
