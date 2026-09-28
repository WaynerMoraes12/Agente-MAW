"""Motor dos cenários E2E: descoberta em `privado/cenarios/**/*.py`, `Contexto`, `Resultado` e
execução com timeout e recuperação de exceção/travamento. Nunca abre a MAW diretamente — quem faz
isso é `SessaoApp` (Task 3, `maw_agent.gui`), importado só quando um cenário chama `ctx.maw()`.
"""
from __future__ import annotations
import contextvars
import dataclasses
import hashlib
import importlib.util
import sys
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import sandbox

ESTADOS_RESULTADO = ("passou", "problema", "pulei")


@dataclass
class Resultado:
    """O que um cenário devolve. `severidade` só importa quando `estado == "problema"`
    (achado bruto): None vira "media". `limitacoes` é só de `rodar_cenario` (nunca do próprio
    cenário — por isso é `kw_only` e `rodar_cenario` só a toca via `dataclasses.replace`): erro do
    agente ou thread que não terminou a tempo; quem chama (a fase `e2e`) só precisa juntar essas
    linhas em `limitacoes-e2e.json`. Um cenário que preencher `limitacoes` por conta própria não
    trava nada — `rodar_cenario` prefixa essas linhas com "(do cenário) " para deixar a origem
    clara, mas nunca as descarta."""
    estado: str
    nota: str = ""
    esperado: str = ""
    obtido: str = ""
    passos: list[str] = field(default_factory=list)
    severidade: str | None = None
    limitacoes: list[str] = field(default_factory=list, kw_only=True)

    def __post_init__(self) -> None:
        if self.estado not in ESTADOS_RESULTADO:
            raise ValueError(f"estado de Resultado inválido: {self.estado!r} "
                             f"(use {', '.join(ESTADOS_RESULTADO)})")


@dataclass
class Contexto:
    """Tudo que um cenário recebe. `.maw()` e `.audio` importam os módulos das Tasks 3 e 5 só na
    hora de usar, para que importar `maw_agent.e2e` nunca precise deles instalados.

    `estado`: o `estado.Estado` da sprint em andamento (o mesmo objeto que a fase `e2e` já tem em
    mãos). `.maw()` repassa para `SessaoApp(..., estado=...)` para que o registro da restauração
    do `%APPDATA%\\MAW` caia no MESMO objeto que a fase salva depois — senão a sessão registraria
    num `Estado` recarregado à parte, e o próximo `e.concluir()`/`e.salvar()` da fase sobrescreveria
    esse registro no disco sem nunca tê-lo visto."""
    alvo: str
    exe_release: Path
    pasta_alvo: Path
    fixtures: Path
    evidencias: Path
    estado: object = None
    sessao_atual: object | None = field(default=None, init=False, repr=False)
    evidencias_registradas: list[dict] = field(default_factory=list, init=False, repr=False)

    @property
    def audio(self):
        from . import audio  # Task 5 — só existe quando o cenário efetivamente usa áudio
        return audio

    def maw(self, **kw):
        from . import gui  # Task 3 — só existe quando o cenário efetivamente abre a MAW
        kw.setdefault("estado", self.estado)
        sessao = gui.SessaoApp(self.exe_release, self.evidencias, **kw)
        self.sessao_atual = sessao
        return sessao

    def registrar_evidencia(self, arquivo: Path, legenda: str, embutir: bool = False) -> None:
        self.evidencias_registradas.append({"arquivo": str(arquivo), "legenda": legenda, "embutir": embutir})


@dataclass
class Cenario:
    id: str
    func: Callable[[Contexto], Resultado]
    bancada: list[str] = field(default_factory=list)
    itens: list[str] = field(default_factory=list)
    requisitos: list[str] = field(default_factory=list)
    alvos: str | list[str] = "todos"
    timeout: float = 300
    arquivo: Path | None = None


def cenario_mira_main(c: Cenario) -> bool:
    """A bancada é sobre o alvo `main`: um cenário mira main quando roda em 'todos' os alvos ou
    quando `main` está explicitamente na sua lista de alvos."""
    return c.alvos == "todos" or "main" in c.alvos


