"""Testes das duas coletas sem rede: Google Trends e API simulados, planilha
real da POF em bases-externas/. Rodam com: python -m pytest tests"""

import json
import os
import sys

import pandas as pd
import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RAIZ, "script"))

import tnf_comum as comum  # noqa: E402
import tnf_pof_trends as pof  # noqa: E402
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


# --- base da EBD ---------------------------------------------------------------------


def test_coleta_completa_grava_bruto_no_contrato_e_as_normalizadas(tmp_path):
    destino = Destino(tmp_path)
    resultado = pof.coletar(destino, cliente=TrendsFalso(), dormir=nunca_dorme)

    assert resultado["status"] == "ok"
    assert resultado["produtos"] == 10
    particao = f"data_coleta={comum.hoje()}"
    bruto = tmp_path / f"datasets/pof_trends/bruto/{particao}/produtos_confeitaria_popularidade_mensal.csv"
    conteudo = bruto.read_bytes()
    assert conteudo.startswith("﻿".encode("utf-8"))
    assert b"\r\n" in conteudo and b"\n" not in conteudo.replace(b"\r\n", b"")
    cabecalho = conteudo.decode("utf-8-sig").split("\r\n")[0].split(";")
    assert cabecalho[:5] == ["produto_id", "produto_ibge", "termo_trends", "consumo_kg_sp", "data_coleta"]
    assert cabecalho[-6:] == comum.METRICAS
    meses = cabecalho[5:-6]
    assert meses == sorted(meses) and all(len(m) == 7 and m[4] == "-" for m in meses)

    resumo = ler_jsonl(tmp_path / f"datasets/pof_trends/resumo/{particao}/resumo.jsonl")
    assert sorted(r["produto_id"] for r in resumo) == list(range(1, 11))
    assert all(r["consumo_kg_sp"] > 0 and r["fator_sazonalidade"] >= 1 for r in resumo)
    serie = ler_jsonl(tmp_path / f"datasets/pof_trends/serie/{particao}/serie.jsonl")
    assert len(serie) == 10 * len(meses)
    assert sum(1 for s in serie if s["mes_parcial"]) == 10
    assert ler_jsonl(tmp_path / f"datasets/pof_trends/rejeicoes/{particao}/rejeicoes.jsonl") == []


def test_termo_sem_dados_bloqueia_sem_gravar_a_coleta_e_registra_a_rejeicao(tmp_path):
    destino = Destino(tmp_path)
    resultado = pof.coletar(destino, cliente=TrendsFalso(sem_dados={"sorvete"}), dormir=nunca_dorme)

    assert resultado["status"] == "falha"
    assert "coleta externa" in resultado["motivo"]
    assert all("/rejeicoes/" in chave for chave in destino.chaves)
    rejeicoes = ler_jsonl(tmp_path / f"datasets/pof_trends/rejeicoes/data_coleta={comum.hoje()}/rejeicoes.jsonl")
    assert rejeicoes[0]["produto"] == "Sorvete" and rejeicoes[0]["severidade"] == "bloqueante"


def test_planilha_sem_a_aba_bloqueia_pela_regra_estrutural(tmp_path):
    vazia = tmp_path / "pof.xlsx"
    pd.DataFrame({"a": [1]}).to_excel(vazia, sheet_name="Outra", index=False)
    resultado = pof.coletar(Destino(tmp_path), cliente=TrendsFalso(), dormir=nunca_dorme, caminho_pof=str(vazia))
    assert resultado["status"] == "falha"
    assert "[estrutural]" in resultado["motivo"]


def test_categoria_sem_termo_no_mapa_bloqueia(tmp_path, monkeypatch):
    # "Bolo" casa com a linha "Bolos" da POF, mas não é a chave exata do mapa.
    categorias = dict(pof.CATEGORIAS)
    categorias["Bolo"] = categorias.pop("Bolos")
    monkeypatch.setattr(pof, "CATEGORIAS", categorias)
    resultado = pof.coletar(Destino(tmp_path), cliente=TrendsFalso(), dormir=nunca_dorme)
    assert resultado["status"] == "falha"
    assert "[mapeamento]" in resultado["motivo"]


def test_popularidade_media_zero_e_rejeitada(tmp_path):
    class Zerado(TrendsFalso):
        def interest_over_time(self):
            df = super().interest_over_time()
            if self.termo == "gelatina":
                df[self.termo] = 0
            return df

    resultado = pof.coletar(Destino(tmp_path), cliente=Zerado(), dormir=nunca_dorme)
    assert resultado["status"] == "falha"
    assert "[negócio]" in resultado["motivo"]


def test_prazo_curto_interrompe_antes_de_consultar(tmp_path):
    cliente = TrendsFalso()
    resultado = pof.coletar(Destino(tmp_path), comum.Prazo(lambda: 60_000), cliente=cliente, dormir=nunca_dorme)
    assert resultado["status"] == "falha"
    assert "prazo" in resultado["motivo"]
    assert cliente.consultas == []


def test_tres_tentativas_com_espera_crescente(tmp_path):
    esperas = []
    cliente = TrendsFalso(erro=RuntimeError("429"))
    resultado = pof.coletar(Destino(tmp_path), cliente=cliente, dormir=esperas.append)
    assert resultado["status"] == "falha"
    assert cliente.consultas == ["pao doce"] * 3
    assert esperas == [60, 120]


def test_segunda_coleta_do_dia_substitui_a_primeira(tmp_path):
    pof.coletar(Destino(tmp_path), cliente=TrendsFalso(valores={"bolo": 10}), dormir=nunca_dorme)
    pof.coletar(Destino(tmp_path), cliente=TrendsFalso(valores={"bolo": 40}), dormir=nunca_dorme)
    resumo = ler_jsonl(tmp_path / f"datasets/pof_trends/resumo/data_coleta={comum.hoje()}/resumo.jsonl")
    assert len(resumo) == 10
    assert not list(tmp_path.rglob("*.tmp"))


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

    def post(self, url, json, timeout):
        assert url.endswith("/auth/login")
        return Resposta(status=self.status_login)

    def get(self, url, timeout):
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


def test_produto_repetido_na_api_bloqueia(tmp_path):
    repetidos = PRODUTOS[:2] + [{"id": 2, "nome": "Outro", "precoVenda": 1, "ativo": True}]
    resultado = tendencias.coletar(
        Destino(tmp_path), "http://alb/api/v1", "e", "s", "dev",
        cliente=TrendsFalso(), dormir=nunca_dorme, sessao=SessaoFalsa(repetidos),
    )
    assert resultado["status"] == "falha" and "[identificação]" in resultado["motivo"]


@pytest.mark.parametrize("modulo", [pof, tendencias])
def test_handler_existe_com_a_assinatura_da_lambda(modulo):
    assert modulo.handler.__code__.co_varnames[:2] == ("event", "context")
