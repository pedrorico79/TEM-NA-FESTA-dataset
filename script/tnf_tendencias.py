"""Catálogo de produtos ativos e inativos do sistema como termos do Google Trends.

Execução local: python ./script/script-tendencias-produtos-TNF.py, com a URL e o
login da API no .env (grava em arquivos-gerados/). Na AWS: a Lambda do ambiente,
com a URL da API em TNF_API_URL e o email e a senha lidos do SSM (spec 10 da
infraestrutura, seção 8).

Falhas da API, produto repetido ou nenhum produto com dados são bloqueantes. Um
produto sem dados no Trends não bloqueia: vai para as rejeições como aviso e
fica de fora da coleta ("Sem dados suficientes", EBD, seção 5).
"""

import logging
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tnf_comum as comum  # noqa: E402

BASE = "tendencias_produtos"
ARQUIVO_BRUTO = "tendencias_produtos_sistema.csv"
TIMEOUT_API = 30
TAMANHO_PAGINA = 100
MAXIMO_PAGINAS = 50
QUANTIDADE_MESES = 12
COLUNAS_INICIAIS = ["produto_id", "produto_nome", "produto_ativo", "preco_venda", "termo_referencia", "data_coleta"]
METRICAS_COMPARAVEIS = ["interesse_relativo_referencia_media", "interesse_relativo_referencia_pico"]
TAMANHO_LOTE = 4
TERMO_REFERENCIA_PADRAO = "Bolo de Chocolate"

logger = logging.getLogger("tnf_dataset")


def listar_produtos(sessao, url):
    """GET /produtos sem filtro de ativos: inclui ativos e inativos e a API
    já exclui os produtos deletados logicamente. A API devolve um Page do
    Spring ({"content": [...], "last": ...}); uma lista simples também é aceita.
    Qualquer outro formato devolve None, que a regra estrutural rejeita."""
    produtos = []
    for pagina in range(MAXIMO_PAGINAS):
        resposta = sessao.get(
            f"{url}/produtos?size={TAMANHO_PAGINA}&page={pagina}", timeout=TIMEOUT_API
        )
        resposta.raise_for_status()
        dados = resposta.json()
        if isinstance(dados, list):
            return dados
        if not isinstance(dados, dict) or not isinstance(dados.get("content"), list):
            return None
        produtos.extend(dados["content"])
        if dados.get("last", True) or not dados["content"]:
            break
    return produtos


def buscar_produtos_catalogo(url, email, senha, rejeicoes, sessao=None):
    """Login e GET /produtos; inclui ativos e inativos não deletados."""
    import requests

    sessao = sessao or requests.Session()
    try:
        login = sessao.post(
            f"{url}/auth/login",
            json={"email": email, "senha": senha, "jwtValidityRememberMe": False},
            timeout=TIMEOUT_API,
        )
        login.raise_for_status()
        produtos = listar_produtos(sessao, url)
    except Exception as erro:
        # A mensagem do requests traz só a URL, nunca o corpo do login.
        rejeicoes.bloquear("coleta externa", f"falha ao consultar a API de produtos: {erro.__class__.__name__}: {erro}")

    if not isinstance(produtos, list):
        rejeicoes.bloquear("estrutural", "a API não retornou uma lista de produtos")

    validos = []
    for produto in produtos:
        if not isinstance(produto, dict):
            continue
        nome = str(produto.get("nome") or "").strip()
        if produto.get("id") is None or not nome:
            rejeicoes.registrar("integridade", "produto do catálogo sem id ou sem nome", produto.get("id"), nome or None, "aviso")
            continue
        if not isinstance(produto.get("ativo"), bool):
            rejeicoes.bloquear("integridade", "produto do catálogo sem status ativo válido", produto.get("id"), nome)
        validos.append({
            "produto_id": produto["id"],
            "produto_nome": nome,
            "produto_ativo": produto["ativo"],
            "preco_venda": produto.get("precoVenda"),
        })

    if not validos:
        rejeicoes.bloquear("integridade", "a API não retornou produtos válidos no catálogo")

    df = pd.DataFrame(validos)
    df["produto_id"] = pd.to_numeric(df["produto_id"], errors="coerce").astype("Int64")
    df["preco_venda"] = pd.to_numeric(df["preco_venda"], errors="coerce")
    if df["produto_id"].isna().any() or df["produto_id"].duplicated().any():
        rejeicoes.bloquear("identificação", "a API retornou produto_id inválido ou repetido")
    ativos = int(df["produto_ativo"].sum())
    logger.info("[api_produtos] %s produtos obtidos da API: %s ativos e %s inativos.", len(df), ativos, len(df) - ativos)
    return df


