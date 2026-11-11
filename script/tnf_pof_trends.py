"""Base da "Especificação de Base de Dados" (EBD): consumo da POF/IBGE em São
Paulo com a popularidade mensal no Google Trends.

Execução local: python ./script/script-tratamento-TNF.py (grava em
arquivos-gerados/). Na AWS: Lambda semanal com o handler deste módulo, que grava
no bucket bronze (spec 10 da infraestrutura).

Regras de validação da EBD, seção 4.2: todas são bloqueantes nesta base. Uma
falha interrompe a coleta sem gravar nada na partição do dia; só o arquivo de
rejeições é gravado, e a coleta anterior fica intacta.
"""

import logging
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tnf_comum as comum  # noqa: E402

BASE = "pof_trends"
ABA_POF = "Tabela 3.3 Sudeste"
COLUNA_SP = "São Paulo"
ARQUIVO_POF = "aquisicao_alimentar_por_unidade_da_federacao.xls"
ARQUIVO_BRUTO = "produtos_confeitaria_popularidade_mensal.csv"

# Categoria da POF => (produto_id fixo, termo no Google Trends). Toda categoria
# encontrada precisa estar aqui: não há termo de reserva (EBD 3.0, mapeamento).
CATEGORIAS = {
    "Pão doce": (1, "pao doce"),
    "Bolos": (2, "bolo"),
    "Biscoito doce": (3, "biscoito recheado"),
    "Rosca doce": (4, "rosca doce"),
    "Bombom": (5, "bombom"),
    "Chocolate em tablete": (6, "barra de chocolate"),
    "Doce a base de leite": (7, "doce de leite"),
    "Sorvete": (8, "sorvete"),
    "Gelatina": (9, "gelatina"),
    "Chocolate em pó": (10, "achocolatado"),
}

COLUNAS_INICIAIS = ["produto_id", "produto_ibge", "termo_trends", "consumo_kg_sp", "data_coleta"]

logger = logging.getLogger("tnf_dataset")


def pasta_bases():
    """bases-externas/: ao lado do módulo no pacote da Lambda, ou na raiz do repositório."""
    aqui = os.path.dirname(os.path.abspath(__file__))
    for candidata in (os.path.join(aqui, "bases-externas"), os.path.join(aqui, "..", "bases-externas")):
        if os.path.isdir(candidata):
            return candidata
    return os.path.join(aqui, "..", "bases-externas")


def carregar_pof(caminho, rejeicoes):
    """Regra estrutural: a aba e a coluna de São Paulo precisam existir."""
    try:
        planilha = pd.ExcelFile(caminho)
    except Exception as erro:
        rejeicoes.bloquear("estrutural", f"não foi possível ler a planilha da POF ({erro})")
    if ABA_POF not in planilha.sheet_names:
        rejeicoes.bloquear("estrutural", f"a planilha da POF não tem a aba '{ABA_POF}'")

    df = pd.read_excel(planilha, sheet_name=ABA_POF, header=6)
    df = df.rename(columns={"Unnamed: 0": "produto_ibge"})
    df = df.loc[:, ~df.columns.astype(str).str.contains("^Unnamed")]
    df.columns = df.columns.astype(str).str.strip()
    if "produto_ibge" not in df.columns or COLUNA_SP not in df.columns:
        rejeicoes.bloquear("estrutural", f"a aba '{ABA_POF}' não tem a coluna '{COLUNA_SP}'")

    mascara = df["produto_ibge"].astype(str).str.contains("|".join(CATEGORIAS), case=False, na=False)
    doces = df.loc[mascara, ["produto_ibge", COLUNA_SP]].rename(columns={COLUNA_SP: "consumo_kg_sp"}).copy()
    doces["produto_ibge"] = doces["produto_ibge"].astype(str).str.strip()
    doces["consumo_kg_sp"] = pd.to_numeric(doces["consumo_kg_sp"], errors="coerce")
    return doces.reset_index(drop=True)


def mapear(doces, rejeicoes):
    """Regras de mapeamento, identificação, integridade e domínio do consumo."""
    for produto in doces["produto_ibge"]:
        if produto not in CATEGORIAS:
            rejeicoes.bloquear("mapeamento", "categoria da POF sem termo no mapa de busca", produto=produto)
    faltando = sorted(set(CATEGORIAS) - set(doces["produto_ibge"]))
    if faltando:
        rejeicoes.bloquear("mapeamento", "categorias esperadas ausentes da POF: " + ", ".join(faltando))

    doces["produto_id"] = doces["produto_ibge"].map(lambda p: CATEGORIAS[p][0]).astype("Int64")
    doces["termo_trends"] = doces["produto_ibge"].map(lambda p: CATEGORIAS[p][1])

    if doces["produto_id"].isna().any() or doces["produto_id"].duplicated().any():
        rejeicoes.bloquear("identificação", "produto_id nulo ou repetido")
    for _, linha in doces.iterrows():
        if pd.isna(linha["consumo_kg_sp"]):
            rejeicoes.bloquear("integridade", "consumo_kg_sp vazio ou não numérico", linha["produto_id"], linha["produto_ibge"])
        if linha["consumo_kg_sp"] <= 0:
            rejeicoes.bloquear("domínio", "consumo_kg_sp precisa ser maior que zero", linha["produto_id"], linha["produto_ibge"])
    return doces