# ---------- decorator + descoberta ----------

# coletor da chamada de `descobrir()` em andamento; None fora dela (o decorator então só anota a
# função, sem registrar em lugar nenhum — útil para quem importa um módulo de cenário direto).
_coletor: contextvars.ContextVar[list[Cenario] | None] = contextvars.ContextVar("_coletor_e2e", default=None)


def cenario(id: str, bancada: list[str] | None = None, itens: list[str] | None = None,
           requisitos: list[str] | None = None, alvos: str | list[str] = "todos", timeout: float = 300):
    """Decorator que marca `def cenario(ctx: Contexto) -> Resultado` como um cenário E2E."""
    def decorador(func: Callable[[Contexto], Resultado]) -> Callable[[Contexto], Resultado]:
        info = Cenario(id=id, func=func, bancada=list(bancada or []), itens=list(itens or []),
                       requisitos=list(requisitos or []),
                       alvos=alvos if alvos == "todos" else list(alvos), timeout=timeout)
        func.cenario_e2e = info
        lote = _coletor.get()
        if lote is not None:
            lote.append(info)
        return func
    return decorador


def _importar_arquivo(caminho: Path):
    # nome sintético e único: dois arquivos de cenário nunca colidem no sys.modules, e reimportar
    # a mesma descoberta não reaproveita um módulo antigo por engano.
    nome = "maw_agent._cenario_" + hashlib.sha1(str(Path(caminho).resolve()).encode()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(nome, caminho)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nome] = modulo
    try:
        spec.loader.exec_module(modulo)
    except BaseException:
        sys.modules.pop(nome, None)
        raise
    return modulo


def descobrir(raiz: Path) -> list[Cenario]:
    """Importa cada `*.py` de `raiz` (recursivo; ignora nomes começando com `_`) e devolve os
    cenários que os `@cenario` desses arquivos registraram, ordenados por (arquivo, id) — a ordem
    em que a fase `e2e` roda."""
    raiz = Path(raiz)
    if not raiz.is_dir():
        return []
    encontrados: list[Cenario] = []
    for arquivo in sorted(raiz.rglob("*.py")):
        if arquivo.name.startswith("_"):
            continue
        token = _coletor.set([])
        try:
            _importar_arquivo(arquivo)
            novos = _coletor.get()
        finally:
            _coletor.reset(token)
        for c in novos:
            c.arquivo = arquivo
            encontrados.append(c)
    return sorted(encontrados, key=lambda c: (str(c.arquivo), c.id))


# ---------- execução com timeout e recuperação ----------