def ultimos_meses():
    atual = pd.Timestamp.now(tz=comum.FUSO).tz_localize(None).normalize().replace(day=1)
    return pd.date_range(end=atual, periods=QUANTIDADE_MESES, freq="MS")


def coletar(destino, url, email, senha, ambiente, prazo=None, cliente=None, dormir=time.sleep, sessao=None):
    prazo = prazo or comum.Prazo()
    data_coleta = comum.hoje()
    particao = f"env={ambiente}/data_coleta={data_coleta}"
    rejeicoes = comum.Rejeicoes(data_coleta)
    try:
        logger.info("[coleta] início da coleta do catálogo de produtos.")
        produtos = buscar_produtos_catalogo(url, email, senha, rejeicoes, sessao)
        cliente = cliente or comum.cliente_trends()
        meses_esperados = ultimos_meses()

        series = {}
        series_referencia = {}
        nomes = produtos["produto_nome"].drop_duplicates().tolist()
        termo_referencia = os.getenv("TNF_TRENDS_REFERENCIA", TERMO_REFERENCIA_PADRAO).strip()
        if not termo_referencia:
            rejeicoes.bloquear("configuração", "TNF_TRENDS_REFERENCIA não pode ser vazio")
        lotes = [nomes[i:i + TAMANHO_LOTE] for i in range(0, len(nomes), TAMANHO_LOTE)]

        for indice_lote, nomes_lote in enumerate(lotes):
            correspondente = next((n for n in nomes_lote if n.casefold() == termo_referencia.casefold()), None)
            referencia_lote = correspondente or termo_referencia
            termos_lote = [referencia_lote] + [n for n in nomes_lote if n.casefold() != termo_referencia.casefold()]
            logger.info("[google_trends] consultando lote %s/%s, referência '%s', %s produtos.", indice_lote + 1, len(lotes), referencia_lote, len(nomes_lote))
            dados_lote = comum.series_mensais_lote(cliente, termos_lote, prazo, rejeicoes, dormir)
            if dados_lote is None:
                for nome in nomes_lote:
                    linha = produtos[produtos["produto_nome"] == nome].iloc[0]
                    rejeicoes.registrar("coleta externa", "Lote sem dados suficientes no Google Trends", linha["produto_id"], nome, "aviso")
            else:
                if referencia_lote not in dados_lote.columns:
                    rejeicoes.bloquear("estrutural", "o Google Trends não retornou a referência comum do lote")
                dados_lote = dados_lote.reindex(meses_esperados)
                serie_ref = dados_lote[referencia_lote].astype("float64")
                for nome in nomes_lote:
                    termo_consultado = referencia_lote if nome.casefold() == termo_referencia.casefold() else nome
                    serie = dados_lote[termo_consultado].astype("float64")
                    if serie.dropna().empty or (serie.fillna(0) == 0).all():
                        linha = produtos[produtos["produto_nome"] == nome].iloc[0]
                        rejeicoes.registrar(
                            "coleta externa",
                            "Sem dados suficientes no Google Trends nos últimos 12 meses",
                            linha["produto_id"],
                            nome,
                            "aviso",
                        )
                    else:
                        series[nome] = serie
                        series_referencia[nome] = (serie.div(serie_ref.where(serie_ref > 0)).mul(100)).round(2)
            if indice_lote < len(lotes) - 1:
                prazo.garantir(rejeicoes, comum.INTERVALO_ENTRE_CONSULTAS)
                dormir(comum.INTERVALO_ENTRE_CONSULTAS)

        if not series:
            rejeicoes.bloquear("coleta externa", "nenhum produto retornou dados válidos do Google Trends")

        trends = pd.DataFrame.from_dict(series, orient="index")
        trends.columns = [c.strftime("%Y-%m") for c in trends.columns]
        meses = list(trends.columns)
        trends.index.name = "produto_nome"
        trends = trends.reset_index()
        relativo = pd.DataFrame.from_dict(series_referencia, orient="index")
        relativo.columns = [f"ref_{c.strftime('%Y-%m')}" for c in relativo.columns]
        relativo.index.name = "produto_nome"
        relativo = relativo.reset_index()
        trends = trends.merge(relativo, on="produto_nome", how="left")
        comum.validar_meses(meses, rejeicoes)
        df = produtos.merge(trends, on="produto_nome", how="inner")
        comum.validar_dominio_trends(df, meses, rejeicoes, "produto_nome")

        colunas_referencia = [f"ref_{m}" for m in meses]
        df = comum.calcular_metricas(df, meses)
        df["termo_referencia"] = termo_referencia
        df["interesse_relativo_referencia_media"] = df[colunas_referencia].mean(axis=1).round(2)
        df["interesse_relativo_referencia_pico"] = df[colunas_referencia].max(axis=1).round(2)
        df["data_coleta"] = data_coleta
        metricas_saida = comum.METRICAS + METRICAS_COMPARAVEIS
        final = df[COLUNAS_INICIAIS + meses + colunas_referencia + metricas_saida].sort_values("interesse_relativo_referencia_media", ascending=False)
        resumo = final[COLUNAS_INICIAIS + metricas_saida].to_dict("records")
        catalogo = produtos.copy()
        catalogo["data_coleta"] = data_coleta
        arquivos = {
            "bruto": (ARQUIVO_BRUTO, comum.csv_bruto(final)),
            "resumo": ("resumo.jsonl", comum.jsonl(resumo)),
            "serie": ("serie.jsonl", comum.jsonl(comum.registros_serie(final, meses, data_coleta))),
            "catalogo": ("catalogo.jsonl", comum.jsonl(catalogo.to_dict("records"))),
        }
        chaves = comum.gravar_coleta(destino, BASE, particao, arquivos)
        chaves.append(comum.gravar_rejeicoes(destino, BASE, particao, rejeicoes))
        resultado = {
            "status": "ok",
            "base": BASE,
            "ambiente": ambiente,
            "data_coleta": data_coleta,
            "produtos_catalogo": len(produtos),
            "produtos_ativos": int(produtos["produto_ativo"].sum()),
            "produtos_inativos": int((~produtos["produto_ativo"]).sum()),
            "produtos_com_dados": len(final),
            "lotes_consultados": len(lotes),
            "termo_referencia": termo_referencia,
            "rejeicoes": len(rejeicoes.linhas),
            "chaves": chaves,
        }
        logger.info("[resultado] %s", resultado)
        return resultado
    except comum.ColetaInterrompida as erro:
        chave = comum.gravar_rejeicoes(destino, BASE, particao, rejeicoes)
        resultado = {
            "status": "falha",
            "base": BASE,
            "ambiente": ambiente,
            "data_coleta": data_coleta,
            "motivo": str(erro),
            "rejeicoes": len(rejeicoes.linhas),
            "chaves": [chave],
        }
        logger.error("[resultado] %s", resultado)
        return resultado


