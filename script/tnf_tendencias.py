"""Base dos produtos cadastrados no sistema: o nome de cada produto ativo
como termo do Google Trends.

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
QUANTIDADE_MESES = 12
COLUNAS_INICIAIS = ["produto_id", "produto_nome", "preco_venda", "data_coleta"]

logger = logging.getLogger("tnf_dataset")


def buscar_produtos_ativos(url, email, senha, rejeicoes, sessao=None):
    """Login e GET /produtos; só os ativos, com id e nome."""
    import requests

    sessao = sessao or requests.Session()
    try:
        login = sessao.post(
            f"{url}/auth/login",
            json={"email": email, "senha": senha, "jwtValidityRememberMe": False},
            timeout=TIMEOUT_API,
        )
        login.raise_for_status()
        resposta = sessao.get(f"{url}/produtos", timeout=TIMEOUT_API)
        resposta.raise_for_status()
        produtos = resposta.json()
    except Exception as erro:
        # A mensagem do requests traz só a URL, nunca o corpo do login.
        rejeicoes.bloquear("coleta externa", f"falha ao consultar a API de produtos: {erro.__class__.__name__}: {erro}")

    if not isinstance(produtos, list):
        rejeicoes.bloquear("estrutural", "a API não retornou uma lista de produtos")

    validos = []
    for produto in produtos:
        if not isinstance(produto, dict) or not produto.get("ativo", False):
            continue
        nome = str(produto.get("nome") or "").strip()
        if produto.get("id") is None or not nome:
            rejeicoes.registrar("integridade", "produto ativo sem id ou sem nome", produto.get("id"), nome or None, "aviso")
            continue
        validos.append({"produto_id": produto["id"], "produto_nome": nome, "preco_venda": produto.get("precoVenda")})

    if not validos:
        rejeicoes.bloquear("integridade", "a API não retornou produtos ativos válidos")

    df = pd.DataFrame(validos)
    df["produto_id"] = pd.to_numeric(df["produto_id"], errors="coerce").astype("Int64")
    df["preco_venda"] = pd.to_numeric(df["preco_venda"], errors="coerce")
    if df["produto_id"].isna().any() or df["produto_id"].duplicated().any():
        rejeicoes.bloquear("identificação", "a API retornou produto_id inválido ou repetido")
    logger.info("%s produtos ativos obtidos da API.", len(df))
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
        produtos = buscar_produtos_ativos(url, email, senha, rejeicoes, sessao)
        cliente = cliente or comum.cliente_trends()
        meses_esperados = ultimos_meses()

        series = []
        nomes = produtos["produto_nome"].drop_duplicates().tolist()
        for indice, nome in enumerate(nomes):
            linha = produtos[produtos["produto_nome"] == nome].iloc[0]
            serie = comum.serie_mensal(cliente, nome, prazo, rejeicoes, dormir)
            if serie is not None:
                serie = serie.reindex(meses_esperados)
            if serie is None or serie.dropna().empty or (serie.fillna(0) == 0).all():
                rejeicoes.registrar(
                    "coleta externa",
                    "Sem dados suficientes no Google Trends nos últimos 12 meses",
                    linha["produto_id"],
                    nome,
                    "aviso",
                )
            else:
                serie.name = nome
                series.append(serie)
            if indice < len(nomes) - 1:
                prazo.garantir(rejeicoes, comum.INTERVALO_ENTRE_CONSULTAS)
                dormir(comum.INTERVALO_ENTRE_CONSULTAS)

        if not series:
            rejeicoes.bloquear("coleta externa", "nenhum produto retornou dados válidos do Google Trends")

        trends = pd.concat(series, axis=1).T
        trends.columns = [c.strftime("%Y-%m") for c in trends.columns]
        meses = list(trends.columns)
        comum.validar_meses(meses, rejeicoes)
        trends = trends.reset_index().rename(columns={"index": "produto_nome"})
        df = produtos.merge(trends, on="produto_nome", how="inner")
        comum.validar_dominio_trends(df, meses, rejeicoes, "produto_nome")

        df = comum.calcular_metricas(df, meses)
        df["data_coleta"] = data_coleta
        final = df[COLUNAS_INICIAIS + meses + comum.METRICAS].sort_values("fator_sazonalidade", ascending=False)
        resumo = final[COLUNAS_INICIAIS + comum.METRICAS].to_dict("records")
        arquivos = {
            "bruto": (ARQUIVO_BRUTO, comum.csv_bruto(final)),
            "resumo": ("resumo.jsonl", comum.jsonl(resumo)),
            "serie": ("serie.jsonl", comum.jsonl(comum.registros_serie(final, meses, data_coleta))),
        }
        chaves = comum.gravar_coleta(destino, BASE, particao, arquivos)
        chaves.append(comum.gravar_rejeicoes(destino, BASE, particao, rejeicoes))
        return {
            "status": "ok",
            "base": BASE,
            "ambiente": ambiente,
            "data_coleta": data_coleta,
            "produtos": len(final),
            "rejeicoes": len(rejeicoes.linhas),
            "chaves": chaves,
        }
    except comum.ColetaInterrompida as erro:
        chave = comum.gravar_rejeicoes(destino, BASE, particao, rejeicoes)
        return {
            "status": "falha",
            "base": BASE,
            "ambiente": ambiente,
            "data_coleta": data_coleta,
            "motivo": str(erro),
            "rejeicoes": len(rejeicoes.linhas),
            "chaves": [chave],
        }


def handler(event, context):
    """Lambda tnf-<env>-tendencias. Variáveis: TNF_BRONZE_BUCKET, TNF_API_URL,
    TNF_ENV e TNF_SSM_PREFIX (os segredos ficam no SSM, nunca nas variáveis)."""
    comum.configurar_log()
    limite = (event or {}).get("prazo_segundos")

    def restante_ms():
        real = context.get_remaining_time_in_millis()
        return min(real, limite * 1000) if limite is not None else real

    prefixo = os.environ["TNF_SSM_PREFIX"].rstrip("/")
    resultado = coletar(
        comum.DestinoS3(os.environ["TNF_BRONZE_BUCKET"]),
        os.environ["TNF_API_URL"].rstrip("/"),
        comum.ler_ssm(f"{prefixo}/dataset_api_email"),
        comum.ler_ssm(f"{prefixo}/dataset_api_password"),
        os.environ["TNF_ENV"],
        comum.Prazo(restante_ms),
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
    pasta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arquivos-gerados")
    resultado = coletar(
        comum.DestinoLocal(pasta),
        os.environ["TNF_API_URL"].rstrip("/"),
        os.environ["TNF_API_EMAIL"],
        os.environ["TNF_API_PASSWORD"],
        os.getenv("TNF_ENV", "local"),
    )
    if resultado["status"] != "ok":
        print(f"\n[ERRO] {resultado['motivo']}\nO CSV anterior foi preservado.")
        raise SystemExit(1)
    with open(resultado["chaves"][0], "rb") as origem, open(os.path.join(pasta, ARQUIVO_BRUTO), "wb") as copia:
        copia.write(origem.read())
    print(f"\n[SUCESSO] {resultado['produtos']} produtos coletados. Arquivo: {os.path.join(pasta, ARQUIVO_BRUTO)}")


if __name__ == "__main__":
    main()
