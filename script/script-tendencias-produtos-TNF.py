import logging
import os
import time

import pandas as pd
import requests
from dotenv import load_dotenv
from pytrends.request import TrendReq


PASTA_SAIDA = "arquivos-gerados"
CAMINHO_SAIDA = f"{PASTA_SAIDA}/tendencias_produtos_sistema.csv"
CAMINHO_TEMPORARIO = f"{CAMINHO_SAIDA}.tmp"
CAMINHO_LOG = f"{PASTA_SAIDA}/pipeline-tendencias.log"

MAX_TENTATIVAS = 3
INTERVALO_ENTRE_CONSULTAS = 20
ESPERA_BASE_NOVA_TENTATIVA = 60
TIMEOUT_API = 30
QUANTIDADE_MESES = 12

EVENTOS_SIMPLES = {
    1: "Verão / Férias Escolares",
    2: "Carnaval",
    3: "Preparação / Antecedência da Páscoa",
    4: "Páscoa / Outono",
    5: "Dia das Mães",
    6: "Dia dos Namorados / Festas Juninas",
    7: "Férias de Inverno",
    8: "Dia dos Pais",
    9: "Início da Primavera",
    10: "Dia das Crianças / Halloween",
    11: "Antecedência de Natal / Black Friday",
    12: "Festas de Fim de Ano / Natal",
}


