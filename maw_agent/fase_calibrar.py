"""`python -m maw_agent calibrar`: reinjeta os defeitos de `privado/calibracao/defeitos.yaml` no main
e mede quantos a suíte (com as sondas) detecta.

Cada rodada (o controle sem defeito e um defeito por vez) usa uma worktree própria do espelho do
agente em `work/calibracao/<id>/`, destacada no commit do main da sprint (sem sprint: `origin/main`),
compila Debug, roda a suíte e remove a worktree. A suíte mexe no %APPDATA%\\MAW: cada execução é
cercada pelo mesmo backup → bandeira `appdata-sujo.json` → restauração conferida da fase `suite`.
Resultado: `calibracao.json` na pasta da sprint (sem sprint: `work/calibracao/ultimo.json`).
"""
from __future__ import annotations
import argparse
import json
import os
import time
from pathlib import Path

from . import alvos, build, calibracao, config, estado, fases, sandbox, sondas, suite, trava_appdata
from .cli import registrar

DEFEITOS = config.PRIVADO / "calibracao" / "defeitos.yaml"
RAIZ = config.WORK / "calibracao"


def _cfg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--so", action="append", metavar="ID", help="mede só este defeito (pode repetir)")
    p.add_argument("--sem-sondas", action="store_true", help="mede só a suíte do alvo, sem as sondas do agente")
    p.add_argument("--timeout", type=int, default=900, help="limite de cada execução da suíte, em segundos")
    p.add_argument("--esperar-maw", type=int, default=300,
                   help="quanto esperar a MAW aberta fechar antes de cada suíte, em segundos")


class OpsReais:
    """Operações de verdade da calibração: git no espelho, MSBuild e a suíte com o ambiente protegido."""

    def __init__(self, e: estado.Estado, em_sprint: bool, commit: str, arquivos_sondas: list[Path],
                 evidencias: Path, timeout: int, esperar_maw: int, raiz: Path = RAIZ):
        self.e, self.em_sprint, self.commit = e, em_sprint, commit
        self.arquivos_sondas = list(arquivos_sondas)
        self.evidencias, self.timeout, self.esperar_maw, self.raiz = Path(evidencias), timeout, esperar_maw, Path(raiz)
        self.env = suite.ambiente_com_ffmpeg(dict(os.environ), config.FERRAMENTAS / "ffmpeg" / "bin")
        self.cwd = config.WORK / "execucao" / "calibracao"
        self.restauracoes: list[dict] = []
        self.sondas: dict[str, dict] = {}

    # --- worktree e build ---
    def preparar(self, nome: str) -> Path:
        wt = calibracao.criar_worktree(config.ESPELHO, self.commit, self.raiz / nome)
        if self.arquivos_sondas:
            self.sondas[nome] = sondas.injetar(wt, self.arquivos_sondas)
        return wt

    def compilar(self, nome: str, worktree: Path) -> dict:
        return build.compilar(f"calibracao-{nome}", worktree, "Debug", self.evidencias).como_dict()

    def limpar(self, nome: str) -> None:
        # pelo id: também remove a sobra de um `preparar` que caiu no meio
        calibracao.remover_worktree(config.ESPELHO, self.raiz / nome)

    # --- suíte com o %APPDATA%\MAW protegido ---
    def _esperar_maw_fechada(self) -> None:
        limite = time.monotonic() + self.esperar_maw
        while suite.maw_aberta():
            if time.monotonic() >= limite:
                raise calibracao.Abortar(fases._MOTIVO_MAW_ABERTA)
            time.sleep(5)

    def _backup(self) -> Path:
        """Ambiente limpo e backup novo, ou Abortar: a suíte nunca roda sobre um ambiente sujo, e um
        backup de ambiente sujo nunca é tirado como se fosse o original. Antes, toda bandeira pendente
        de qualquer lugar (sprints, sessão avulsa da GUI, calibração fora de sprint que caiu) é
        restaurada pela regra do fases (a mais antiga é a original)."""
        r = fases.restaurar_pendencias_ambiente("calibracao", self.e)
        self.restauracoes += [reg for cat in ("restauradas", "descartadas", "adiadas", "erros") for reg in r[cat]]
        pendentes = fases.pendencias_ambiente()
        if pendentes:
            motivo = ("restauração pendente do %APPDATA%\\MAW ("
                      + ", ".join(fases._descrever_bandeira(p) for p in pendentes)
                      + ") não foi concluída: a calibração não faz backup de um ambiente sujo")
            if self.em_sprint:
                fases._acrescentar_limitacoes(self.e, "calibracao", [motivo])
            raise calibracao.Abortar(motivo)
        try:
            return sandbox.backup_pasta(config.APPDATA_MAW, config.BACKUPS)
        except OSError as ex:
            raise calibracao.Abortar(f"não foi possível fazer o backup do %APPDATA%\\MAW: {ex}") from ex

    def rodar(self, nome: str, exe: Path) -> dict:
        # a trava do %APPDATA%\\MAW do começo ao fim (backup → suíte → restauração)
        posse = trava_appdata.adquirir(fases.ESPERA_TRAVA_APPDATA)
        if posse is None:
            raise calibracao.Abortar(f"outra sessão do agente usou a MAW por mais de {fases.ESPERA_TRAVA_APPDATA:g} s "
                                     "(trava do %APPDATA%\\MAW ocupada)")
        try:
            return self._rodar_na_trava(nome, exe)
        finally:
            posse.liberar()

    def _rodar_na_trava(self, nome: str, exe: Path) -> dict:
        self._esperar_maw_fechada()
        backup = self._backup()
        try:
            sandbox.escrever_json(self.e.pasta / "appdata-sujo.json", {"backup": str(backup), "desde": fases._agora()})
            r = suite.rodar_suite(Path(exe), sandbox.criar_pasta(self.cwd), timeout=self.timeout, env=self.env)
        finally:
            reg = fases._restaurar_ambiente(self.e, backup, "calibracao")
            self.restauracoes.append(reg)
        if not reg["verificado"]:
            raise calibracao.Abortar(f"o %APPDATA%\\MAW não foi restaurado: {reg.get('erro')}")
        d = r.como_dict()
        sandbox.escrever_json(self.evidencias / f"suite-{nome}.json", d)
        d.pop("bruto", None)
        return d


