"""Partes comuns das duas coletas do Tem Na Festa.

As duas bases (a da "Especificação de Base de Dados", POF/IBGE + Google
Trends, e a dos produtos cadastrados no sistema) usam o mesmo cliente do
Trends, as mesmas métricas, o mesmo registro de rejeições e as mesmas saídas:
o arquivo bruto no contrato da EBD (seção 2.1) e as versões normalizadas em
JSON Lines (resumo, série mensal e rejeições), gravadas numa pasta local ou no
bucket bronze, sob a partição data_coleta.
"""

import io
import json
import logging
import time
from dataclasses import dataclass, field

import pandas as pd

FUSO = "America/Sao_Paulo"

MAX_TENTATIVAS = 3
INTERVALO_ENTRE_CONSULTAS = 20
ESPERA_BASE_NOVA_TENTATIVA = 60
# Com menos que isso de prazo, a Lambda não começa outra consulta (spec 10 da
# infra, seção 3, item 6).
FOLGA_MINIMA_SEGUNDOS = 120

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

METRICAS = [
    "popularidade_media",
    "popularidade_pico",
    "mes_pico",
    "fator_sazonalidade",
    "tipo_feriado_pico",
    "evento_associado",
]

logger = logging.getLogger("tnf_dataset")


def hoje():
    """Data da coleta: o dia local de São Paulo, como AAAA-MM-DD."""
    return pd.Timestamp.now(tz=FUSO).strftime("%Y-%m-%d")


def mes_corrente():
    return pd.Timestamp.now(tz=FUSO).strftime("%Y-%m")


# --- rejeições e interrupção -------------------------------------------------------


class ColetaInterrompida(Exception):
    """Uma regra bloqueante falhou: nada é gravado na partição do dia."""


@dataclass
class Rejeicoes:
    """Arquivo de rejeições da EBD, seção 4.1: uma linha por problema."""

    data_coleta: str
    linhas: list = field(default_factory=list)

    def registrar(self, regra, motivo, produto_id=None, produto=None, severidade="bloqueante"):
        self.linhas.append(
            {
                "produto_id": None if produto_id is None or pd.isna(produto_id) else int(produto_id),
                "produto": produto,
                "regra": regra,
                "motivo": motivo,
                "severidade": severidade,
                "data_ocorrencia": pd.Timestamp.now(tz=FUSO).isoformat(timespec="seconds"),
            }
        )
        nivel = logging.ERROR if severidade == "bloqueante" else logging.WARNING
        logger.log(nivel, "[%s] %s%s", regra, f"{produto}: " if produto else "", motivo)

    def bloquear(self, regra, motivo, produto_id=None, produto=None):
        self.registrar(regra, motivo, produto_id, produto, "bloqueante")
        raise ColetaInterrompida(f"[{regra}] {motivo}")


# --- prazo -------------------------------------------------------------------------


class Prazo:
    """Tempo restante da execução. Na Lambda vem do context; fora dela, sem limite."""

    def __init__(self, restante_ms=None):
        self._restante_ms = restante_ms

    def segundos(self):
        if self._restante_ms is None:
            return float("inf")
        return self._restante_ms() / 1000

    def garantir(self, rejeicoes, precisa):
        """Interrompe se a próxima etapa não cabe no prazo, com a folga mínima."""
        if self.segundos() - precisa < FOLGA_MINIMA_SEGUNDOS:
            rejeicoes.bloquear(
                "coleta externa",
                "o prazo da execução acabaria antes de terminar a coleta no Google Trends; "
                "a coleta foi interrompida sem gravar nada",
            )


# --- Google Trends -----------------------------------------------------------------


def cliente_trends():
    from pytrends.request import TrendReq

    return TrendReq(hl="pt-BR", tz=180)


def serie_mensal(cliente, termo, prazo, rejeicoes, dormir=time.sleep):
    """Popularidade mensal do termo em SP nos últimos 12 meses, ou None.

    Até três tentativas, com espera crescente entre elas (EBD, seção 4.2,
    coleta externa). Antes de cada consulta e de cada espera, confere o prazo.
    """
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        prazo.garantir(rejeicoes, 0)
        try:
            cliente.build_payload([termo], cat=0, timeframe="today 12-m", geo="BR-SP")
            dados = cliente.interest_over_time()
            if dados.empty or termo not in dados.columns:
                raise ValueError("o Google Trends não retornou dados")
            return dados[termo].resample("MS").mean().round(2)
        except ColetaInterrompida:
            raise
        except Exception as erro:  # o pytrends levanta exceções variadas, inclusive 429
            logger.warning("Tentativa %s de %s falhou para '%s': %s", tentativa, MAX_TENTATIVAS, termo, erro)
            if tentativa < MAX_TENTATIVAS:
                espera = ESPERA_BASE_NOVA_TENTATIVA * tentativa
                prazo.garantir(rejeicoes, espera)
                dormir(espera)
    return None


def colunas_de_meses(df):
    return [c for c in df.columns if isinstance(c, str) and len(c) == 7 and c[4] == "-" and c[:4].isdigit()]


def validar_meses(colunas_meses, rejeicoes):
    """Regra temporal da EBD, seção 4.2: AAAA-MM, sem repetição, em ordem."""
    if not colunas_meses:
        rejeicoes.bloquear("temporal", "a coleta não gerou nenhuma coluna mensal")
    if len(set(colunas_meses)) != len(colunas_meses):
        rejeicoes.bloquear("temporal", "há meses repetidos na série coletada")
    if colunas_meses != sorted(colunas_meses):
        rejeicoes.bloquear("temporal", "os meses da série não estão em ordem cronológica")