os.makedirs(PASTA_SAIDA, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(CAMINHO_LOG, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger("tnf_tendencias")


def carregar_configuracao_api():
    load_dotenv()

    configuracao = {
        "url": os.getenv("TNF_API_URL", "").rstrip("/"),
        "email": os.getenv("TNF_API_EMAIL", ""),
        "senha": os.getenv("TNF_API_PASSWORD", ""),
    }

    variaveis_ausentes = [
        nome
        for nome, valor in {
            "TNF_API_URL": configuracao["url"],
            "TNF_API_EMAIL": configuracao["email"],
            "TNF_API_PASSWORD": configuracao["senha"],
        }.items()
        if not valor
    ]

    if variaveis_ausentes:
        raise RuntimeError(
            "Variáveis de ambiente não configuradas: "
            + ", ".join(variaveis_ausentes)
        )

    return configuracao


def buscar_produtos_ativos(configuracao):
    try:
        with requests.Session() as sessao:
            resposta_login = sessao.post(
                f"{configuracao['url']}/auth/login",
                json={
                    "email": configuracao["email"],
                    "senha": configuracao["senha"],
                    "jwtValidityRememberMe": False,
                },
                timeout=TIMEOUT_API,
            )
            resposta_login.raise_for_status()

            resposta_produtos = sessao.get(
                f"{configuracao['url']}/produtos",
                timeout=TIMEOUT_API,
            )
            resposta_produtos.raise_for_status()

    except requests.RequestException as erro:
        raise RuntimeError(f"Falha ao consultar a API de produtos: {erro}") from erro

    try:
        produtos = resposta_produtos.json()
    except ValueError as erro:
        raise RuntimeError("A API retornou uma resposta que não é JSON.") from erro

    if not isinstance(produtos, list):
        raise RuntimeError("A API não retornou uma lista de produtos.")

    produtos_validos = []

    for produto in produtos:
        if not isinstance(produto, dict) or not produto.get("ativo", False):
            continue

        produto_id = produto.get("id")
        produto_nome = str(produto.get("nome") or "").strip()

        if produto_id is None or not produto_nome:
            logger.warning(
                "Produto ativo ignorado porque não possui ID ou nome válido."
            )
            continue

        produtos_validos.append(
            {
                "produto_id": produto_id,
                "produto_nome": produto_nome,
                "preco_venda": produto.get("precoVenda"),
            }
        )

    if not produtos_validos:
        raise RuntimeError("A API não retornou produtos ativos válidos.")

    df_produtos = pd.DataFrame(produtos_validos)
    df_produtos["produto_id"] = pd.to_numeric(
        df_produtos["produto_id"], errors="raise"
    ).astype("Int64")
    df_produtos["preco_venda"] = pd.to_numeric(
        df_produtos["preco_venda"], errors="coerce"
    )

    if df_produtos["produto_id"].duplicated().any():
        raise RuntimeError("A API retornou identificadores de produto duplicados.")

    logger.info(
        "%s produtos ativos foram obtidos da API.",
        len(df_produtos),
    )

    return df_produtos


def extrair_serie_mensal(cliente_trends, produto_nome):
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            cliente_trends.build_payload(
                [produto_nome],
                cat=0,
                timeframe="today 12-m",
                geo="BR-SP",
            )
            dados = cliente_trends.interest_over_time()

            if dados.empty or produto_nome not in dados.columns:
                logger.warning(
                    "O Google Trends não retornou dados para o produto '%s'.",
                    produto_nome,
                )
                return None

            valores = dados[produto_nome]

            if valores.dropna().empty or (valores.fillna(0) == 0).all():
                logger.warning(
                    "O Google Trends retornou somente valores vazios ou zero "
                    "para o produto '%s'.",
                    produto_nome,
                )
                return None

            mes_atual = (
                pd.Timestamp.now(tz="America/Sao_Paulo")
                .tz_localize(None)
                .normalize()
                .replace(day=1)
            )
            meses_esperados = pd.date_range(
                end=mes_atual,
                periods=QUANTIDADE_MESES,
                freq="MS",
            )
            serie_mensal = (
                valores.resample("MS")
                .mean()
                .reindex(meses_esperados)
                .round(2)
            )

            if (
                serie_mensal.dropna().empty
                or (serie_mensal.fillna(0) == 0).all()
            ):
                logger.warning(
                    "O Google Trends não retornou dados válidos nos últimos "
                    "%s meses para o produto '%s'.",
                    QUANTIDADE_MESES,
                    produto_nome,
                )
                return None

            return serie_mensal

        except Exception as erro:
            logger.warning(
                "Tentativa %s de %s falhou para o produto '%s': %s",
                tentativa,
                MAX_TENTATIVAS,
                produto_nome,
                erro,
            )

            if tentativa < MAX_TENTATIVAS:
                espera = ESPERA_BASE_NOVA_TENTATIVA * tentativa
                logger.info(
                    "Aguardando %s segundos antes da próxima tentativa.",
                    espera,
                )
                time.sleep(espera)

    logger.error(
        "Não foi possível coletar o produto '%s' após %s tentativas.",
        produto_nome,
        MAX_TENTATIVAS,
    )
    return None


def coletar_tendencias(df_produtos):
    cliente_trends = TrendReq(hl="pt-BR", tz=180)
    nomes_produtos = df_produtos["produto_nome"].drop_duplicates().tolist()
    series_coletadas = []

    logger.info("Iniciando a coleta no Google Trends para São Paulo.")

    for indice, produto_nome in enumerate(nomes_produtos):
        logger.info("Coletando o produto '%s'.", produto_nome)
        serie = extrair_serie_mensal(cliente_trends, produto_nome)

        if serie is not None:
            serie.name = produto_nome
            series_coletadas.append(serie)

        if indice < len(nomes_produtos) - 1:
            time.sleep(INTERVALO_ENTRE_CONSULTAS)

    if not series_coletadas:
        raise RuntimeError("Nenhum produto retornou dados válidos do Google Trends.")

    df_trends_mensal = pd.concat(series_coletadas, axis=1).T
    df_trends_mensal.columns = [
        coluna.strftime("%Y-%m") for coluna in df_trends_mensal.columns
    ]
    df_trends_mensal = df_trends_mensal.reset_index().rename(
        columns={"index": "produto_nome"}
    )

    produtos_sem_dados = sorted(
        set(nomes_produtos) - set(df_trends_mensal["produto_nome"])
    )

    if produtos_sem_dados:
        logger.warning(
            "%s produtos foram ignorados por não possuírem dados no Google Trends: %s",
            len(produtos_sem_dados),
            ", ".join(produtos_sem_dados),
        )

    df_produtos_com_dados = df_produtos[
        df_produtos["produto_nome"].isin(df_trends_mensal["produto_nome"])
    ].copy()

    return pd.merge(
        df_produtos_com_dados,
        df_trends_mensal,
        on="produto_nome",
        how="inner",
    )


def obter_mes_pico(linha, colunas_meses):
    if linha[colunas_meses].isna().all() or (linha[colunas_meses] == 0).all():
        return pd.NA

    coluna_maxima = linha[colunas_meses].idxmax()
    return int(coluna_maxima.split("-")[1])


def calcular_metricas(df_produtos):
    colunas_meses = [
        coluna
        for coluna in df_produtos.columns
        if len(coluna) == 7 and coluna[4] == "-" and coluna[:4].isdigit()
    ]

    if not colunas_meses:
        raise RuntimeError("A coleta não gerou colunas mensais.")

    df_produtos["popularidade_media"] = (
        df_produtos[colunas_meses].mean(axis=1).round(2)
    )
    df_produtos["popularidade_pico"] = (
        df_produtos[colunas_meses].max(axis=1).round(2)
    )
    df_produtos["mes_pico"] = df_produtos.apply(
        obter_mes_pico,
        axis=1,
        colunas_meses=colunas_meses,
    )
    df_produtos["fator_sazonalidade"] = (
        df_produtos["popularidade_pico"]
        / df_produtos["popularidade_media"].replace(0, pd.NA)
    ).round(2)
    df_produtos["tipo_feriado_pico"] = "Sazonalidade Comercial"
    df_produtos["evento_associado"] = df_produtos["mes_pico"].map(
        EVENTOS_SIMPLES
    )
    df_produtos["data_coleta"] = pd.Timestamp.now(
        tz="America/Sao_Paulo"
    ).strftime("%Y-%m-%d")

    colunas_finais = (
        ["produto_id", "produto_nome", "preco_venda", "data_coleta"]
        + colunas_meses
        + [
            "popularidade_media",
            "popularidade_pico",
            "mes_pico",
            "fator_sazonalidade",
            "tipo_feriado_pico",
            "evento_associado",
        ]
    )

    return df_produtos[colunas_finais].sort_values(
        by="fator_sazonalidade",
        ascending=False,
    )


def salvar_csv(df_final):
    try:
        df_final.to_csv(
            CAMINHO_TEMPORARIO,
            index=False,
            sep=";",
            encoding="utf-8-sig",
        )
        os.replace(CAMINHO_TEMPORARIO, CAMINHO_SAIDA)
    finally:
        if os.path.exists(CAMINHO_TEMPORARIO):
            os.remove(CAMINHO_TEMPORARIO)


def main():
    logger.info("Início da execução do pipeline de tendências.")

    try:
        configuracao = carregar_configuracao_api()
        df_produtos = buscar_produtos_ativos(configuracao)
        df_produtos = coletar_tendencias(df_produtos)
        df_final = calcular_metricas(df_produtos)
        salvar_csv(df_final)

    except Exception as erro:
        logger.error(
            "Execução interrompida: %s O CSV anterior foi preservado.",
            erro,
        )
        raise SystemExit(1) from erro

    print("\n=================== DATASET FINAL MÊS A MÊS ===================")
    print(df_final.to_string(index=False))

    logger.info(
        "Execução concluída com sucesso. Data da coleta: %s. "
        "Produtos gerados: %s. Arquivo: %s",
        df_final["data_coleta"].iloc[0],
        len(df_final),
        CAMINHO_SAIDA,
    )


if __name__ == "__main__":
    main()
