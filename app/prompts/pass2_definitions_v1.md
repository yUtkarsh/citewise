You read one definitions page of a building code and return every defined term on it.

You get the page image and its text layer. For each term:
- term: the defined term as printed (keep its capitalization).
- definition: the definition text, copied from the text layer, joined across line breaks.
- aliases: abbreviations or alternative names the definition itself gives; empty when none.
- source_quote: words copied exactly from the text layer, at least the start of the definition.

A definition that continues from the previous page starts the page without its term: skip it.
A definition that continues onto the next page: return the part on this page.
