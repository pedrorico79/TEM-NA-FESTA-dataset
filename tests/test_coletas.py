"""Testes da coleta dos produtos do sistema sem rede. Rode com python -m pytest tests."""

import json
import os
import sys

import pandas as pd

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RAIZ, "script"))

import tnf_comum as comum  # noqa: E402
import tnf_tendencias as tendencias  # noqa: E402


class TrendsFalso:
    """Imita o TrendReq: semanas dos últimos 12 meses com valores fixos por termo."""

    def __init__(self, valores=None, sem_dados=(), erro=None):
        self.valores = valores or {}
        self.sem_dados = set(sem_dados)
        self.erro = erro
        self.consultas = []
        self.termo = None

    def build_payload(self, termos, **_):
        self.termo = termos[0]
        self.consultas.append(self.termo)

    def interest_over_time(self):
        if self.erro:
            raise self.erro
        if self.termo in self.sem_dados:
            return pd.DataFrame()
        fim = pd.Timestamp.now(tz=comum.FUSO).tz_localize(None).normalize()
        semanas = pd.date_range(end=fim, periods=52, freq="W-SUN")
        base = self.valores.get(self.termo, 50)
        serie = [min(100, base + (i % 12) * 3) for i in range(len(semanas))]
        return pd.DataFrame({self.termo: serie, "isPartial": False}, index=semanas)


class Destino(comum.DestinoLocal):
    def __init__(self, pasta):
        super().__init__(str(pasta))
        self.chaves = []

    def gravar(self, chave, conteudo):
        self.chaves.append(chave)
        return super().gravar(chave, conteudo)


def nunca_dorme(_):
    pass


def ler_jsonl(caminho):
    with open(caminho, encoding="utf-8") as arquivo:
        return [json.loads(linha) for linha in arquivo if linha.strip()]


# --- produtos do sistema -----------------------------------------------------------


class Resposta:
    def __init__(self, dados=None, status=200):
        self.dados = dados
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"{self.status} na API")

    def json(self):
        return self.dados


class SessaoFalsa:
    def __init__(self, produtos=None, status_login=200):
        self.produtos = produtos
        self.status_login = status_login
        self.urls = []

    def post(self, url, json, timeout):
        assert url.endswith("/auth/login")
        return Resposta(status=self.status_login)

    def get(self, url, timeout):
        self.urls.append(url)
        if callable(self.produtos):
            return Resposta(self.produtos(url))
        return Resposta(self.produtos)


PRODUTOS = [
    {"id": 1, "nome": "Bolo de pote", "precoVenda": 12.5, "ativo": True},
    {"id": 2, "nome": "Brigadeiro", "precoVenda": 2.0, "ativo": True},
    {"id": 3, "nome": "Produto inativo", "precoVenda": 1.0, "ativo": False},
]


def test_produtos_do_sistema_produto_sem_dados_vira_aviso_e_os_demais_seguem(tmp_path):
    resultado = tendencias.coletar(
        Destino(tmp_path), "http://alb/api/v1", "coleta@exemplo.com", "s", "dev",
        cliente=TrendsFalso(sem_dados={"Brigadeiro"}), dormir=nunca_dorme, sessao=SessaoFalsa(PRODUTOS),
    )
    assert resultado["status"] == "ok" and resultado["produtos"] == 1
    particao = f"env=dev/data_coleta={comum.hoje()}"
    rejeicoes = ler_jsonl(tmp_path / f"datasets/tendencias_produtos/rejeicoes/{particao}/rejeicoes.jsonl")
    assert rejeicoes[0]["produto"] == "Brigadeiro" and rejeicoes[0]["severidade"] == "aviso"
    resumo = ler_jsonl(tmp_path / f"datasets/tendencias_produtos/resumo/{particao}/resumo.jsonl")
    assert resumo[0]["produto_nome"] == "Bolo de pote" and resumo[0]["preco_venda"] == 12.5


def test_api_fora_do_ar_bloqueia_sem_gravar_e_sem_vazar_a_senha(tmp_path):
    destino = Destino(tmp_path)
    resultado = tendencias.coletar(
        destino, "http://alb/api/v1", "coleta@exemplo.com", "senha-secreta", "dev",
        cliente=TrendsFalso(), dormir=nunca_dorme, sessao=SessaoFalsa(PRODUTOS, status_login=503),
    )
    assert resultado["status"] == "falha"
    assert all("/rejeicoes/" in chave for chave in destino.chaves)
    for arquivo in tmp_path.rglob("*"):
        if arquivo.is_file():
            assert b"senha-secreta" not in arquivo.read_bytes()
    assert "senha-secreta" not in json.dumps(resultado)



def test_produtos_paginados_da_api_sao_lidos_em_todas_as_paginas(tmp_path):
    def paginas(url):
        if url.endswith("page=0"):
            return {"content": PRODUTOS[:1], "last": False}
        return {"content": PRODUTOS[1:], "last": True}

    sessao = SessaoFalsa(paginas)
    resultado = tendencias.coletar(
        Destino(tmp_path), "http://alb/api/v1", "e", "s", "dev",
        cliente=TrendsFalso(), dormir=nunca_dorme, sessao=sessao,
    )
    assert resultado["status"] == "ok" and resultado["produtos"] == 2
    assert [u.rsplit("page=", 1)[1] for u in sessao.urls] == ["0", "1"]
    assert all("ativos=true" in u for u in sessao.urls)


def test_resposta_da_api_sem_lista_bloqueia_pela_regra_estrutural(tmp_path):
    resultado = tendencias.coletar(
        Destino(tmp_path), "http://alb/api/v1", "e", "s", "dev",
        cliente=TrendsFalso(), dormir=nunca_dorme, sessao=SessaoFalsa({"erro": "x"}),
    )
    assert resultado["status"] == "falha" and "[estrutural]" in resultado["motivo"]

def test_produto_repetido_na_api_bloqueia(tmp_path):
    repetidos = PRODUTOS[:2] + [{"id": 2, "nome": "Outro", "precoVenda": 1, "ativo": True}]
    resultado = tendencias.coletar(
        Destino(tmp_path), "http://alb/api/v1", "e", "s", "dev",
        cliente=TrendsFalso(), dormir=nunca_dorme, sessao=SessaoFalsa(repetidos),
    )
    assert resultado["status"] == "falha" and "[identificação]" in resultado["motivo"]


def test_handler_existe_com_a_assinatura_da_lambda():
    assert tendencias.handler.__code__.co_varnames[:2] == ("event", "context")
