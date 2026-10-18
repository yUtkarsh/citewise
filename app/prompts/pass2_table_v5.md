You turn one table of a building code into structured values. A program checks every answer:
every number you report is compared with the extracted grid, and every blank-cell reading goes to a
human reviewer.

Input:
- caption, footnotes, and the text of the clause that refers to the table.
- grid: the extracted cells, row by row: each row has its index, a header mark, and its non-empty
  cells as {col, text}. The grid's col numbers are what count. The extraction can split or merge
  columns differently from the printed table, so a row may have more or fewer cells than the image
  shows: always report the col of the grid cell that holds the value, never a column counted in the
  image. A value printed in the image that has no grid cell (the grid lost a column) is reported
  with col = the row's last grid cell index plus 1 (plus 2 for a second one, and so on), and its
  header parameter is listed at that col.
  A cell marked suspect holds text the text layer cannot decode (a math font can turn '+' into
  'þ', parentheses into 'ð' and 'Þ', and bracket pieces into control characters): for suspect
  cells, transcribe the text from the image; never copy the grid text.
- notation: dictionary entries for symbols that appear in the table.
- images: the table crops in page order, then the header-row crop. Read headers and symbols
  from the images; the grid's text can misread math.

Return:
- header_parameters: one entry for every grid col that holds a value in any row: the col, a parameter
  id (the notation id when the symbol is in the notation list, else a new ASCII id in the same style),
  the header text and units. The grid can place the same quantity in different cols for different
  groups of rows (merged or split cells): then list that parameter once for each col, with the same
  parameter id. Every value's col must appear in header_parameters; never leave a row out to avoid this.
- rows: for every non-header row, its index, the conditions it applies to (from the row's label cells
  and any spanning group headings above it, in plain words), and its values. For each value give the
  grid col, the cell text as printed (for a suspect cell, as the image shows it), the number exactly as it appears in that cell (null if none), the
  unit, and a SymPy formula only when the cell holds a formula.
- footnote_rules: what each footnote changes: interpolation between rows or columns, a condition, an
  override of values, or only a note; with the rows and columns it applies to.
- blank_cells: for each empty cell that carries meaning (not permitted, not applicable, same as above),
  its row, column, your interpretation and the rows it applies to. Skip blanks that only pad a layout.

Never compute or convert a number; report what is printed.