def handler(event, context):
    """Lambda tnf-<env>-tendencias. Variáveis: TNF_BRONZE_BUCKET, TNF_API_URL,
    TNF_ENV e TNF_SSM_PREFIX (os segredos ficam no SSM, nunca nas variáveis)."""
    comum.configurar_log()
    limite = (event or {}).get("prazo_segundos")

    def restante_ms():
        real = context.get_remaining_time_in_millis()
        return min(real, limite * 1000) if limite is not None else real

    ambiente = os.environ["TNF_ENV"]
    prefixo = os.environ["TNF_SSM_PREFIX"].rstrip("/")
    destino = comum.DestinoS3(os.environ["TNF_BRONZE_BUCKET"])
    inicio = pd.Timestamp.now(tz=comum.FUSO)
    execucao_id = f"{ambiente}-{inicio.strftime('%Y%m%dT%H%M%S%z')}-{context.aws_request_id}"
    eventos = []

    class CapturaLogs(logging.Handler):
        def emit(self, registro):
            instante = pd.Timestamp(registro.created, unit="s", tz="UTC").tz_convert(comum.FUSO)
            mensagem = registro.getMessage()
            etapa = mensagem[1:].split("]", 1)[0] if mensagem.startswith("[") and "]" in mensagem else "geral"
            eventos.append({
                "timestamp": instante.isoformat(timespec="seconds"),
                "execucao_id": execucao_id,
                "nivel": registro.levelname,
                "etapa": etapa,
                "mensagem": mensagem,
            })

    captura = CapturaLogs()
    logger.addHandler(captura)
    try:
        resultado = coletar(
            destino,
            os.environ["TNF_API_URL"].rstrip("/"),
            comum.ler_ssm(f"{prefixo}/dataset_api_email"),
            comum.ler_ssm(f"{prefixo}/dataset_api_password"),
            ambiente,
            comum.Prazo(restante_ms),
        )
    except Exception as erro:
        # Guarda só o tipo da exceção para não levar dados sensíveis aos logs.
        logger.error("[execucao] falha inesperada: %s", erro.__class__.__name__)
        resultado = {
            "status": "falha",
            "base": BASE,
            "ambiente": ambiente,
            "data_coleta": comum.hoje(),
            "motivo": erro.__class__.__name__,
            "rejeicoes": 0,
        }
    finally:
        logger.removeHandler(captura)

    fim = pd.Timestamp.now(tz=comum.FUSO)
    resumo = {
        "execucao_id": execucao_id,
        "inicio": inicio.isoformat(timespec="seconds"),
        "fim": fim.isoformat(timespec="seconds"),
        "status": resultado.get("status", "falha"),
        "data_coleta": resultado.get("data_coleta"),
        "produtos_catalogo": resultado.get("produtos_catalogo"),
        "produtos_ativos": resultado.get("produtos_ativos"),
        "produtos_inativos": resultado.get("produtos_inativos"),
        "produtos_com_dados": resultado.get("produtos_com_dados"),
        "rejeicoes": resultado.get("rejeicoes"),
        "duracao_segundos": round((fim - inicio).total_seconds(), 2),
        "motivo": resultado.get("motivo", ""),
    }
    particao = f"env={ambiente}/year={inicio.year}/month={inicio.month:02d}/day={inicio.day:02d}"
    destino.gravar(
        f"monitoramento/execucoes/{particao}/{execucao_id}.jsonl",
        comum.jsonl([resumo]),
    )
    if eventos:
        destino.gravar(
            f"monitoramento/eventos/{particao}/{execucao_id}.jsonl",
            comum.jsonl(eventos),
        )

    logger.info("Resultado: %s", resultado)
    return resultado