def validar_dominio_trends(df, colunas_meses, rejeicoes, rotulo):
    """Regra de domínio: popularidade do Trends entre 0 e 100."""
    valores = df[colunas_meses]
    fora = df[((valores < 0) | (valores > 100)).any(axis=1)]
    for _, linha in fora.iterrows():
        rejeicoes.bloquear("domínio", "popularidade fora do intervalo de 0 a 100", linha["produto_id"], linha[rotulo])


def mes_pico(linha, colunas_meses):
    valores = linha[colunas_meses]
    if valores.isna().all() or (valores == 0).all():
        return pd.NA
    return int(str(valores.astype("float64").idxmax()).split("-")[1])


def calcular_metricas(df, colunas_meses):
    """Métricas da EBD: média, pico, mês do pico, fator de sazonalidade e evento.

    O fator divide o pico pela média; média zero não vira 1 (EBD 3.0): fica
    sem fator, e a validação de negócio decide o que fazer.
    """
    meses = df[colunas_meses].astype("float64")
    df["popularidade_media"] = meses.mean(axis=1).round(2)
    df["popularidade_pico"] = meses.max(axis=1).round(2)
    df["mes_pico"] = df.apply(mes_pico, axis=1, colunas_meses=colunas_meses).astype("Int64")
    df["fator_sazonalidade"] = (
        df["popularidade_pico"] / df["popularidade_media"].replace(0, float("nan"))
    ).round(2)
    df["tipo_feriado_pico"] = "Sazonalidade Comercial"
    df["evento_associado"] = df["mes_pico"].map(EVENTOS_SIMPLES)
    return df


# --- saídas ------------------------------------------------------------------------


def csv_bruto(df):
    """O arquivo do contrato da EBD, seção 2.1: UTF-8 com BOM, ';', '\\r\\n'."""
    texto = df.to_csv(index=False, sep=";", lineterminator="\r\n")
    return "﻿".encode("utf-8") + texto.encode("utf-8")


def _valor_json(valor):
    if valor is None or (not isinstance(valor, (list, dict, str)) and pd.isna(valor)):
        return None
    if hasattr(valor, "item"):
        return valor.item()
    return valor


def jsonl(registros):
    linhas = [json.dumps({k: _valor_json(v) for k, v in r.items()}, ensure_ascii=False) for r in registros]
    return ("\n".join(linhas) + ("\n" if linhas else "")).encode("utf-8")


def registros_serie(df, colunas_meses, data_coleta):
    """Formato longo: uma linha por produto e mês de referência."""
    atual = mes_corrente()
    serie = []
    for _, linha in df.iterrows():
        for mes in colunas_meses:
            serie.append(
                {
                    "produto_id": linha["produto_id"],
                    "mes_referencia": mes,
                    "popularidade": linha[mes],
                    "mes_parcial": mes == atual,
                    "data_coleta": data_coleta,
                }
            )
    return serie


# --- destinos ----------------------------------------------------------------------


class DestinoLocal:
    """Grava numa pasta: execução local (main) e testes."""

    def __init__(self, pasta):
        self.pasta = pasta

    def gravar(self, chave, conteudo):
        import os

        caminho = os.path.join(self.pasta, chave)
        os.makedirs(os.path.dirname(caminho), exist_ok=True)
        temporario = caminho + ".tmp"
        with open(temporario, "wb") as arquivo:
            arquivo.write(conteudo)
        os.replace(temporario, caminho)
        return caminho


class DestinoS3:
    """Grava no bucket bronze: cada objeto vai primeiro para uma chave .tmp e
    só então é copiado por cima do definitivo (carga full e idempotente)."""

    def __init__(self, bucket, cliente=None):
        import boto3

        self.bucket = bucket
        self.s3 = cliente or boto3.client("s3")

    def gravar(self, chave, conteudo):
        temporaria = chave + ".tmp"
        self.s3.put_object(Bucket=self.bucket, Key=temporaria, Body=conteudo)
        self.s3.copy_object(Bucket=self.bucket, Key=chave, CopySource={"Bucket": self.bucket, "Key": temporaria})
        self.s3.delete_object(Bucket=self.bucket, Key=temporaria)
        return f"s3://{self.bucket}/{chave}"


def gravar_coleta(destino, base, particao, arquivos):
    """Grava os arquivos de uma coleta bem-sucedida. arquivos: {tipo: (nome, bytes)}."""
    chaves = []
    for tipo in ("bruto", "resumo", "serie"):
        nome, conteudo = arquivos[tipo]
        chaves.append(destino.gravar(f"datasets/{base}/{tipo}/{particao}/{nome}", conteudo))
    return chaves


def gravar_rejeicoes(destino, base, particao, rejeicoes):
    return destino.gravar(f"datasets/{base}/rejeicoes/{particao}/rejeicoes.jsonl", jsonl(rejeicoes.linhas))


def configurar_log():
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    logger.setLevel(logging.INFO)


def ler_ssm(nome):
    import boto3

    return boto3.client("ssm").get_parameter(Name=nome, WithDecryption=True)["Parameter"]["Value"]


def ler_csv_bytes(conteudo):
    """Lê de volta um CSV bruto (usado nos testes do contrato)."""
    return pd.read_csv(io.BytesIO(conteudo), sep=";", encoding="utf-8-sig")
