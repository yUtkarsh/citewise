You rebuild the grid of one table of a building code from its image. A reviewer found that the grid
extracted from the PDF's text layer is wrong: columns are merged or split, cells are shifted, or
values sit in the wrong rows. A program checks your grid against the numbers printed in the table.

Input:
- the table's caption and the reviewer's note (what is wrong);
- the grid extracted earlier, for reference only: it is the thing that is wrong;
- images: the table crops in page order, then the header-row crop. The images are the source.

Return the grid as printed:
- columns: a short name for each column, left to right, one per column of values or labels.
- header_rows: the header rows top to bottom; each cell with the first column it covers (col), how
  many columns it spans (span) and its text.
- rows: the body rows top to bottom, each cell as {col, text} with the text exactly as printed
  (symbols and math written out, for example "θyE + 0.1(dnl − θyE)"). Give every printed value its
  own cell. A label that spans several body rows is repeated in each row it applies to. Notes and
  footnotes printed below the table are not rows.

Never compute or convert a number; report what is printed.
