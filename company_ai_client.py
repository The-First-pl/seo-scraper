from __future__ import annotations

import json
import re

import anthropic

_PROMPT = """Jesteś ekspertem SEO dla stron firmowych (nie sklepów).
Analizujesz podstrony firmy świadczącej usługi.

Dane podstron:
{dane_podstron}

Dla każdej podstrony podaj rekomendacje w JSON:
{{
  "pages": [
    {{
      "url": "...",
      "current_h1": "...",
      "current_title": "...",
      "current_description": "...",
      "recommended_h1": "...",
      "recommended_title": "...",
      "recommended_description": "...",
      "target_phrase": "główna fraza SEO dla tej podstrony",
      "page_type": "usługa|o-nas|kontakt|realizacje|blog|inne",
      "priority": 1,
      "issues": ["brak H1", "za krótki opis", "title zbyt ogólny"],
      "reasoning": "krótkie uzasadnienie rekomendacji"
    }}
  ],
  "summary": {{
    "biggest_issues": ["lista 3 największych problemów SEO całej strony"],
    "quick_wins": ["lista 3 rzeczy do zrobienia w pierwszej kolejności"]
  }}
}}

Zasady:
- Rekomenduj frazy lokalne jeśli firma ma zasięg lokalny (np. "pompy ciepła Toruń")
- Dla podstron usługowych — frazy transakcyjne ("instalacja X", "montaż X", "cena X")
- Dla strony głównej — fraza brandowa + główna usługa
- priority: 1 = najważniejsze (krytyczne), 2 = ważne, 3 = opcjonalne
- Zwróć TYLKO JSON, bez tekstu przed ani po"""

PAGE_TYPE_LABELS = {
    "usługa":     "🔧 Usługa",
    "o-nas":      "👥 O nas",
    "kontakt":    "📞 Kontakt",
    "realizacje": "🏆 Realizacje",
    "blog":       "📝 Blog",
    "inne":       "📄 Inne",
}

EMPTY_RESULT: dict = {"pages": [], "summary": {"biggest_issues": [], "quick_wins": []}}


def get_company_recommendations(client: anthropic.Anthropic, pages: list[dict]) -> dict:
    """Send all pages to Claude in one batch call. Returns parsed JSON result."""
    lines: list[str] = []
    for p in pages:
        h2_text = ", ".join(p.get("h2s", [])) or "(brak)"
        gsc_text = ""
        if p.get("queries"):
            top = p["queries"][:10]
            gsc_text = "\n  GSC: " + "; ".join(
                f'"{q["query"]}" ({q["clicks"]} klik., poz. {q["position"]:.1f})'
                for q in top
            )
        lines.append(
            f"- URL: {p['url']}\n"
            f"  H1: {p['h1'] or '(brak)'}\n"
            f"  H2: {h2_text}\n"
            f"  Title: {p['meta_title'] or '(brak)'}\n"
            f"  Opis: {p['meta_description'] or '(brak)'}\n"
            f"  Formularz kontaktowy: {'tak' if p.get('has_form') else 'nie'}"
            + gsc_text
        )

    prompt = _PROMPT.format(dane_podstron="\n".join(lines))

    for attempt in range(2):
        retry_note = (
            "\n\nUWAGA: Poprzednia odpowiedź zawierała niepoprawny JSON. Zwróć TYLKO JSON."
            if attempt else ""
        )
        try:
            msg = client.messages.create(
                model="claude-opus-4-6",
                max_tokens=8000,
                messages=[{"role": "user", "content": prompt + retry_note}],
            )
            raw = msg.content[0].text.strip()
            fence = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
            if fence:
                raw = fence.group(1).strip()
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            if attempt == 1:
                return {**EMPTY_RESULT, "_error": "Niepoprawny JSON po 2 próbach."}
        except Exception as e:
            return {**EMPTY_RESULT, "_error": str(e)}

    return EMPTY_RESULT