def _saida(obj: dict, ok: bool = True) -> int:
    """JSON no stdout. Redirecionado para arquivo (script noturno), o stdout do Windows é cp1252 e
    não tem "→"; nesse caso sai o mesmo JSON só com ASCII (escapes do JSON) em vez de uma exceção."""
    try:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    except UnicodeEncodeError:
        print(json.dumps(obj, ensure_ascii=True, indent=2))
    return 0 if ok else 1


def _commit_do_main(e: estado.Estado | None) -> str:
    if e is not None and (e.pasta / "alvos.json").exists():
        for a in fases._alvos(e, "main"):
            return a["commit"]
    return alvos.git(["rev-parse", "origin/main"], config.ESPELHO)


def _resumo(res: dict) -> dict:
    campos = ("detectado", "detectado_so_por_outro_bloco", "blocos_esperados_que_falharam",
              "outros_blocos_que_falharam", "erro", "segundos")
    return {k: {c: v.get(c) for c in campos} for k, v in res.items()}


@registrar("calibrar", "reinjeta defeitos conhecidos no main e mede quantos a suíte detecta", _cfg)
def cmd_calibrar(args: argparse.Namespace) -> int:
    e = estado.em_andamento(config.RELATORIOS)
    em_sprint = e is not None
    if e is None:
        sandbox.criar_pasta(RAIZ)
        e = estado.Estado(RAIZ, 0)  # só para a bandeira e o registro das restaurações desta calibração
    saida = e.pasta / "calibracao.json" if em_sprint else RAIZ / "ultimo.json"
    evidencias = e.pasta / "evidencias" / "calibracao" if em_sprint else RAIZ / "evidencias"

    defeitos = calibracao.carregar_defeitos(DEFEITOS)
    if args.so:
        desconhecidos = sorted(set(args.so) - {d.id for d in defeitos})
        if desconhecidos:
            return _saida({"erro": f"defeito desconhecido: {', '.join(desconhecidos)}"}, False)
        defeitos = [d for d in defeitos if d.id in args.so]

    if em_sprint:
        e.iniciar("calibrar")
    try:
        ops = OpsReais(e, em_sprint, _commit_do_main(e if em_sprint else None),
                       [] if args.sem_sondas else sondas.listar(), evidencias, args.timeout, args.esperar_maw)
        musica_antes = fases._arquivos_musica()
        parcial: dict[str, dict] = {}

        def gravar(id_: str, r: dict) -> None:
            parcial[id_] = r
            sandbox.escrever_json(saida, parcial)

        res, controle = calibracao.calibrar(defeitos, ops, gravar)
        sandbox.escrever_json(saida, res)
        musica_depois = fases._arquivos_musica()
    except Exception as ex:
        if em_sprint:
            e.falhar("calibrar", f"{type(ex).__name__}: {ex}")
        raise

    novos = {p: sorted(set(l) - set(musica_antes.get(p, []))) for p, l in musica_depois.items()}
    novos = {k: v for k, v in novos.items() if v}
    t = calibracao.taxa(res)
    ctrl = {"erro": controle["erro"], "blocos_que_falharam": controle["blocos_que_falharam"],
            "segundos": controle.get("segundos")}
    limitacoes = [f"calibração: controle inválido: {controle['erro']}"] if controle["erro"] else []
    limitacoes += [f"calibração: defeito {k}: {r['erro']}" for k, r in res.items() if r.get("erro")]
    limitacoes += [f"calibração: a suíte deixou {len(v)} arquivo(s) em Music\\{k}" for k, v in novos.items()]
    restaurado = all(r.get("verificado") for r in ops.restauracoes)
    if em_sprint:
        fases._acrescentar_limitacoes(e, "calibracao", limitacoes)
        e.concluir("calibrar", {"taxa": t, "controle": ctrl, "sondas": not args.sem_sondas})
    return _saida({"saida": str(saida), "commit": ops.commit, "taxa": t, "controle": ctrl,
                         "defeitos": _resumo(res), "sondas": {k: v.get("injetadas") for k, v in ops.sondas.items()},
                         "limitacoes": limitacoes, "ambiente_restaurado": restaurado,
                         "restauracoes": ops.restauracoes},
                        controle["erro"] is None and restaurado)
