# Feriado + IBGE + Trends (Mês a Mês)

import time
import pandas as pd
from pytrends.request import TrendReq

# ---------------------------------------------------------
# 1. CARREGAR E TRATAR BASE DE FERIADOS (2026.csv)
# ---------------------------------------------------------
caminho_feriados = "bases-externas/feriados_nacionais_2026.csv"

df_feriados = pd.read_csv(caminho_feriados)

col_data = df_feriados.columns[0]
col_nome = df_feriados.columns[1]

df_feriados["Data_dt"] = pd.to_datetime(
    df_feriados[col_data], format="%d/%m/%Y", errors="coerce"
)
df_feriados["mes_pico"] = df_feriados["Data_dt"].dt.month

feriados_oficiais_mes = (
    df_feriados.dropna(subset=["mes_pico"])
    .groupby("mes_pico")[col_nome]
    .apply(lambda x: " / ".join(x))
    .to_dict()
)

datas_comerciais_mes = {
    4: "Páscoa",
    5: "Dia das Mães",
    6: "Dia dos Namorados / Festas Juninas",
    7: "Inverno / Férias Escolares",
    10: "Dia das Crianças / Halloween",
}

# ---------------------------------------------------------
# 2. CARREGAR E TRATAR BASE DA POF - IBGE (SÃO PAULO)
# ---------------------------------------------------------
caminho_pof = "bases-externas/aquisicao_alimentar_por_unidade_da_federacao.xls"

df_pof = pd.read_excel(caminho_pof, sheet_name="Tabela 3.3 Sudeste", header=6)
df_pof = df_pof.rename(columns={"Unnamed: 0": "produto_ibge"})
df_pof = df_pof.loc[:, ~df_pof.columns.str.contains('^Unnamed')]
df_pof.columns = df_pof.columns.str.strip()

# Filtrar produtos de confeitaria
palavras_chave = [
    "Pão doce", "Bolos", "Biscoito doce", "Rosca doce",
    "Bombom", "Chocolate em tablete", "Doce a base de leite",
    "Sorvete", "Gelatina", "Chocolate em pó"
]

mask = df_pof["produto_ibge"].str.contains('|'.join(palavras_chave), case=False, na=False)
df_doces_sp = df_pof[mask][["produto_ibge", "São Paulo"]].copy()
df_doces_sp = df_doces_sp.rename(columns={"São Paulo": "consumo_kg_sp"})
df_doces_sp["consumo_kg_sp"] = pd.to_numeric(df_doces_sp["consumo_kg_sp"], errors="coerce")

# Remove a indentação usada pela planilha do IBGE
df_doces_sp["produto_ibge"] = df_doces_sp["produto_ibge"].astype(str).str.strip()

# Mapeamento ajustado para corresponder exatamente às chaves
mapa_busca = {
    "Pão doce": "pao doce",
    "Bolos": "bolo",
    "Biscoito doce": "biscoito recheado",
    "Rosca doce": "rosca doce",
    "Bombom": "bombom",
    "Chocolate em tablete": "barra de chocolate",
    "Doce a base de leite": "doce de leite",
    "Sorvete": "sorvete",
    "Gelatina": "gelatina",
    "Chocolate em pó": "achocolatado"
}

# Identificador fixo de cada produto no dataset
mapa_id_produto = {
    "Pão doce": 1,
    "Bolos": 2,
    "Biscoito doce": 3,
    "Rosca doce": 4,
    "Bombom": 5,
    "Chocolate em tablete": 6,
    "Doce a base de leite": 7,
    "Sorvete": 8,
    "Gelatina": 9,
    "Chocolate em pó": 10,
}

# Realiza o mapeamento usando os nomes já limpos
df_doces_sp["produto_id"] = (
    df_doces_sp["produto_ibge"].map(mapa_id_produto).astype("Int64")
)
df_doces_sp["termo_trends"] = df_doces_sp["produto_ibge"].map(mapa_busca).fillna(df_doces_sp["produto_ibge"])

# ---------------------------------------------------------
# 3. EXTRAÇÃO MÊS A MÊS VIA PYTRENDS
# ---------------------------------------------------------
pytrends = TrendReq(hl="pt-BR", tz=180)
MAX_TENTATIVAS = 3
INTERVALO_ENTRE_CONSULTAS = 20
ESPERA_BASE_NOVA_TENTATIVA = 60


