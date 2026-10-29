"""
Generic polars-DataFrame to LaTeX booktabs table converter.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from dv_tracking.utils import escape_latex, format_latex_number, latex_column_spec

DEFAULT_DECIMALS: int = 1
MISSING_CELL: str = "--"


class LatexTable:
    """A polars DataFrame rendered as a LaTeX booktabs table."""

    def __init__(
        self,
        frame: pl.DataFrame,
        *,
        caption: str,
        label: str,
        alignments: tuple[str, ...] | None = None,
        column_formats: dict[str, int] | None = None,
        header_overrides: dict[str, str] | None = None,
        preamble: str | None = None,
    ) -> None:
        """
        Binds the table to a frame and its rendering configuration.

        Args:
            frame: The DataFrame to render, one output row per frame row.
            caption: The table caption.
            label: The table label for cross-referencing.
            alignments: One alignment letter per column; inferred from dtype when None.
            column_formats: Per-column decimal places for float columns; DEFAULT_DECIMALS otherwise.
            header_overrides: Display headers keyed by column name; the column name is used otherwise.
            preamble: Text emitted before the table environment (e.g. provenance comments), or None.
        Returns:
            None
        """
        self.frame = frame
        self.caption = caption
        self.label = label
        self.alignments = alignments
        self.column_formats = column_formats or {}
        self.header_overrides = header_overrides or {}
        self.preamble = preamble

    def column_alignments(self) -> tuple[str, ...]:
        """
        The per-column alignment letters, explicit or inferred from dtype.

        Args:
            None
        Returns:
            alignments: The configured alignments, else "r" for numeric columns and "l" otherwise.
        """
        if self.alignments is not None:
            return self.alignments
        return tuple(
            "r" if (dtype.is_float() or dtype.is_integer()) else "l"
            for dtype in self.frame.schema.values()
        )

    def header_labels(self) -> list[str]:
        """
        The escaped header labels, with any overrides applied.

        Args:
            None
        Returns:
            labels: One escaped header per column.
        """
        return [
            escape_latex(self.header_overrides.get(column, column)) for column in self.frame.columns
        ]

    def render_cell(self, column: str, value: object) -> str:
        """
        Renders one cell value for its column.

        Args:
            column: The column the value belongs to.
            value: The cell value.
        Returns:
            cell: MISSING_CELL for None, a fixed-point number for floats, a bare
                integer for integers, else the escaped string form.
        """
        if value is None:
            return MISSING_CELL
        dtype = self.frame.schema[column]
        if dtype.is_float():
            decimals = self.column_formats.get(column, DEFAULT_DECIMALS)
            return format_latex_number(float(value), decimals)
        if dtype.is_integer():
            return str(int(value))
        return escape_latex(str(value))

    def body_rows(self) -> list[str]:
        """
        The rendered table body, one LaTeX row string per frame row.

        Args:
            None
        Returns:
            rows: Each row's cells joined by " & " and terminated with a row break.
        """
        columns = self.frame.columns
        rows = []
        for record in self.frame.iter_rows(named=True):
            cells = [self.render_cell(column, record[column]) for column in columns]
            rows.append(" & ".join(cells) + r" \\")
        return rows

    def to_latex(self) -> str:
        """
        Renders the full LaTeX table environment.

        Args:
            None
        Returns:
            latex: The complete table environment with booktabs rules.
        """
        header = " & ".join(self.header_labels()) + r" \\"
        lines = [
            *([self.preamble] if self.preamble else []),
            r"\begin{table}[htbp]",
            r"\centering",
            rf"\caption{{{self.caption}}}",
            rf"\label{{{self.label}}}",
            rf"\begin{{tabular}}{{{latex_column_spec(self.column_alignments())}}}",
            r"\toprule",
            header,
            r"\midrule",
            *self.body_rows(),
            r"\bottomrule",
            r"\end{tabular}",
            r"\end{table}",
        ]
        return "\n".join(lines) + "\n"

    def write(self, path: Path) -> Path:
        """
        Writes the rendered table to a path.

        Args:
            path: The file path to write.
        Returns:
            path: The path written.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_latex(), encoding="utf-8")
        return path