def _rodar_em_thread(func: Callable[[Contexto], Resultado], ctx: Contexto, timeout: float
                     ) -> tuple[str, Resultado | None, str | None, BaseException | None, threading.Thread]:
    """('ok', resultado, None, None, thread) | ('excecao', None, traceback, excecao, thread) |
    ('timeout', None, None, None, thread).

    A thread é `daemon`: se o cenário travar de verdade, ela morre com o processo em vez de
    prender a fase inteira — o travamento vira 'problema' e a fase segue para o próximo cenário.
    A thread devolvida ainda pode estar viva (timeout): quem chama decide quanto mais esperar."""
    caixa: dict = {}

    def alvo() -> None:
        try:
            caixa["resultado"] = func(ctx)
        except BaseException as ex:
            caixa["excecao"] = traceback.format_exc()
            caixa["excecao_obj"] = ex

    t = threading.Thread(target=alvo, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return "timeout", None, None, None, t
    if "excecao" in caixa:
        return "excecao", None, caixa["excecao"], caixa["excecao_obj"], t
    return "ok", caixa.get("resultado"), None, None, t


def _entrada_real_necessaria(excecao: BaseException | None) -> bool:
    """`gui.EntradaRealNecessaria` (Task 3) importado só na hora: `gui` pode nem existir. Se o
    import falhar, cai para casar pelo nome da classe — o melhor que dá sem a classe real."""
    if excecao is None:
        return False
    try:
        from . import gui
        return isinstance(excecao, gui.EntradaRealNecessaria)
    except ImportError:
        return type(excecao).__name__ == "EntradaRealNecessaria"


def _som_necessario(excecao: BaseException | None) -> bool:
    """`gui.SomNecessario`: a ação faria o Windows tocar som (só à noite, com MAW_AGENTE_SOM=1)."""
    return excecao is not None and type(excecao).__name__ == "SomNecessario"


def _app_travou(excecao: BaseException | None) -> bool:
    """`gui.AppTravou`: a MAW parou de responder ou fechou sozinha no meio do cenário — é observação
    sobre a MAW (achado), não erro do agente. Casa pela hierarquia de nomes para não importar `gui`."""
    return excecao is not None and any(k.__name__ == "AppTravou" for k in type(excecao).__mro__)


def _situacao_da_maw(ctx: Contexto) -> str:
    """No tempo esgotado: 'sem_sessao' (a MAW nem abriu), 'fechou', 'nao_responde' ou 'respondendo'.
    Nunca levanta: na dúvida, 'respondendo' (o que não gera achado contra a MAW)."""
    sessao = ctx.sessao_atual
    if sessao is None:
        return "sem_sessao"
    try:
        if not sessao.viva():
            return "fechou"
        return "respondendo" if sessao.respondendo() else "nao_responde"
    except Exception:
        return "respondendo"


def _app_do_usuario(ctx: Contexto) -> dict | None:
    """A sessão foi encerrada porque o usuário abriu a MAW durante o teste (`SessaoApp.maw_estrangeira`)."""
    v = getattr(ctx.sessao_atual, "maw_estrangeira", None)
    return v if isinstance(v, dict) else None


def _esperando_trava(ctx: Contexto) -> bool:
    """A sessão ainda esperava outra sessão do agente soltar a MAW (`SessaoApp.esperando_trava`)."""
    return getattr(ctx.sessao_atual, "esperando_trava", False) is True


def _forcar_fechamento(ctx: Contexto) -> None:
    """Se o cenário travado tinha uma SessaoApp aberta, mata o processo para não prender a
    instância única da MAW para o próximo cenário. Best-effort: nunca levanta."""
    sessao = ctx.sessao_atual
    if sessao is None:
        return
    for nome_metodo in ("matar", "fechar_forcado", "forcar_fechamento"):
        metodo = getattr(sessao, nome_metodo, None)
        if callable(metodo):
            try:
                metodo()
            except Exception:
                pass
            return


def _evidencia_ultima_captura(ctx: Contexto) -> None:
    """Melhor esforço: se a sessão sabe tirar print (`SessaoApp.captura`), guarda o estado da tela
    no momento do tempo esgotado. Bônus só — nunca levanta, e o achado de timeout já se sustenta
    sozinho sem essa evidência."""
    sessao = ctx.sessao_atual
    if sessao is None:
        return
    tirar_captura = getattr(sessao, "captura", None)
    if not callable(tirar_captura):
        return
    try:
        caminho = tirar_captura("tempo_esgotado")
        if caminho:
            ctx.registrar_evidencia(caminho, "última captura antes do tempo esgotado", False)
    except Exception:
        pass


def _resumo_traceback(texto: str) -> str:
    linhas = [l for l in texto.strip().splitlines() if l.strip()]
    return linhas[-1].strip() if linhas else "exceção sem detalhe"


def _registrar_evidencia_traceback(ctx: Contexto, texto: str) -> None:
    try:
        caminho = sandbox.escrever_texto(Path(ctx.evidencias) / "traceback.txt", texto)
        ctx.registrar_evidencia(caminho, "traceback da exceção", False)
    except Exception:
        pass  # EscritaProibida (não é OSError) ou qualquer outra falha: sem evidência de sobra,
              # mas o achado ainda sai com o resumo em `nota`/`limitacoes`


def rodar_cenario(c: Cenario, ctx: Contexto, requisitos_ausentes: set[str] | frozenset[str] = frozenset(),
                  *, tempo_apos_forcar: float = 10) -> Resultado:
    """Roda um cenário já filtrado para este alvo: `pulei` sem rodar se falta requisito; senão roda
    em uma thread com o timeout do cenário. Nunca propaga: quem chama sempre recebe um `Resultado`.

    Erro do agente — exceção no cenário, cenário que não devolve um `Resultado`, ou `problema` sem
    `obtido` (sem evidência do defeito) — nunca vira achado contra a MAW: some para `pulei` com a
    nota começando em "erro do agente..." e uma linha em `Resultado.limitacoes` (a fase `e2e` junta
    isso em `limitacoes-e2e.json`; nunca nas fichas). `gui.EntradaRealNecessaria` é uma exceção à
    exceção: não é erro do agente nem acaba em limitação — é o cenário dizendo, do jeito certo, que
    só dá para rodar com teclado/mouse reais (execução noturna); vira `pulei` com nota própria e
    sem linha de limitação. Só o tempo esgotado de verdade continua `problema`: a MAW não respondeu
    a tempo, e isso é uma observação válida sobre ela — depois de tentar fechar a sessão à força,
    ainda dá até `tempo_apos_forcar` segundos de cortesia para a thread terminar sozinha; se não
    terminar, isso também vira uma linha de limitação (a thread ainda está viva, presa, e a fase
    segue sem esperar mais)."""
    faltando = sorted(r for r in c.requisitos if r in requisitos_ausentes)
    if faltando:
        return Resultado("pulei", nota=f"requisito(s) ausente(s): {', '.join(faltando)}")
    situacao, resultado, detalhe, excecao, thread = _rodar_em_thread(c.func, ctx, c.timeout)
    if situacao == "timeout":
        esperando = _esperando_trava(ctx)  # antes de forçar: matar() cancela a espera
        maw = "sem_sessao" if esperando else _situacao_da_maw(ctx)
        if not esperando:
            _evidencia_ultima_captura(ctx)
        _forcar_fechamento(ctx)
        limitacoes = []
        thread.join(tempo_apos_forcar)
        if thread.is_alive():
            limitacoes.append(f"thread do cenário '{c.id}' ({ctx.alvo}) não terminou depois do tempo "
                              f"esgotado (nem com mais {tempo_apos_forcar:g}s de cortesia após o fechamento forçado)")
        if esperando:  # a MAW nem chegou a abrir: não é observação sobre ela
            return Resultado("pulei", nota="a MAW estava ocupada por outra sessão do agente",
                             limitacoes=[f"cenário '{c.id}' ({ctx.alvo}) não rodou: a MAW ficou ocupada por outra "
                                         f"sessão do agente durante os {c.timeout:g}s do cenário", *limitacoes])
        if maw in ("nao_responde", "fechou"):  # observação sobre a MAW: achado
            obtido = ("a MAW parou de responder" if maw == "nao_responde" else "a MAW fechou sozinha")
            return Resultado("problema", nota="tempo esgotado",
                             esperado="a MAW continua aberta e respondendo durante o cenário",
                             obtido=f"{obtido}; tempo esgotado depois de {c.timeout:g}s",
                             severidade="alta", limitacoes=limitacoes)
        # a MAW respondia (ou nem abriu): o lento foi o cenário/driver do agente — não é achado
        motivo = ("a MAW nem chegou a abrir" if maw == "sem_sessao" else "com a MAW respondendo")
        return Resultado("pulei", nota=f"o cenário não terminou em {c.timeout:g}s ({motivo})",
                         limitacoes=[f"cenário '{c.id}' ({ctx.alvo}) não terminou em {c.timeout:g}s ({motivo}): "
                                     f"lentidão ou trava do agente, não da MAW", *limitacoes])
    estrangeira = _app_do_usuario(ctx)
    if estrangeira is not None:
        return Resultado("pulei", nota="o usuário abriu a MAW durante o teste",
                         limitacoes=[f"o usuário abriu a MAW durante o teste (cenário '{c.id}', {ctx.alvo}, pid "
                                     f"{estrangeira.get('pid')}): a sessão foi encerrada antes do fim e o "
                                     f"%APPDATA% da MAW restaurado"])
    if situacao == "excecao":
        if _som_necessario(excecao):
            return Resultado("pulei", nota=f"exige som (roda na execução noturna, MAW_AGENTE_SOM=1): {excecao}")
        if _entrada_real_necessaria(excecao):
            return Resultado("pulei", nota=f"exige teclado/mouse reais (roda na execução noturna): {excecao}")
        if _app_travou(excecao):
            captura = getattr(excecao, "captura", None)
            if captura:
                ctx.registrar_evidencia(Path(captura), "a janela da MAW quando ela travou ou fechou")
            fechou = getattr(excecao, "motivo", "") == "encerrou"
            return Resultado("problema", nota="a MAW travou ou fechou durante o cenário",
                             esperado="a MAW continua aberta e respondendo durante o cenário",
                             obtido=f"{'a MAW fechou sozinha' if fechou else 'a MAW parou de responder'}: {excecao}",
                             passos=[f"MA e2e --alvo {ctx.alvo} --cenario {c.id}"], severidade="alta")
        resumo = _resumo_traceback(detalhe)
        _registrar_evidencia_traceback(ctx, detalhe)
        linha = f"erro do agente no cenário '{c.id}' ({ctx.alvo}): {resumo}"
        return Resultado("pulei", nota=f"erro do agente no cenário: {resumo}", limitacoes=[linha])
    if not isinstance(resultado, Resultado):
        linha = (f"erro do agente no cenário '{c.id}' ({ctx.alvo}): não devolveu um Resultado "
                f"(devolveu {resultado!r})")
        return Resultado("pulei", nota="erro do agente no cenário: não devolveu um Resultado", limitacoes=[linha])
    if resultado.estado == "problema" and not (resultado.obtido or "").strip():
        linha = (f"erro do agente no cenário '{c.id}' ({ctx.alvo}): devolveu 'problema' sem 'obtido' "
                f"(sem evidência do defeito)")
        return Resultado("pulei", nota="erro do agente no cenário: 'problema' sem 'obtido' (sem evidência)",
                         limitacoes=[linha])
    if resultado.limitacoes:  # um cenário não deveria preencher isso, mas se preencher não se perde
        resultado = dataclasses.replace(resultado, limitacoes=[f"(do cenário) {l}" for l in resultado.limitacoes])
    return resultado


# ---------- achado bruto (mecânico, fonte "e2e") ----------

_ITEM_PADRAO = "interface/geral"


def achado_de_problema(c: Cenario, alvo: dict, resultado: Resultado, evidencias: list[dict] | None = None) -> dict:
    """Achado bruto de um cenário em `problema`, pronto para `achados-brutos/e2e-<alvo>-<id>.json`
    (valida por `achados.validar`)."""
    evidencias = evidencias or []
    item = c.itens[0] if c.itens else _ITEM_PADRAO
    texto_obtido = resultado.obtido or resultado.nota or "cenário terminou em problema sem detalhe"
    return {
        "titulo": f"Cenário e2e '{c.id}' terminou em problema",
        "tipo": "bug",
        "severidade": resultado.severidade or "media",
        "prioridade": None,
        "alvos": [{"alvo": alvo["nome"], "commit": alvo["commit"]}],
        "item_catalogo": item,
        "principio": None,
        "passos": resultado.passos or [f"MA e2e --alvo {alvo['nome']} (cenário {c.id})"],
        "esperado": resultado.esperado or "o cenário passa",
        "obtido": texto_obtido,
        "evidencias": evidencias,
        "causa_provavel": None,
        "sugestao": "investigar a causa relatada pelo cenário e2e",
        "criterio_aceite": f"o cenário e2e '{c.id}' passa para o alvo {alvo['nome']}",
        "confianca": "confirmado",
        "assinatura": f"e2e::{c.id}",  # sem o valor medido: a mesma falha é o mesmo achado entre sprints
        "fonte": "e2e",
    }