def extrair_série_mensal(termo):
  for tentativa in range(1, MAX_TENTATIVAS + 1):
    try:
      pytrends.build_payload([termo], cat=0, timeframe="today 12-m", geo="BR-SP")
      dados = pytrends.interest_over_time()

      if dados.empty or termo not in dados.columns:
        raise ValueError("o Google Trends não retornou dados")

      # Agrupa por mês usando média
      dados_mensais = dados[termo].resample("MS").mean().round(2)
      return dados_mensais
    except Exception as erro:
      print(f"Tentativa {tentativa} de {MAX_TENTATIVAS} falhou para '{termo}': {erro}")

      if tentativa < MAX_TENTATIVAS:
        espera = ESPERA_BASE_NOVA_TENTATIVA * tentativa
        print(f"Aguardando {espera} segundos antes de tentar novamente...")
        time.sleep(espera)

  return None


print("\n--- Iniciando Extração Mês a Mês no Google Trends (SP) ---")
lista_series = []

for termo in df_doces_sp["termo_trends"]:
  print(f"Coletando série mensal para: {termo}...")
  serie = extrair_série_mensal(termo)

  if serie is None:
    print(
        f"\n[ERRO] Não foi possível coletar os dados de '{termo}'.\n"
        "O CSV não foi alterado. Aguarde alguns minutos e execute o script novamente."
    )
    raise SystemExit(1)

  serie.name = termo
  lista_series.append(serie)
  time.sleep(INTERVALO_ENTRE_CONSULTAS)

# Junta todas as séries temporais coletadas em um único DataFrame de meses
df_trends_mensal = pd.concat(lista_series, axis=1).T

# Formata os nomes das colunas de datas para YYYY-MM
df_trends_mensal.columns = [
    col.strftime("%Y-%m") for col in df_trends_mensal.columns
]
df_trends_mensal = df_trends_mensal.reset_index().rename(
    columns={"index": "termo_trends"}
)

# Une a série temporal ao DataFrame principal
df_doces_sp = pd.merge(df_doces_sp, df_trends_mensal, on="termo_trends", how="left")

# Identifica quais são as colunas de meses dinamicamente
colunas_meses = [col for col in df_trends_mensal.columns if col != "termo_trends"]

# Impede a geração de um CSV incompleto caso alguma linha fique sem dados
produtos_sem_dados = df_doces_sp.loc[
    df_doces_sp[colunas_meses].isna().all(axis=1), "termo_trends"
].tolist()

if produtos_sem_dados:
  print(
      "\n[ERRO] Produtos sem dados no Google Trends: "
      + ", ".join(produtos_sem_dados)
      + "\nO CSV não foi alterado."
  )
  raise SystemExit(1)

# ---------------------------------------------------------
# 4. ENGENHARIA DE FEATURES E CRUZAMENTO DE EVENTOS
# ---------------------------------------------------------
df_doces_sp["popularidade_media"] = df_doces_sp[colunas_meses].mean(axis=1).round(2)
df_doces_sp["popularidade_pico"] = df_doces_sp[colunas_meses].max(axis=1).round(2)

# Descobre qual mês (1-12) teve o maior valor
def obter_mes_pico(row):
  if row[colunas_meses].isna().all() or (row[colunas_meses] == 0).all():
    return pd.NA
  col_max = row[colunas_meses].idxmax()
  return int(col_max.split("-")[1])

df_doces_sp["mes_pico"] = df_doces_sp.apply(obter_mes_pico, axis=1)

df_doces_sp["fator_sazonalidade"] = (
    df_doces_sp["popularidade_pico"]
    / df_doces_sp["popularidade_media"].replace(0, 1)
).round(2)

# Mapeamento simplificado que já considera a antecedência de compras
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

# Associação direta e limpa
df_doces_sp["evento_associado"] = df_doces_sp["mes_pico"].map(EVENTOS_SIMPLES)
df_doces_sp["tipo_feriado_pico"] = "Sazonalidade Comercial"

# ---------------------------------------------------------
# 5. ORDENAÇÃO E EXIBIÇÃO DA BASE FINAL
# ---------------------------------------------------------
# Registra a data em que os dados foram coletados
df_doces_sp["data_coleta"] = pd.Timestamp.now(tz="America/Sao_Paulo").strftime("%Y-%m-%d")

colunas_finais = (
    ["produto_id", "produto_ibge", "termo_trends", "consumo_kg_sp", "data_coleta"]
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

df_final = df_doces_sp[colunas_finais].sort_values(
    by="fator_sazonalidade", ascending=False
)

print("\n=================== DATASET FINAL MÊS A MÊS ===================")
print(df_final.to_string(index=False))

# ---------------------------------------------------------
# 6. EXPORTAÇÃO PARA ARQUIVO CSV
# ---------------------------------------------------------
caminho_saida = "arquivos-gerados/produtos_confeitaria_popularidade_mensal.csv"

# Exporta com codificação utf-8-sig (compatível com Excel) e separador ponto e vírgula ';'
df_final.to_csv(caminho_saida, index=False, sep=";", encoding="utf-8-sig")

print(f"\n[SUCESSO] Arquivo CSV salvo com sucesso em:\n{caminho_saida}")