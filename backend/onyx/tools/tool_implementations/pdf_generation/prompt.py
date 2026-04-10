PDF_GENERATION_SYSTEM_PROMPT = """\
When generating PDF content, follow these rules:
1. Write section body text in clear, professional prose. No filler phrases.
2. Every numeric claim must be specific (e.g. "43% increase" not "significant increase").
3. Bullet points must be 5–10 words each. No full sentences ending in periods.
4. Tables must have a header row. Use numbers not prose in data cells.
5. Callout boxes are for key insights or warnings only — max 2 sentences.
6. The first section is always an Executive Summary (3–5 bullet points).
7. The last section is always Next Steps or Recommendations.
8. Document title must be specific and date-stamped.
"""
