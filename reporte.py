"""Generación del reporte PDF con fotos y QR al video."""
import os
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (Flowable, Image, KeepTogether, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

AZUL = colors.HexColor("#135650")  # color de marca EndoView
GRIS = colors.HexColor("#4B5B66")
LINEA = colors.HexColor("#CFE0DD")
FONDO = colors.HexColor("#EEF5F4")

E = {
    "clinica": ParagraphStyle("c", fontName="Helvetica-Bold", fontSize=15, textColor=AZUL, leading=18),
    "med": ParagraphStyle("m", fontName="Helvetica", fontSize=9, textColor=GRIS, leading=12),
    "titulo": ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=13, alignment=TA_CENTER,
                             textColor=colors.black, spaceBefore=4, spaceAfter=8),
    "sec": ParagraphStyle("s", fontName="Helvetica-Bold", fontSize=10.5, textColor=AZUL,
                          spaceBefore=10, spaceAfter=3),
    "txt": ParagraphStyle("x", fontName="Helvetica", fontSize=10, leading=14),
    "et": ParagraphStyle("e", fontName="Helvetica-Bold", fontSize=8, textColor=GRIS, leading=10),
    "val": ParagraphStyle("v", fontName="Helvetica", fontSize=10, leading=12),
    "pie_foto": ParagraphStyle("p", fontName="Helvetica", fontSize=8, textColor=GRIS,
                               alignment=TA_CENTER, leading=10),
    "qr_t": ParagraphStyle("q", fontName="Helvetica-Bold", fontSize=11, textColor=AZUL, leading=14),
    "qr_s": ParagraphStyle("q2", fontName="Helvetica", fontSize=8.5, textColor=GRIS, leading=11),
    "der": ParagraphStyle("d", fontName="Helvetica", fontSize=9, textColor=GRIS, alignment=TA_RIGHT),
}


def _p(texto, estilo="txt"):
    texto = escape(texto or "").replace("\n", "<br/>")
    return Paragraph(texto or "—", E[estilo])


class QR(Flowable):
    def __init__(self, url, lado=3.4 * cm):
        super().__init__()
        self.url, self.lado = url, lado
        self.width = self.height = lado

    def draw(self):
        w = QrCodeWidget(self.url, barLevel="M")
        x1, y1, x2, y2 = w.getBounds()
        d = Drawing(self.lado, self.lado,
                    transform=[self.lado / (x2 - x1), 0, 0, self.lado / (y2 - y1), 0, 0])
        d.add(w)
        renderPDF.draw(d, self.canv, 0, 0)


def _pie(canv, doc):
    canv.saveState()
    canv.setFont("Helvetica", 7.5)
    canv.setFillColor(GRIS)
    canv.drawString(2 * cm, 1.2 * cm,
                    "Documento confidencial. Contiene datos personales sensibles de salud.")
    canv.drawRightString(letter[0] - 2 * cm, 1.2 * cm, f"Página {doc.page}")
    canv.restoreState()