def coletar(destino, prazo=None, cliente=None, dormir=time.sleep, caminho_pof=None):
    """Executa a coleta e devolve o resumo: status ok ou falha, chaves gravadas."""
    prazo = prazo or comum.Prazo()
    data_coleta = comum.hoje()
    particao = f"data_coleta={data_coleta}"
    rejeicoes = comum.Rejeicoes(data_coleta)
    try:
        doces = mapear(carregar_pof(caminho_pof or os.path.join(pasta_bases(), ARQUIVO_POF), rejeicoes), rejeicoes)
        cliente = cliente or comum.cliente_trends()

        series = []
        termos = doces["termo_trends"].tolist()
        for indice, termo in enumerate(termos):
            logger.info("Coletando a série mensal de '%s'.", termo)
            serie = comum.serie_mensal(cliente, termo, prazo, rejeicoes, dormir)
            if serie is None:
                linha = doces[doces["termo_trends"] == termo].iloc[0]
                rejeicoes.bloquear(
                    "coleta externa",
                    f"sem dados do Google Trends depois de {comum.MAX_TENTATIVAS} tentativas",
                    linha["produto_id"],
                    linha["produto_ibge"],
                )
            serie.name = termo
            series.append(serie)
            if indice < len(termos) - 1:
                prazo.garantir(rejeicoes, comum.INTERVALO_ENTRE_CONSULTAS)
                dormir(comum.INTERVALO_ENTRE_CONSULTAS)

        trends = pd.concat(series, axis=1).T
        trends.columns = [c.strftime("%Y-%m") for c in trends.columns]
        meses = list(trends.columns)
        comum.validar_meses(meses, rejeicoes)
        trends = trends.reset_index().rename(columns={"index": "termo_trends"})
        df = doces.merge(trends, on="termo_trends", how="left")

        for _, linha in df[df[meses].isna().all(axis=1)].iterrows():
            rejeicoes.bloquear("coleta externa", "o Google Trends não retornou nenhum mês", linha["produto_id"], linha["produto_ibge"])
        comum.validar_dominio_trends(df, meses, rejeicoes, "produto_ibge")

        df = comum.calcular_metricas(df, meses)
        for _, linha in df[df["popularidade_media"] == 0].iterrows():
            rejeicoes.bloquear(
                "negócio",
                "popularidade média igual a zero: o fator de sazonalidade não pode ser calculado",
                linha["produto_id"],
                linha["produto_ibge"],
            )

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
            "data_coleta": data_coleta,
            "motivo": str(erro),
            "rejeicoes": len(rejeicoes.linhas),
            "chaves": [chave],
        }


def handler(event, context):
    """Lambda tnf-coleta-pof-trends: grava no bucket de TNF_BRONZE_BUCKET.

    O evento pode trazer "prazo_segundos" para encurtar o prazo num teste
    (D8 da infraestrutura).
    """
    comum.configurar_log()
    limite = (event or {}).get("prazo_segundos")

    def restante_ms():
        real = context.get_remaining_time_in_millis()
        return min(real, limite * 1000) if limite is not None else real

    resultado = coletar(comum.DestinoS3(os.environ["TNF_BRONZE_BUCKET"]), comum.Prazo(restante_ms))
    logger.info("Resultado: %s", resultado)
    return resultado


def main():
    """Execução local: grava em arquivos-gerados/, com o CSV bruto também no
    caminho de antes."""
    comum.configurar_log()
    pasta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arquivos-gerados")
    resultado = coletar(comum.DestinoLocal(pasta))
    if resultado["status"] != "ok":
        print(f"\n[ERRO] {resultado['motivo']}\nNada foi gravado; veja o arquivo de rejeições em {resultado['chaves'][0]}.")
        raise SystemExit(1)
    bruto = resultado["chaves"][0]
    with open(bruto, "rb") as origem, open(os.path.join(pasta, ARQUIVO_BRUTO), "wb") as copia:
        copia.write(origem.read())
    print(f"\n[SUCESSO] {resultado['produtos']} produtos coletados. Arquivo: {os.path.join(pasta, ARQUIVO_BRUTO)}")


if __name__ == "__main__":
    main()