def main():
    """Execução local, com TNF_API_URL, TNF_API_EMAIL e TNF_API_PASSWORD no .env."""
    from dotenv import load_dotenv

    comum.configurar_log()
    load_dotenv()
    faltando = [n for n in ("TNF_API_URL", "TNF_API_EMAIL", "TNF_API_PASSWORD") if not os.getenv(n)]
    if faltando:
        print("Variáveis de ambiente não configuradas: " + ", ".join(faltando))
        raise SystemExit(1)
    import csv
    pasta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arquivos-gerados")
    ambiente = os.getenv("TNF_ENV", "local")
    inicio = pd.Timestamp.now(tz=comum.FUSO)
    eventos = []

    class CapturaLogs(logging.Handler):
        def emit(self, registro):
            instante = pd.Timestamp(registro.created, unit="s", tz="UTC").tz_convert(comum.FUSO)
            mensagem = registro.getMessage()
            etapa = mensagem[1:].split("]", 1)[0] if mensagem.startswith("[") and "]" in mensagem else "geral"
            eventos.append({
                "timestamp": instante.isoformat(timespec="seconds"),
                "ambiente": ambiente,
                "execucao_id": execucao_id,
                "nivel": registro.levelname,
                "etapa": etapa,
                "mensagem": mensagem,
            })

    execucao_id = f"{ambiente}-{inicio.strftime('%Y%m%dT%H%M%S%z')}"
    captura = CapturaLogs()
    logger.addHandler(captura)
    try:
        resultado = coletar(
            comum.DestinoLocal(pasta),
            os.environ["TNF_API_URL"].rstrip("/"),
            os.environ["TNF_API_EMAIL"],
            os.environ["TNF_API_PASSWORD"],
            ambiente,
        )
    except Exception as erro:
        # Registra só o tipo da exceção; evita incluir detalhes sensíveis na saída.
        logger.error("[execucao] falha inesperada: %s", erro.__class__.__name__)
        resultado = {
            "status": "falha",
            "data_coleta": comum.hoje(),
            "motivo": erro.__class__.__name__,
            "rejeicoes": 0,
        }
    finally:
        logger.removeHandler(captura)
    fim = pd.Timestamp.now(tz=comum.FUSO)
    monitoramento = os.path.join(pasta, "monitoramento")
    os.makedirs(monitoramento, exist_ok=True)
    resumo = {
        "execucao_id": execucao_id,
        "inicio": inicio.isoformat(timespec="seconds"),
        "fim": fim.isoformat(timespec="seconds"),
        "ambiente": ambiente,
        "data_coleta": resultado.get("data_coleta"),
        "status": resultado.get("status"),
        "produtos_catalogo": resultado.get("produtos_catalogo"),
        "produtos_ativos": resultado.get("produtos_ativos"),
        "produtos_inativos": resultado.get("produtos_inativos"),
        "produtos_com_dados": resultado.get("produtos_com_dados"),
        "rejeicoes": resultado.get("rejeicoes"),
        "duracao_segundos": round((fim - inicio).total_seconds(), 2),
        "motivo": resultado.get("motivo", ""),
    }

    def anexar_csv(caminho, campos, linhas):
        existe = os.path.exists(caminho)
        with open(caminho, "a", encoding="utf-8-sig", newline="") as arquivo:
            escritor = csv.DictWriter(arquivo, fieldnames=campos, extrasaction="ignore")
            if not existe or os.path.getsize(caminho) == 0:
                escritor.writeheader()
            escritor.writerows(linhas)

    campos_resumo = list(resumo)
    campos_eventos = ["timestamp", "ambiente", "execucao_id", "nivel", "etapa", "mensagem"]
    anexar_csv(os.path.join(monitoramento, "execucoes_coleta.csv"), campos_resumo, [resumo])
    anexar_csv(os.path.join(monitoramento, "eventos_coleta.csv"), campos_eventos, eventos)

    if resultado["status"] != "ok":
        print(f"\n[ERRO] {resultado['motivo']}\nO CSV anterior foi preservado.")
        raise SystemExit(1)
    with open(resultado["chaves"][0], "rb") as origem, open(os.path.join(pasta, ARQUIVO_BRUTO), "wb") as copia:
        copia.write(origem.read())
    print(
        f"\n[SUCESSO] Catálogo: {resultado['produtos_ativos']} ativos + "
        f"{resultado['produtos_inativos']} inativos; {resultado['produtos_com_dados']} com dados no Trends. "
        f"Bruto: {os.path.join(pasta, ARQUIVO_BRUTO)}"
    )
    print(f"Monitoramento local: {monitoramento}")


if __name__ == "__main__":
    main()
