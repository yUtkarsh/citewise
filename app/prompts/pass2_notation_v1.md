You read one notation (symbols) page of a building code and return every symbol it defines.

You get the page image and the page's text layer. The text layer can misread math: primes, subscripts,
Greek letters and radicals may come out as wrong letters, private-use characters or ligatures. Read
symbols from the image; copy wording from the text layer.

For each entry:
- display: the symbol as printed, in Unicode.
- latex: LaTeX for the symbol.
- id: a stable ASCII identifier that is also a valid Python name. Spell primes as "prime" and keep
  subscripts after an underscore (for example f_prime_cE, V_d, Q_CE). The same symbol always gets the
  same id. If the page lists one symbol twice with different meanings, give the two entries different
  ids (add a short suffix) and fill scope_chapters for each.
- description and units as printed (units null when none are given).
- defined_in: the places the entry says the symbol is defined or used, exactly as printed
  (for example "Section 2.4.1", "Equation 3-1"); empty when none are printed.
- aliases: other spellings of the symbol you can see in the text layer for this entry (the garbled
  text-layer form included), so the symbol can be found in clause text.
- source_quote: words copied exactly from the text layer for this entry, at least the description.

Return only entries printed on this page. Do not invent units or chapters.
