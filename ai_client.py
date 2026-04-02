from __future__ import annotations

import json
import anthropic


PROMPT_TEMPLATE = """Jesteś ekspertem SEO dla polskich sklepów internetowych. Przeanalizuj poniższą kategorię sklepu z kawą i podaj konkretne rekomendacje.

## Dane kategorii

- **URL:** {url}
- **Obecny H1:** {h1}
- **Obecny meta title:** {meta_title}
- **Obecny meta description:** {meta_description}

## Frazy z Google Search Console (ostatnie 90 dni)

{queries_text}

## Zadanie

Na podstawie powyższych danych przygotuj rekomendacje SEO. Odpowiedz WYŁĄCZNIE w formacie JSON (bez markdown, bez komentarzy):

{{
  "h1_rekomendacja": "nowy tekst H1 (max 60 znaków, zawierający główną frazę kluczową)",
  "h1_uzasadnienie": "krótkie uzasadnienie zmiany (1-2 zdania)",
  "meta_title_rekomendacja": "nowy meta title (max 60 znaków)",
  "meta_description_rekomendacja": "nowy meta description (max 155 znaków, z CTA)",
  "frazy_h2": ["fraza 1 do H2", "fraza 2 do H2", "fraza 3 do H2", "fraza 4 do H2", "fraza 5 do H2"],
  "propozycje_podkategorii": ["podkategoria 1", "podkategoria 2", "podkategoria 3"],
  "glowna_fraza": "najważniejsza fraza kluczowa dla tej kategorii",
  "ocena_obecnego_seo": "krótka ocena obecnego stanu SEO (1-2 zdania)"
}}"""

EMPTY_REC = {
    "h1_rekomendacja": "",
    "h1_uzasadnienie": "",
    "meta_title_rekomendacja": "",
    "meta_description_rekomendacja": "",
    "frazy_h2": [],
    "propozycje_podkategorii": [],
    "glowna_fraza": "",
    "ocena_obecnego_seo": "",
}


def get_recommendations(client: anthropic.Anthropic, cat: dict) -> dict:
    top_queries = cat["queries"][:20]
    if top_queries:
        queries_text = "\n".join(
            f'  - "{q["query"]}" | kliknięcia: {q["clicks"]} | '
            f'wyświetlenia: {q["impressions"]} | pozycja: {q["position"]}'
            for q in top_queries
        )
    else:
        queries_text = "  (brak danych GSC)"

    prompt = PROMPT_TEMPLATE.format(
        url=cat["url"],
        h1=cat["h1"] or "(brak)",
        meta_title=cat["meta_title"] or "(brak)",
        meta_description=cat["meta_description"] or "(brak)",
        queries_text=queries_text,
    )

    try:
        message = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        return {**EMPTY_REC, "h1_uzasadnienie": f"Błąd API: {e}"}
