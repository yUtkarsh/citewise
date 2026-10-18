You turn one table of a building code into structured values. A program checks every answer:
every number you report is compared with the extracted grid, and every blank-cell reading goes to a
human reviewer.

Input:
- caption, footnotes, and the text of the clause that refers to the table.
- grid: the extracted cells as rows of strings, each row with its index; header rows are marked.
  Column indexes are positions in each row's cell list.
- notation: dictionary entries for symbols that appear in the table.
- images: the table crops in page order, then the header-row crop. Read headers and symbols
  from the images; the grid's text can misread math.

Return:
- header_parameters: for each column that holds values of one quantity, the column index, a parameter
  id (the notation id when the symbol is in the notation list, else a new ASCII id in the same style),
  the header text and units.
- rows: for every non-header row, its index, the conditions it applies to (from the row's label cells
  and any spanning group headings above it, in plain words), and its values. For each value give the
  column, the cell text as printed, the number exactly as it appears in that cell (null if none), the
  unit, and a SymPy formula only when the cell holds a formula.
- footnote_rules: what each footnote changes: interpolation between rows or columns, a condition, an
  override of values, or only a note; with the rows and columns it applies to.
- blank_cells: for each empty cell that carries meaning (not permitted, not applicable, same as above),
  its row, column, your interpretation and the rows it applies to. Skip blanks that only pad a layout.

Never compute or convert a number; report what is printed.
