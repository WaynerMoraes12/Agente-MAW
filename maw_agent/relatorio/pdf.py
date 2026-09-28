"""HTML (Jinja2) → PDF pelo Edge do Windows (Playwright, canal msedge) + anexo JSON (pypdf)."""
from __future__ import annotations
import io
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright
from pypdf import PdfReader, PdfWriter

from .. import sandbox

_MODELOS = Path(__file__).parent / "modelos"
_ROTULOS = {"passou": "✓", "falhou": "✗", "nao_testavel": "–", "na": "n/a"}


def _html(ctx: dict) -> str:
    env = Environment(loader=FileSystemLoader(_MODELOS), autoescape=select_autoescape(["html", "j2"]))
    env.filters["simbolo"] = lambda r: _ROTULOS.get(r, "?")
    css = (_MODELOS / "estilo.css").read_text(encoding="utf-8")
    return env.get_template("relatorio.html.j2").render(**ctx, css=css)


def renderizar(ctx: dict, destino: Path) -> Path:
    destino = sandbox.garantir_escrita(Path(destino))
    html = _html(ctx)
    rodape = ('<div style="font-size:8px;width:100%;padding:0 12mm;color:#666;display:flex;justify-content:space-between">'
              f'<span>MAW — Relatório de testes · {ctx["sprint"]}</span>'
              '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>')
    with sync_playwright() as pw:
        nav = pw.chromium.launch(channel="msedge")
        try:
            pagina = nav.new_page()
            pagina.set_content(html, wait_until="load")
            dados = pagina.pdf(format="A4", print_background=True, display_header_footer=True,
                               header_template="<span></span>", footer_template=rodape,
                               margin={"top": "14mm", "bottom": "16mm", "left": "12mm", "right": "12mm"},
                               prefer_css_page_size=True)
        finally:
            nav.close()
    sandbox.escrever_bytes(destino, dados)
    return destino


def anexar(pdf: Path, arquivo: Path, nome: str) -> None:
    # lido para a memória: no Windows um PdfReader sobre o caminho segura o arquivo aberto.
    # O novo PDF também é montado inteiro em memória antes de tocar o disco: escrever_bytes
    # grava num arquivo temporário e só troca com os.replace no final, então uma falha aqui
    # nunca apaga o PDF original.
    escritor = PdfWriter(clone_from=PdfReader(io.BytesIO(Path(pdf).read_bytes())))
    escritor.add_attachment(nome, Path(arquivo).read_bytes())
    buf = io.BytesIO()
    escritor.write(buf)
    sandbox.escrever_bytes(Path(pdf), buf.getvalue())


def ler_anexo(pdf: Path, nome: str) -> bytes:
    anexos = PdfReader(io.BytesIO(Path(pdf).read_bytes())).attachments
    if nome not in anexos:
        raise KeyError(f"{nome} não está anexado a {pdf}")
    return anexos[nome][0]
