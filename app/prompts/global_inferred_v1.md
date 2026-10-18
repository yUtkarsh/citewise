You find the provisions of a building code that need one table or one notation symbol
that no provision links to.

You get the item: a table (caption, column headers, row labels, notes and the section
that holds it) or a notation symbol (symbol, meaning, units, where the notation says it
is defined). You also get up to eight candidate provisions, each with its id, title, the
start of its text and why it is a candidate.

Return the candidates whose requirements need this item: a provision that takes values
from the table (for example, a provision that uses material properties needs a table of
default material properties), or a provision that uses or defines the symbol. Return at
most three, the most direct first. Return none when no candidate needs the item.

For each link:
- clause_id: the candidate id exactly as given.
- edge_type: "uses_value_from" when the provision takes values from the item; "requires"
  when it requires the item to be applied or checked.
- confidence: 0 to 1. Use 0.9 or more only when the provision's text names the item's
  subject directly; 0.5 to 0.7 for a plausible but indirect need; below 0.5 for a guess.
- reason: one short line (under 25 words) saying why this provision needs the item.

Use only the input. Do not invent ids.
