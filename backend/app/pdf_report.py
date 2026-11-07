"""Génération de rapports PDF (reportlab, importé à la demande).

Un rapport est un titre, un sous-titre et des sections (paragraphes et tableaux). Le texte vient de la base
(titres d'événements, notes d'opérateur) : il est échappé et ramené au jeu de caractères de la police standard
(Latin-1/cp1252) pour ne jamais produire de balisage ni de caractères manquants.
"""
import io
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

FOOTER = "Rapport généré par NiifYéén : observations et estimations automatiques non vérifiées, qui ne constituent pas une preuve en soi."


class PdfUnavailable(RuntimeError):
    """reportlab n'est pas installé."""


def clean(value: Any, limit: Optional[int] = None) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").replace("→", "->").replace("—", "-").replace("–", "-") \
               .replace("’", "'").replace("…", "...").replace(" ", " ")
    text = text.encode("cp1252", "replace").decode("cp1252")
    if limit and len(text) > limit:
        text = text[:limit - 1] + "…".encode("cp1252", "replace").decode("cp1252")
    return text


def _bar_chart(bars: Dict[str, Any], width: float):
    """Histogramme simple (reportlab.graphics) : ``{"labels": [...], "values": [...]}``."""
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib import colors
    values = [max(0, v) for v in bars["values"]]
    drawing = Drawing(width, 140)
    chart = VerticalBarChart()
    chart.x, chart.y, chart.width, chart.height = 28, 24, width - 40, 100
    chart.data = [values]
    chart.categoryAxis.categoryNames = [clean(label) for label in bars.get("labels", [""] * len(values))]
    chart.categoryAxis.labels.fontSize = 6
    chart.valueAxis.valueMin = 0
    chart.valueAxis.valueMax = max(values + [1]) * 1.1
    chart.valueAxis.labels.fontSize = 6
    chart.bars[0].fillColor = colors.HexColor("#3a6ea5")
    chart.bars[0].strokeColor = None
    drawing.add(chart)
    return drawing


def build_pdf(title: str, subtitle: str, sections: Sequence[Dict[str, Any]], generated: Optional[datetime] = None) -> bytes:
    """``sections`` : liste de ``{"heading": str, "paragraphs": [str], "table": {"header": [..], "rows": [[..]], "widths": [..]}}``."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        from xml.sax.saxutils import escape
    except ImportError as exc:                      # pragma: no cover - dépend de l'installation
        raise PdfUnavailable("Export PDF indisponible : installez reportlab (pip install reportlab).") from exc

    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], fontName="Helvetica", fontSize=9.5, leading=13)
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=10)
    cell = ParagraphStyle("cell", parent=body, fontSize=7.5, leading=9)
    head = ParagraphStyle("head", parent=cell, fontName="Helvetica-Bold", textColor=colors.white)
    generated = generated or datetime.utcnow()

    def p(text, style=body):
        return Paragraph(escape(clean(text)), style)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.drawString(15 * mm, 9 * mm, clean(FOOTER)[:150])
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f"Page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=16 * mm,
                            title=clean(title), author="NiifYéén".encode("cp1252").decode("cp1252"))
    story: List[Any] = [Paragraph(escape(clean(title)), styles["Title"]), p(subtitle, small),
                        p(f"Généré le {generated:%d/%m/%Y à %H:%M} (UTC)", small), Spacer(1, 6 * mm)]
    usable = A4[0] - 30 * mm
    for sec in sections:
        if sec.get("heading"):
            story.append(Paragraph(escape(clean(sec["heading"])), styles["Heading2"]))
        for para in sec.get("paragraphs", []):
            story += [p(para), Spacer(1, 2 * mm)]
        bars = sec.get("bars")
        if bars and bars.get("values"):
            story += [_bar_chart(bars, usable), Spacer(1, 4 * mm)]
        table = sec.get("table")
        if table:
            header = [Paragraph(escape(clean(h)), head) for h in table["header"]]
            rows = [[Paragraph(escape(clean(c, 300)), cell) for c in r] for r in table["rows"]]
            widths = table.get("widths")
            widths = [usable * w / sum(widths) for w in widths] if widths else None
            t = Table([header] + rows, colWidths=widths, repeatRows=1)
            t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#33415c")),
                                   ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f4f9")]),
                                   ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c5cee0")),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
            story += [t, Spacer(1, 4 * mm)]
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