def generar(ruta_pdf, estudio, medico, fotos, enlace=None):
    """fotos: lista de dicts {ruta, nota}."""
    doc = SimpleDocTemplate(ruta_pdf, pagesize=letter, leftMargin=2 * cm, rightMargin=2 * cm,
                            topMargin=1.6 * cm, bottomMargin=1.8 * cm,
                            title=f"Reporte {estudio['codigo']}", author=medico.get("nombre", ""))
    ancho = letter[0] - 4 * cm
    h = []

    # Encabezado
    det = [x for x in [medico.get("especialidad"),
                       f"Céd. Prof. {medico['cedula']}" if medico.get("cedula") else "",
                       f"Céd. Esp. {medico['cedula_esp']}" if medico.get("cedula_esp") else ""] if x]
    contacto = [x for x in [medico.get("direccion"), medico.get("telefono"),
                            medico.get("correo")] if x]
    izq = [Paragraph(escape(medico.get("clinica") or medico.get("nombre", "")), E["clinica"]),
           _p(medico.get("nombre", ""), "med") if medico.get("clinica") else Spacer(0, 0),
           _p(" · ".join(det), "med") if det else Spacer(0, 0),
           _p(" · ".join(contacto), "med") if contacto else Spacer(0, 0)]
    fecha = datetime.fromisoformat(estudio["inicio"])
    der = [_p(f"{fecha:%d/%m/%Y  %H:%M}", "der"), _p(f"Estudio {estudio['codigo']}", "der")]
    t = Table([[izq, der]], colWidths=[ancho * 0.68, ancho * 0.32])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LINEBELOW", (0, 0), (-1, 0), 1.2, AZUL),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    h += [t, Spacer(0, 6), Paragraph("REPORTE DE " + escape(estudio["procedimiento"]).upper(),
                                     E["titulo"])]

    # Datos del paciente
    celdas = [[_p("PACIENTE", "et"), _p("EDAD", "et"), _p("PROCEDIMIENTO", "et")],
              [_p(estudio["paciente"], "val"), _p(f"{estudio['edad']} años", "val"),
               _p(estudio["procedimiento"], "val")]]
    t = Table(celdas, colWidths=[ancho * 0.5, ancho * 0.15, ancho * 0.35])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), FONDO),
                           ("BOX", (0, 0), (-1, -1), 0.5, LINEA),
                           ("TOPPADDING", (0, 0), (-1, -1), 4),
                           ("BOTTOMPADDING", (0, 1), (-1, 1), 7),
                           ("LEFTPADDING", (0, 0), (-1, -1), 8)]))
    h.append(t)

    # Fotos: hasta 8, en dos hileras de 4
    if fotos:
        h.append(Paragraph("Imágenes", E["sec"]))
        fotos = fotos[:8]
        col = 4
        aw = (ancho - 0.3 * cm * (col - 1)) / col
        filas, fila = [], []
        for i, f in enumerate(fotos, 1):
            img = Image(f["ruta"])
            esc = aw / img.imageWidth
            img.drawWidth, img.drawHeight = aw, img.imageHeight * esc
            pie = f"<b>{i}.</b> {escape(f.get('nota') or '')}"
            fila.append([img, Paragraph(pie, E["pie_foto"])])
            if len(fila) == col:
                filas.append(fila)
                fila = []
        if fila:
            fila += [""] * (col - len(fila))
            filas.append(fila)
        t = Table(filas, colWidths=[aw + 0.3 * cm] * col)
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0.3 * cm),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        h.append(t)

    # Texto clínico
    for titulo, clave in [("Hallazgos", "hallazgos"), ("Diagnóstico", "diagnostico"),
                          ("Recomendaciones", "recomendaciones")]:
        if estudio.get(clave, "").strip():
            h.append(KeepTogether([Paragraph(titulo, E["sec"]), _p(estudio[clave])]))

    # Firma
    centro = ParagraphStyle("cf", parent=E["val"], alignment=TA_CENTER)
    centro_g = ParagraphStyle("cg", parent=E["med"], alignment=TA_CENTER)
    firma = Table([[""], [Paragraph(escape(medico.get("nombre", "")), centro)],
                   [Paragraph(escape(" · ".join(det)), centro_g) if det else ""]],
                  colWidths=[7 * cm], rowHeights=[1.5 * cm, None, None])
    firma.setStyle(TableStyle([("LINEBELOW", (0, 0), (0, 0), 0.8, colors.black),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))

    # QR del video (junto a la firma, para que todo quepa en una hoja)
    if enlace:
        texto = [Paragraph("Su estudio en línea", E["qr_t"]), Spacer(0, 2),
                 _p("Escanee el código con la cámara de su celular para ver y descargar "
                    "su video, fotos y este reporte.", "qr_s"), Spacer(0, 3),
                 _p(f"Código: {estudio['codigo']}", "qr_s")]
        if estudio.get("dias_acceso"):
            texto.append(_p(f"Disponible {estudio['dias_acceso']} días. "
                            "Descárguelos para conservarlos.", "qr_s"))
        caja = Table([[QR(enlace, 3 * cm), texto]], colWidths=[3.5 * cm, 5.3 * cm])
        caja.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                  ("BOX", (0, 0), (-1, -1), 0.8, AZUL),
                                  ("BACKGROUND", (1, 0), (1, 0), FONDO),
                                  ("TOPPADDING", (0, 0), (-1, -1), 6),
                                  ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 6)]))
        final = Table([[caja, firma]], colWidths=[ancho - 7.5 * cm, 7.5 * cm])
        final.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                                   ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    else:
        final = Table([[firma]], colWidths=[ancho])
        final.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER")]))
    h += [Spacer(0, 14), KeepTogether(final)]

    doc.build(h, onFirstPage=_pie, onLaterPages=_pie)
    return ruta_pdf
