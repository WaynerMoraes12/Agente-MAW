"""Fase `servico`: sobe o script do serviço de IA de cada alvo, no Python do próprio alvo
(`work/py310-<alvo>`, montado pelos requisitos dele; sem ele, o ambiente comum `work/py310` e uma
limitação), roda as checagens HTTP, de comando e de porta de `privado/cenarios/servico.yaml` e
registra resultados/achados — como a fase `e2e`. Nada de conhecimento específico do alvo mora neste
arquivo público (nome do script, rotas, texto de erro, nomes de pacote): tudo isso vem de
`privado/cenarios/servico.yaml`.

Rodando fora do ambiente do próprio alvo, a falha de uma checagem que depende dos pacotes instalados
(`depende_do_ambiente`) não é achado — vira `nao_testavel` com o motivo. A exceção é a checagem que
julga os próprios requisitos (`julga_requisitos`) quando eles são insatisfazíveis: aí a causa é do
alvo, e a falha continua achado.

`MA servico ambientes` monta, com uv, o ambiente de cada alvo que ainda não o tem (`--listar` só
mostra a escolha de cada um, sem montar nada).

Nunca lê, imprime nem grava a chave de API do serviço de IA: a rota de "pedir um texto gerado" só
é exercitada pelos erros de validação documentados no YAML privado (nunca chega a chamar o
provedor de verdade), e o ambiente do processo filho nunca carrega nenhuma variável do agente cujo
nome pareça uma credencial (`servico._ambiente_sem_segredos`, regra genérica, sem nome de variável
específico em lugar nenhum do código) — só as `variaveis` do YAML privado entram por cima, com valor
forjado (um valor com cara de credencial de verdade é recusado ao carregar). O restante fica
`nao_testavel`, documentado no YAML privado.

Uma porta já ocupada por outro processo nunca vira um defeito do alvo: o agente não inicia nada
nesse caso, não mexe no processo que já está lá, e registra a situação como `nao_testavel`.

Alvo cujo script usa o token de quem chama (`token:` do YAML privado): o serviço dele grava o token
num arquivo do %APPDATA%\\MAW real do usuário. Por isso esse alvo roda cercado como a suíte: trava do
%APPDATA%\\MAW → (MAW fechada, pendências restauradas) → backup → bandeira `appdata-sujo.json` →
serviço e checagens → restauração conferida (`fases._restaurar_ambiente`, que `sprint encerrar`
confere). Toda checagem leva o token, a menos que peça outra coisa; o token nunca vai para evidência,
achado, resultado nem estado (`servico.redigir`). As checagens `requer_token` são `na` num alvo
sem token, e as variáveis `requer_token` do YAML só entram no ambiente do serviço de um alvo com
token: um alvo antigo roda exatamente como antes, sem tocar no %APPDATA%\\MAW.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from . import achados, catalogo, config, estado, fases, sandbox, servico, suite, trava_appdata
from .cli import registrar

_MOTIVO_NA = ("o script do serviço deste alvo não usa o token de quem chama: a funcionalidade não existe "
              "neste alvo")
_MOTIVO_MAW_ABERTA = ("MAW aberta: o serviço de um alvo com token grava no %APPDATA%\\MAW e só roda com a MAW "
                      "fechada — feche-a e rode `MA servico` de novo")

CAMINHO_CENARIOS = config.PRIVADO / "cenarios" / "servico.yaml"


def _saida(obj: dict, ok: bool = True) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def _sprint_atual() -> estado.Estado:
    e = estado.em_andamento()
    if e is None:
        raise SystemExit("nenhuma sprint em andamento: rode `sprint iniciar`")
    return e


def _alvos(e: estado.Estado, so: str | None = None) -> list[dict]:
    lista = json.loads((e.pasta / "alvos.json").read_text(encoding="utf-8"))["alvos"]
    return [a for a in lista if so is None or a["nome"] == so]


def _itens_catalogo() -> list[dict]:
    return catalogo.carregar(config.CATALOGO) if config.CATALOGO.exists() else []


def _itens_servico(itens: list[dict]) -> set[str]:
    return {it["id"] for it in itens if "servico" in (it.get("verificacao") or [])}


def _acrescentar_limitacoes(e: estado.Estado, nome: str, linhas: list[str]) -> None:
    if not linhas:
        return
    p = e.pasta / f"limitacoes-{nome}.json"
    atuais = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    novas = atuais + [l for l in linhas if l not in atuais]
    if novas != atuais:
        sandbox.escrever_json(p, novas)


# ---------- cobertura: todo item 'servico' do catálogo termina registrado ou justificado ----------

def cobertura(itens_servico: set[str], dados_cenarios: dict) -> tuple[set[str], list[str]]:
    """Une os itens do catálogo cobertos por checagens/comandos/portas/nao_testavel de
    `dados_cenarios` e devolve (cobertos, item sem checagem nenhuma). Nada some em silêncio: um
    item 'servico' do catálogo sem checagem alguma vira uma limitação, não um resultado ausente."""
    cobertos: set[str] = set()
    for c in dados_cenarios["checagens"]:
        cobertos.update(c.itens_catalogo)
    for c in dados_cenarios["comandos"]:
        cobertos.update(c.itens_catalogo)
    for c in dados_cenarios["portas"]:
        cobertos.update(c.itens_catalogo)
    for n in dados_cenarios["nao_testavel"]:
        cobertos.update(n.itens_catalogo)
    faltando = sorted(itens_servico - cobertos)
    return cobertos, faltando


# ---------- achados ----------

def _evidencia(e: estado.Estado, nome_alvo: str, chk_id: str, detalhe: dict) -> str:
    caminho = e.pasta / "evidencias" / "servico" / f"{nome_alvo}-{chk_id}.json"
    texto = servico.redigir(json.dumps(detalhe, ensure_ascii=False, indent=2))
    sandbox.escrever_texto(caminho, texto)
    return f"evidencias/servico/{nome_alvo}-{chk_id}.json"


def _escrever_achado_validado(e: estado.Estado, nome_arquivo: str, achado: dict) -> None:
    """Só grava em `achados-brutos/` um achado que passa no próprio esquema — um achado inválido é
    erro do agente (regra 5 da constituição): vira limitação, nunca uma ficha ruim no disco."""
    erros = achados.validar(achado)
    if erros:
        _acrescentar_limitacoes(e, "servico",
                                [f"achado não registrado por não passar no esquema ({nome_arquivo}): {erros}"])
        return
    sandbox.escrever_json(e.pasta / "achados-brutos" / nome_arquivo, achado)


def _rel(caminho: Path | None) -> str:
    """Caminho relativo à raiz do agente, para os passos de um achado (sem o nome do usuário)."""
    if caminho is None:
        return "work/py310/Scripts/python.exe"
    try:
        return Path(caminho).resolve().relative_to(config.RAIZ.resolve()).as_posix()
    except (ValueError, OSError):
        return Path(caminho).as_posix()


def _fora_do_ambiente(amb, depende: bool, julga_requisitos: bool = False) -> str | None:
    """Motivo de `nao_testavel` para a FALHA de uma checagem que depende dos pacotes do Python do
    serviço quando ele não é o do próprio alvo; None = a falha é julgada normalmente (achado)."""
    if amb is None or not depende or amb.proprio:
        return None
    if amb.insatisfazivel and julga_requisitos:
        return None
    if amb.insatisfazivel:
        return f"consequência dos requisitos insatisfazíveis do alvo, julgados à parte — {amb.limitacao}"
    return f"o serviço não rodou no ambiente do próprio alvo — {amb.limitacao}"


def achado_de_checagem(chk_id: str, rota: str, metodo: str, principio: str | None, esperado: str,
                       item_catalogo: str, alvo: dict, problemas: list[str], caminho_evidencia: str,
                       nome_script: str, python: Path | None = None) -> dict:
    obtido = "; ".join(problemas) if problemas else "resposta não bateu com o esperado"
    return {
        "titulo": f"Serviço: {metodo} {rota} não bate com o contrato ({chk_id})",
        "tipo": "erro", "severidade": "alta", "prioridade": None,
        "alvos": [{"alvo": alvo["nome"], "commit": alvo["commit"]}],
        "item_catalogo": item_catalogo, "principio": principio,
        "passos": [f"subir {nome_script} ({_rel(python)}, com o ffmpeg e os executáveis do ambiente no PATH) e "
                  f"mandar {metodo} {rota} com o corpo do cenário '{chk_id}' (privado/cenarios/servico.yaml)"],
        "esperado": esperado or "ver privado/cenarios/servico.yaml", "obtido": servico.redigir(obtido),
        "evidencias": [{"arquivo": caminho_evidencia, "legenda": f"resposta de {chk_id}", "embutir": False}],
        "causa_provavel": None, "sugestao": "corrigir o serviço até a resposta bater com o contrato documentado",
        "criterio_aceite": esperado or "a resposta bate com o contrato documentado em servico.yaml",
        "confianca": "confirmado", "assinatura": f"servico:{chk_id}", "fonte": "servico",
    }


def achado_de_comando(chk, alvo: dict, saida: str, codigo: int | None, python: Path | None = None,
                      ambiente=None) -> dict:
    resumo = "\n".join(saida.strip().splitlines()[-15:])
    item_principal = chk.itens_catalogo[0] if chk.itens_catalogo else "servico/geral"
    # fora do ambiente do próprio alvo (requisitos insatisfazíveis), o motivo vai junto no obtido
    contexto = (f"{ambiente.limitacao}. No ambiente comum: "
                if ambiente is not None and not ambiente.proprio and ambiente.limitacao else "")
    return {
        "titulo": f"Serviço: comando '{' '.join(chk.argumentos)}' não bate com o esperado ({chk.id})",
        "tipo": "erro", "severidade": "alta", "prioridade": None,
        "alvos": [{"alvo": alvo["nome"], "commit": alvo["commit"]}],
        "item_catalogo": item_principal, "principio": None,
        "passos": [f"{_rel(python)} {' '.join(chk.argumentos)} (cwd={alvo['nome']})"],
        "esperado": chk.esperado or f"código de saída {chk.codigo_esperado}",
        "obtido": servico.redigir(f"{contexto}código de saída {codigo}: {resumo}"),
        "evidencias": [], "causa_provavel": None,
        "sugestao": "corrigir as dependências do serviço até o comando funcionar",
        "criterio_aceite": chk.esperado or f"o comando sai com código {chk.codigo_esperado}",
        "confianca": "confirmado", "assinatura": f"servico:{chk.id}", "fonte": "servico",
    }


def achado_de_porta(chk, alvo: dict, aberta: bool, nome_script: str, porta: int | None = None,
                    problemas: list[str] | None = None) -> dict:
    porta = chk.porta if porta is None else porta
    item_principal = chk.itens_catalogo[0] if chk.itens_catalogo else "servico/geral"
    padrao = (f"porta {porta} deveria estar "
             f"{'aberta' if chk.aberta_esperada else 'fechada'}")
    esperado = chk.esperado or padrao
    obtido = "; ".join(problemas) if problemas else f"porta {porta} está {'aberta' if aberta else 'fechada'}"
    return {
        "titulo": f"Serviço: porta {porta} não bate com o esperado ({chk.id})",
        "tipo": "afirmacao_falsa", "severidade": "baixa", "prioridade": None,
        "alvos": [{"alvo": alvo["nome"], "commit": alvo["commit"]}],
        "item_catalogo": item_principal, "principio": "P5",
        "passos": [f"subir {nome_script}, tentar conectar em 127.0.0.1:{porta} e comparar com a porta que a "
                  "documentação do alvo cita"],
        "esperado": esperado, "obtido": obtido,
        "evidencias": [], "causa_provavel": None,
        "sugestao": "corrigir a documentação (ou o código) para os dois concordarem sobre a porta",
        "criterio_aceite": esperado, "confianca": "confirmado", "assinatura": f"servico:{chk.id}",
        "fonte": "servico",
    }


# ---------- bancada (mesmo mapa da fase e2e; opcional por checagem) ----------

def _bancada_registrar(e: estado.Estado, codigo: str | None, ok: bool, nota: str, alvo: str,
                       estado_bancada: str | None = None) -> None:
    if not codigo:
        return
    try:
        from . import bancada
    except ImportError:
        _acrescentar_limitacoes(e, "servico", [
            "maw_agent.bancada não existe nesta árvore: os códigos de bancada do serviço não foram "
            "registrados (a fase e2e cria esse módulo; rode de novo depois que ela existir)"])
        return
    bancada.registrar(e.pasta, codigo, estado_bancada or ("passou" if ok else "problema"), nota, alvo)


# ---------- execução de um alvo ----------

def _preparar_fixtures(pasta_trabalho: Path) -> dict[str, str]:
    audio_valido = pasta_trabalho / "fixture.wav"
    servico.gerar_wav_curto(audio_valido)
    return {
        "audio_valido": str(audio_valido),
        "audio_inexistente": str(pasta_trabalho / "nao-existe.wav"),
        "saida": str(pasta_trabalho / "saida"),
    }


def _acumular(agregados: dict[str, list[tuple[bool, str]]], itens: list[str], ok: bool, motivo: str) -> None:
    """Um item do catálogo pode ser citado por mais de uma checagem (ex.: 'principio/P4' aparece em
    vários cenários de erro). `catalogo.registrar_resultado` grava uma linha por chamada e a leitura
    fica com a ÚLTIMA — chamar duas vezes para o mesmo item perderia a primeira em silêncio. Por isso
    as checagens só acumulam aqui; `_registrar_agregados` grava uma vez por item, com o pior caso."""
    for item in itens:
        agregados.setdefault(item, []).append((ok, motivo))


def _motivo_sem_token(app) -> str:
    return (f"o token de quem chama não pôde ser lido do arquivo do serviço deste alvo "
            f"({app.token_caminho}): a checagem precisa dele")


def _rodar_checagens_http(e: estado.Estado, alvo: dict, app: "servico.ServicoApp | None", checagens: list,
                          contexto: dict[str, str], agregados: dict[str, list[tuple[bool, str]]],
                          nome_script: str, ambiente=None, nao_testaveis: dict[str, str] | None = None,
                          com_token: bool = False, nao_se_aplica: dict[str, str] | None = None) -> list[dict]:
    """`com_token`: o script do alvo usa o token — toda checagem leva `app.token`; senão as
    `requer_token` vão para `nao_se_aplica` (`na`) sem pedido nenhum (e `app` pode ser None)."""
    resumo = []
    nao_testaveis = {} if nao_testaveis is None else nao_testaveis
    nao_se_aplica = {} if nao_se_aplica is None else nao_se_aplica
    for chk in checagens:
        if chk.requer_token and not com_token:
            for item in chk.itens_catalogo:
                nao_se_aplica.setdefault(item, _MOTIVO_NA)
            resumo.append({"id": chk.id, "ok": None, "na": _MOTIVO_NA})
            continue
        token = app.token if com_token else None
        if com_token and servico.precisa_do_token(chk) and not (token and token.valor):
            motivo = _motivo_sem_token(app)
            for item in chk.itens_catalogo:
                nao_testaveis.setdefault(item, motivo)
            resumo.append({"id": chk.id, "ok": None, "nao_testavel": motivo})
            continue
        try:
            ok, problemas, detalhe = servico.executar_checagem(app.base_url, chk, contexto, token=token)
        except Exception as ex:  # a checagem nunca pode derrubar a fase inteira
            ok, problemas = False, [servico.redigir(f"a checagem levantou uma exceção: {ex}")]
            detalhe = {"excecao": servico.redigir(str(ex))}
        motivo = "; ".join(problemas)
        fora = None if ok else _fora_do_ambiente(ambiente, chk.depende_do_ambiente)
        if fora:
            # falhou num Python que não é o do alvo: sem achado; o item fica não testável com o motivo
            _evidencia(e, alvo["nome"], chk.id, detalhe)
            for item in chk.itens_catalogo:
                nao_testaveis.setdefault(item, f"{motivo} ({fora})")
            _bancada_registrar(e, chk.bancada, False, f"não testável: {fora}", alvo["nome"], "pulei")
            resumo.append({"id": chk.id, "ok": None, "problemas": problemas, "nao_testavel": fora})
            continue
        _acumular(agregados, chk.itens_catalogo, ok, motivo)
        if not ok:
            evidencia = _evidencia(e, alvo["nome"], chk.id, detalhe)
            item_principal = chk.itens_catalogo[0] if chk.itens_catalogo else "servico/geral"
            achado = achado_de_checagem(chk.id, chk.rota, chk.metodo, chk.principio, chk.esperado,
                                        item_principal, alvo, problemas, evidencia, nome_script,
                                        ambiente.python if ambiente is not None else None)
            _escrever_achado_validado(e, f"servico-{alvo['nome']}-{chk.id}.json", achado)
        _bancada_registrar(e, chk.bancada, ok, motivo or "ok", alvo["nome"])
        resumo.append({"id": chk.id, "ok": ok, "problemas": problemas})
    return resumo


def _rodar_comandos(e: estado.Estado, alvo: dict, comandos: list,
                    agregados: dict[str, list[tuple[bool, str]]], ambiente=None,
                    nao_testaveis: dict[str, str] | None = None) -> list[dict]:
    """Cada comando roda com o MESMO Python que serviu o alvo (o do ambiente escolhido para ele)."""
    resumo = []
    nao_testaveis = {} if nao_testaveis is None else nao_testaveis
    python = ambiente.python if ambiente is not None else servico.PYTHON_SERVICO_PADRAO
    for chk in comandos:
        try:
            codigo, texto = servico.rodar_comando(python, chk.argumentos, chk.timeout,
                                                  cwd=config.ALVOS_DIR / alvo["nome"])
        except Exception as ex:  # um comando nunca pode derrubar a fase inteira
            codigo, texto = None, f"o comando levantou uma exceção: {ex}"
        ok, problemas = servico.avaliar_comando(chk, codigo, texto)
        fora = None if ok else _fora_do_ambiente(ambiente, chk.depende_do_ambiente, chk.julga_requisitos)
        if fora:
            for item in chk.itens_catalogo:
                nao_testaveis.setdefault(item, f"{'; '.join(problemas)} ({fora})")
            resumo.append({"id": chk.id, "ok": None, "codigo": codigo, "nao_testavel": fora})
            continue
        _acumular(agregados, chk.itens_catalogo, ok, "; ".join(problemas))
        if not ok:
            achado = achado_de_comando(chk, alvo, servico.redigir(texto), codigo, python, ambiente)
            _escrever_achado_validado(e, f"servico-{alvo['nome']}-{chk.id}.json", achado)
        resumo.append({"id": chk.id, "ok": ok, "codigo": codigo})
    return resumo


def _rodar_portas(e: estado.Estado, alvo: dict, portas: list, agregados: dict[str, list[tuple[bool, str]]],
                  nome_script: str, porta_do_servico: int | None = None, pasta_alvo: Path | None = None,
                  nao_testaveis: dict[str, str] | None = None) -> list[dict]:
    """Porta fixa ou a do serviço do alvo (`porta: servico`), aberta ou fechada como o YAML espera e,
    se ele disser onde o alvo documenta a porta, igual à documentada. Documentação que não cita porta
    nenhuma não é achado: a checagem fica `nao_testavel` com o que foi procurado."""
    resumo = []
    nao_testaveis = {} if nao_testaveis is None else nao_testaveis
    for chk in portas:
        try:
            porta = servico.resolver_porta(chk, porta_do_servico)
        except ValueError as ex:
            for item in chk.itens_catalogo:
                nao_testaveis.setdefault(item, str(ex))
            resumo.append({"id": chk.id, "ok": None, "nao_testavel": str(ex)})
            continue
        documentadas: list[tuple[str, int]] = []
        if chk.documentacao:
            documentadas, sem = servico.portas_documentadas(pasta_alvo or (config.ALVOS_DIR / alvo["nome"]),
                                                            chk.documentacao)
            if not documentadas:
                motivo = f"a documentação deste alvo não cita a porta do serviço (procurado em: {'; '.join(sem)})"
                for item in chk.itens_catalogo:
                    nao_testaveis.setdefault(item, motivo)
                resumo.append({"id": chk.id, "ok": None, "porta": porta, "nao_testavel": motivo})
                continue
        try:
            aberta = servico.porta_aberta(porta)
        except Exception:  # uma porta nunca pode derrubar a fase inteira
            aberta = False
        ok, problemas = servico.avaliar_porta(chk, aberta, porta=porta, documentadas=documentadas)
        _acumular(agregados, chk.itens_catalogo, ok, "; ".join(problemas))
        if not ok:
            achado = achado_de_porta(chk, alvo, aberta, nome_script, porta, problemas)
            _escrever_achado_validado(e, f"servico-{alvo['nome']}-{chk.id}.json", achado)
        resumo.append({"id": chk.id, "ok": ok, "aberta": aberta, "porta": porta,
                       "documentada": [list(d) for d in documentadas]})
    return resumo


def _registrar_agregados(e: estado.Estado, nome_alvo: str, agregados: dict[str, list[tuple[bool, str]]],
                        nao_testaveis: dict[str, str] | None = None,
                        nao_se_aplica: dict[str, str] | None = None) -> None:
    # item cujas checagens são todas de um recurso que este alvo não tem (`requer_token` num alvo sem token)
    for item, motivo in (nao_se_aplica or {}).items():
        if item not in agregados and item not in (nao_testaveis or {}):
            catalogo.registrar_resultado(e.pasta, item, nome_alvo, "na", motivo, fonte="servico")
    # item cuja única checagem ficou não testável (fora do ambiente do alvo, documentação sem porta)
    for item, motivo in (nao_testaveis or {}).items():
        if item not in agregados:
            catalogo.registrar_resultado(e.pasta, item, nome_alvo, "nao_testavel", motivo, fonte="servico")
    for item, resultados in agregados.items():
        falhas = [m for ok, m in resultados if not ok and m]
        if any(not ok for ok, _ in resultados):
            catalogo.registrar_resultado(e.pasta, item, nome_alvo, "falhou",
                                        "; ".join(dict.fromkeys(falhas)) or None, fonte="servico")
        else:
            catalogo.registrar_resultado(e.pasta, item, nome_alvo, "passou", fonte="servico")


def _registrar_nao_testavel(e: estado.Estado, alvo: dict, nao_testavel: list, motivo_extra: str = "") -> None:
    for n in nao_testavel:
        for item in n.itens_catalogo:
            motivo = n.motivo + (f" ({motivo_extra})" if motivo_extra else "")
            catalogo.registrar_resultado(e.pasta, item, alvo["nome"], "nao_testavel", motivo, fonte="servico")


def _servico_de_um_alvo(e: estado.Estado, alvo: dict, dados_cenarios: dict, itens_servico: set[str]) -> dict:
    nome = alvo["nome"]
    passo = f"servico:{nome}"
    e.iniciar(passo)
    pasta_alvo = config.ALVOS_DIR / nome
    nome_script = dados_cenarios["script"]
    cfg = dados_cenarios.get("ambiente") or servico.ConfigAmbiente()
    ambiente = servico.escolher_ambiente(nome, pasta_alvo, cfg.requisitos)
    if ambiente.limitacao:
        _acrescentar_limitacoes(e, "servico", [ambiente.limitacao])
    python = ambiente.python
    motivo_indisponivel = None
    if not python.exists():
        motivo_indisponivel = f"python do serviço ausente: {python}"
        _acrescentar_limitacoes(e, "servico", [f"{motivo_indisponivel} — fase servico não rodou para {nome}"])
    elif not (pasta_alvo / nome_script).exists():
        motivo_indisponivel = f"o script do serviço não existe neste alvo ({nome_script})"
        _acrescentar_limitacoes(e, "servico", [f"{motivo_indisponivel} — fase servico não rodou para {nome}"])
    resultado: dict = {"alvo": nome, "python": str(python), "ambiente_proprio": ambiente.proprio}
    if motivo_indisponivel:
        for item in itens_servico:
            catalogo.registrar_resultado(e.pasta, item, nome, "nao_testavel", motivo_indisponivel, fonte="servico")
        resultado["ok"] = False
        resultado["motivo"] = motivo_indisponivel
        e.concluir(passo, resultado)
        return resultado
    com_token = servico.usa_token(pasta_alvo / nome_script, dados_cenarios.get("token"))
    resultado["usa_token"] = com_token

    def rodar() -> None:
        _rodar_no_alvo(e, alvo, dados_cenarios, itens_servico, ambiente, resultado, com_token)

    if not com_token:
        rodar()  # alvo sem token: exatamente como sempre foi, sem tocar no %APPDATA%\MAW
    else:
        recusa, restauracao = _com_appdata_protegido(e, rodar)
        if recusa is not None:
            _nao_rodou(e, nome, itens_servico, recusa, resultado)
        else:
            resultado["ambiente_restaurado"] = restauracao["verificado"]
            if not restauracao["verificado"]:
                resultado["ok"] = False
                _acrescentar_limitacoes(e, "servico", [
                    f"{nome}: o %APPDATA%\\MAW não foi restaurado depois do serviço: "
                    f"{restauracao.get('erro') or 'sem mensagem de erro'}"])
    e.concluir(passo, resultado)
    return resultado


def _nao_rodou(e: estado.Estado, nome: str, itens_servico: set[str], motivo: str, resultado: dict) -> None:
    """O serviço do alvo nem subiu por um motivo do ambiente do agente (nunca defeito do alvo)."""
    for item in itens_servico:
        catalogo.registrar_resultado(e.pasta, item, nome, "nao_testavel", motivo, fonte="servico")
    _acrescentar_limitacoes(e, "servico", [f"{motivo} — fase servico não rodou para {nome}"])
    resultado["ok"] = False
    resultado["motivo"] = motivo


def _com_appdata_protegido(e: estado.Estado, rodar) -> tuple[str | None, dict | None]:
    """Roda `rodar()` como a suíte roda a MAW: com a trava do %APPDATA%\\MAW do começo ao fim, só com
    a MAW fechada e o ambiente limpo (pendências antigas restauradas antes), backup → bandeira →
    `rodar()` → restauração conferida (registrada no estado da sprint). Devolve (motivo de não ter
    rodado — nada foi tocado —, None) ou (None, registro da restauração)."""
    posse = trava_appdata.adquirir(fases.ESPERA_TRAVA_APPDATA)
    if posse is None:
        return (f"outra sessão do agente usou a MAW por mais de {fases.ESPERA_TRAVA_APPDATA:g} s (trava do "
                "%APPDATA%\\MAW ocupada): o serviço do alvo com token não rodou"), None
    try:
        if suite.maw_aberta():
            return _MOTIVO_MAW_ABERTA, None
        fases.restaurar_pendencias_ambiente("servico", e)
        sujas = fases.pendencias_ambiente()
        if sujas:
            return (f"restauração pendente do %APPDATA%\\MAW ({', '.join(map(fases._descrever_bandeira, sujas))}) "
                    "não foi concluída: o serviço de um alvo com token não roda sobre um ambiente sujo"), None
        try:
            backup = sandbox.backup_pasta(config.APPDATA_MAW, config.BACKUPS)
        except OSError as ex:
            return f"não foi possível fazer o backup do %APPDATA%\\MAW: {servico.redigir(str(ex))}", None
        try:
            sandbox.escrever_json(e.pasta / fases.BANDEIRA, {"backup": str(backup), "desde": fases._agora()})
            rodar()
        finally:
            restauracao = fases._restaurar_ambiente(e, backup, "servico")
        return None, restauracao
    finally:
        posse.liberar()


def _apagar_token_fora_do_backup(e: estado.Estado, nome: str, app: "servico.ServicoApp") -> None:
    """O arquivo do token fica onde o shell diz que é a pasta de dados do usuário; se ela não é a do
    backup (%APPDATA%\\MAW do ambiente), a restauração não o alcança: o token que não existia antes
    é apagado aqui (e a pasta dele, se também não existia), e conferido."""
    caminho = app.token_caminho
    if caminho is None or servico.dentro_de(caminho, config.APPDATA_MAW):
        return
    _acrescentar_limitacoes(e, "servico", [
        f"{nome}: o token do serviço fica fora do backup do %APPDATA%\\MAW ({caminho.parent}): o agente "
        "apaga o que o serviço criou ali, em vez de restaurar"])
    if app.token_existia:
        return
    try:
        if caminho.exists():
            sandbox.remover(caminho)
        pasta = caminho.parent
        if not app.pasta_token_existia and pasta.is_dir() and not any(pasta.iterdir()):
            sandbox.remover(pasta)
    except OSError as ex:
        _acrescentar_limitacoes(e, "servico", [f"{nome}: não deu para apagar o token criado pelo serviço: "
                                               f"{servico.redigir(str(ex))}"])
        return
    if caminho.exists():
        _acrescentar_limitacoes(e, "servico", [f"{nome}: o token criado pelo serviço continua em {caminho}"])


def _rodar_checagens_em_pipe(e: estado.Estado, alvo: dict, checagens: list, contexto: dict[str, str],
                             agregados: dict, nome_script: str, ambiente, nao_testaveis: dict, com_token: bool,
                             nao_se_aplica: dict, abrir) -> list[dict]:
    """As checagens `saida_em_pipe` rodam num segundo serviço, com a saída num pipe que ninguém lê.
    Num alvo sem token as `requer_token` são `na` sem subir nada."""
    aplicaveis = [c for c in checagens if com_token or not c.requer_token]
    resumo = _rodar_checagens_http(e, alvo, None, [c for c in checagens if c not in aplicaveis], contexto,
                                   agregados, nome_script, ambiente, nao_testaveis, com_token, nao_se_aplica)
    if not aplicaveis:
        return resumo
    try:
        with abrir(saida_em_pipe=True) as app:
            resumo += _rodar_checagens_http(e, alvo, app, aplicaveis, {**contexto, "porta": str(app.porta)},
                                            agregados, nome_script, ambiente, nao_testaveis, com_token,
                                            nao_se_aplica)
    except servico.PortaOcupada as ex:
        motivo = servico.redigir(str(ex))
        for chk in aplicaveis:
            for item in chk.itens_catalogo:
                nao_testaveis.setdefault(item, motivo)
            resumo.append({"id": chk.id, "ok": None, "nao_testavel": motivo})
    except servico.ServicoIndisponivel as ex:
        problemas = [f"o serviço não subiu com a saída num pipe que ninguém lê: {servico.redigir(str(ex))}"]
        for chk in aplicaveis:
            _acumular(agregados, chk.itens_catalogo, False, problemas[0])
            evidencia = _evidencia(e, alvo["nome"], chk.id, {"excecao": problemas[0]})
            item_principal = chk.itens_catalogo[0] if chk.itens_catalogo else "servico/geral"
            achado = achado_de_checagem(chk.id, chk.rota, chk.metodo, chk.principio, chk.esperado, item_principal,
                                        alvo, problemas, evidencia, nome_script,
                                        ambiente.python if ambiente is not None else None)
            _escrever_achado_validado(e, f"servico-{alvo['nome']}-{chk.id}.json", achado)
            resumo.append({"id": chk.id, "ok": False, "problemas": problemas})
    return resumo


def _rodar_no_alvo(e: estado.Estado, alvo: dict, dados_cenarios: dict, itens_servico: set[str], ambiente,
                   resultado: dict, com_token: bool) -> None:
    """Sobe o serviço do alvo, roda as checagens HTTP/comandos/portas e registra tudo em `resultado`
    (e no catálogo/achados). Para um alvo sem token, é o que a fase sempre fez."""
    nome = alvo["nome"]
    pasta_alvo = config.ALVOS_DIR / nome
    nome_script = dados_cenarios["script"]
    python = ambiente.python
    pasta_trabalho = sandbox.criar_pasta(config.WORK / "execucao" / nome / "servico")
    contexto = _preparar_fixtures(pasta_trabalho)
    cfg_token = dados_cenarios.get("token") if com_token else None
    extra = servico.variaveis_do_processo(dados_cenarios.get("variaveis") or [], com_token,
                                          {"pasta_alvo": str(pasta_alvo), "trabalho": str(pasta_trabalho)})
    normais = [c for c in dados_cenarios["checagens"] if not c.saida_em_pipe]
    em_pipe = [c for c in dados_cenarios["checagens"] if c.saida_em_pipe]
    agregados: dict[str, list[tuple[bool, str]]] = {}
    nao_testaveis: dict[str, str] = {}
    nao_se_aplica: dict[str, str] = {}

    def abrir(**kw) -> "servico.ServicoApp":
        if not kw.get("saida_em_pipe"):
            kw["log"] = pasta_trabalho / "servico.log"
        return servico.ServicoApp(pasta_alvo, script=nome_script, python=python, ffmpeg=servico.FFMPEG_BIN_PADRAO,
                                  ambiente_extra=extra or None, token=cfg_token, **kw)

    try:
        app = abrir()
        try:
            with app:
                resultado["porta"] = app.porta
                resultado["saude"] = app.saude
                if cfg_token is not None:
                    lido = bool(app.token and app.token.valor)
                    resultado["token"] = {"existia_antes": app.token_existia, "lido": lido}
                    if not lido:
                        _acrescentar_limitacoes(e, "servico", [
                            f"{nome}: o token de quem chama não pôde ser lido ({app.token_caminho}) depois do "
                            "/health: as checagens que precisam dele ficaram não testáveis"])
                ctx = {**contexto, "porta": str(app.porta)}
                resultado["checagens"] = _rodar_checagens_http(e, alvo, app, normais, ctx, agregados, nome_script,
                                                               ambiente, nao_testaveis, com_token, nao_se_aplica)
                resultado["comandos"] = _rodar_comandos(e, alvo, dados_cenarios["comandos"], agregados, ambiente,
                                                        nao_testaveis)
                resultado["portas"] = _rodar_portas(e, alvo, dados_cenarios["portas"], agregados, nome_script,
                                                    app.porta, pasta_alvo, nao_testaveis)
            if em_pipe:
                resultado["checagens"] += _rodar_checagens_em_pipe(
                    e, alvo, em_pipe, contexto, agregados, nome_script, ambiente, nao_testaveis, com_token,
                    nao_se_aplica, abrir)
        finally:
            _apagar_token_fora_do_backup(e, nome, app)
        _registrar_agregados(e, nome, agregados, nao_testaveis, nao_se_aplica)
    except servico.PortaOcupada as ex:
        # nunca é defeito do alvo: o agente não iniciou nada, não mexeu em processo nenhum.
        motivo = servico.redigir(str(ex))
        for item in itens_servico:
            catalogo.registrar_resultado(e.pasta, item, nome, "nao_testavel", motivo, fonte="servico")
        _acrescentar_limitacoes(e, "servico", [f"{motivo} — fase servico não rodou para {nome}"])
        resultado["ok"] = False
        resultado["motivo"] = motivo
        return
    except servico.ServicoIndisponivel as ex:
        motivo = f"o serviço não respondeu a /health: {servico.redigir(str(ex))}"
        for item in itens_servico:
            catalogo.registrar_resultado(e.pasta, item, nome, "falhou", motivo, fonte="servico")
        achado = {
            "titulo": f"Serviço de IA não sobe em {nome}", "tipo": "erro", "severidade": "critica",
            "prioridade": None, "alvos": [{"alvo": nome, "commit": alvo["commit"]}],
            "item_catalogo": "servico/geral", "principio": "P4",
            "passos": [f"{_rel(python)} {nome_script} (cwd=alvo, ffmpeg e os executáveis do ambiente no PATH)"],
            "esperado": "GET /health responde 200 em poucos segundos",
            "obtido": motivo, "evidencias": [], "causa_provavel": None,
            "sugestao": "investigar por que o processo não abre a porta nem responde /health",
            "criterio_aceite": "GET /health responde 200", "confianca": "confirmado",
            "assinatura": f"servico:indisponivel:{nome}", "fonte": "servico"}
        _escrever_achado_validado(e, f"servico-{nome}-indisponivel.json", achado)
        resultado["ok"] = False
        resultado["motivo"] = motivo
        return
    _registrar_nao_testavel(e, alvo, dados_cenarios["nao_testavel"])
    resultado["ok"] = True


def _herdar(e: estado.Estado, todos: list[dict]) -> None:
    res = catalogo.carregar_resultados(e.pasta)
    for a in todos:
        origem = a.get("compartilha_com")
        if not origem:
            continue
        for (item, alvo_res), r in list(res.items()):
            if alvo_res == origem and r.get("fonte") == "servico":
                catalogo.registrar_resultado(e.pasta, item, a["nome"], r["resultado"],
                                            f"mesma árvore de código de {origem}" +
                                            (f": {r['motivo']}" if r.get("motivo") else ""),
                                            r.get("achados"), "servico")


def _cfg_servico(p: argparse.ArgumentParser) -> None:
    p.add_argument("acao", nargs="?", choices=("rodar", "ambientes"), default="rodar",
                   help="rodar (padrão): sobe o serviço de cada alvo e roda as checagens; ambientes: monta, com uv, "
                        "o Python do serviço de cada alvo pelos requisitos dele (work/py310-<alvo>)")
    p.add_argument("--alvo")
    p.add_argument("--listar", action="store_true",
                   help="(ambientes) só mostra qual Python cada alvo usaria e por quê, sem montar nada")
    p.add_argument("--forcar", action="store_true",
                   help="(ambientes) instala de novo mesmo o que já está montado ou já foi visto insatisfazível")


def _alvos_para_ambientes(so: str | None) -> list[str]:
    """Os alvos da sprint em andamento (sem os que compartilham árvore com outro); fora de uma sprint,
    as worktrees que existem em work/alvos."""
    e = estado.em_andamento(config.RELATORIOS)
    if e is not None and (e.pasta / "alvos.json").exists():
        return [a["nome"] for a in _alvos(e, so) if not a.get("compartilha_com")]
    if not config.ALVOS_DIR.is_dir():
        return []
    return sorted(p.name for p in config.ALVOS_DIR.iterdir() if p.is_dir() and (so is None or p.name == so))


def _cmd_ambientes(args: argparse.Namespace) -> int:
    """`MA servico ambientes [--alvo X] [--listar] [--forcar]`. Requisitos insatisfazíveis são uma
    propriedade do alvo (o serviço dele roda no ambiente comum, com limitação), não uma falha do
    comando; falha é um alvo que devia ter ambiente próprio e ficou sem."""
    cfg = servico.ler_config_ambiente(CAMINHO_CENARIOS)
    saida = []
    ok = True
    for nome in _alvos_para_ambientes(getattr(args, "alvo", None)):
        pasta_alvo = config.ALVOS_DIR / nome
        r: dict = {"alvo": nome}
        if not getattr(args, "listar", False):
            r.update(servico.montar_ambiente(nome, pasta_alvo, cfg.requisitos, versao_python=cfg.python,
                                             forcar=getattr(args, "forcar", False)))
        amb = servico.escolher_ambiente(nome, pasta_alvo, cfg.requisitos)
        r.update({"python_escolhido": str(amb.python), "proprio": amb.proprio,
                  "requisitos": servico._relativo(amb.requisitos, pasta_alvo) if amb.requisitos else None,
                  "insatisfazivel": amb.insatisfazivel, "limitacao": amb.limitacao})
        ok = ok and (amb.proprio or amb.insatisfazivel or getattr(args, "listar", False))
        saida.append(r)
    return _saida({"ambientes": saida, "requisitos_procurados": list(cfg.requisitos), "python": cfg.python}, ok)


@registrar("servico", "sobe o serviço de IA de cada alvo e roda os cenários HTTP (ou monta os ambientes)", _cfg_servico)
def cmd_servico(args: argparse.Namespace) -> int:
    if getattr(args, "acao", "rodar") == "ambientes":
        return _cmd_ambientes(args)
    e = _sprint_atual()
    if not CAMINHO_CENARIOS.exists():
        return _saida({"ok": False, "erro": f"cenários ausentes: {CAMINHO_CENARIOS}"}, False)
    dados_cenarios = servico.carregar_checagens(CAMINHO_CENARIOS)
    if not dados_cenarios.get("script"):
        return _saida({"ok": False, "erro": f"'script' ausente em {CAMINHO_CENARIOS}"}, False)
    itens_servico = _itens_servico(_itens_catalogo())
    _cobertos, faltando = cobertura(itens_servico, dados_cenarios)
    if faltando:
        _acrescentar_limitacoes(e, "servico",
                                [f"item do catálogo sem checagem em servico.yaml: {i}" for i in faltando])
    todos = _alvos(e)
    pendentes = [a for a in _alvos(e, args.alvo) if not a["compartilha_com"] and not e.feito(f"servico:{a['nome']}")]
    resultados = []
    ok_geral = True
    for a in pendentes:
        r = _servico_de_um_alvo(e, a, dados_cenarios, itens_servico)
        # itens sem checagem: nao_testavel também para quem rodou (não silenciosamente ausente)
        for item in faltando:
            catalogo.registrar_resultado(e.pasta, item, a["nome"], "nao_testavel",
                                        "sem checagem definida em privado/cenarios/servico.yaml", fonte="servico")
        ok_geral = ok_geral and r.get("ok", False)
        resultados.append(r)
    _herdar(e, todos)
    if not e.feito("servico") and all(e.feito(f"servico:{a['nome']}") for a in todos if not a["compartilha_com"]):
        e.concluir("servico", {"alvos": len(resultados)})
    return _saida({"resultados": resultados, "itens_sem_checagem": faltando}, ok_geral)
