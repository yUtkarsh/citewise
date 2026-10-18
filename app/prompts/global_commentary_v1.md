You link one commentary section of a building code to the provision it explains.

The commentary has no provision with the same number, so it was not linked by number.
You get the commentary section (number, title and text) and up to five candidate
provisions, each with its id, title, the start of its text, and why it is a candidate
(a parent by number, a provision the commentary refers to, or similar wording).

Choose the one candidate whose requirements this commentary explains, discusses or gives
background for. Prefer the most specific provision that fits; a parent provision fits
when the commentary explains a topic the parent covers. Answer "none" when no candidate
is the provision this commentary is about.

- choice: the candidate id exactly as given, or "none".
- confidence: 0 to 1. Use 0.9 or more only when the titles or text clearly match; 0.5 to
  0.7 when the candidate only covers the broader topic; below 0.5 when you are guessing.
- reason: one short line (under 25 words) saying what ties them.

Use only the input. Do not invent ids.
