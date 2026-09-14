# Analytics de Confeitaria: Consumo Real vs. Demanda Digital (SP)

Este repositório contém o pipeline completo para coleta, tratamento, análise e visualização de dados de mercado para o setor de confeitaria no Estado de São Paulo. O projeto cruza os dados do consumo físico per capita (POF/IBGE) com o volume de buscas digitais em série temporal (Google Trends).

## 🛠️ Tecnologias Utilizadas

* **Python 3.10+**
* **Pandas:** Manipulação, limpeza de dados e agregação de séries temporais.
* **PyTrends:** Extração automatizada de métricas via API não oficial do Google Trends.
* **Matplotlib:** Construção de gráficos estatísticos customizados para apresentação.

---

## 📂 Estrutura do Projeto

```plaintext
.
├── data/
│   ├── 2026.csv                                         # Base com datas e feriados oficiais
│   ├── aquisicao_alimentar_por_unidade_da_federacao.xls # Tabela bruta POF/IBGE (Sudeste)
│   └── produtos_confeitaria_popularidade_mensal.csv    # Dataset final consolidado
├── scripts/
│   ├── ingestao_e_pipeline_trends.py                    # Pipeline de coleta, tratamento e geração do CSV
│   └── gerar_graficos.py                               # Script para geração dos gráficos comparativos
├── docs/
│   └── documentacao_pipeline.md                        # Documentação técnica detalhada das features
└── README.md
```

---

## 🚀 Como Executar o Projeto

### 1. Pré-requisitos e Instalação
Certifique-se de ter as bibliotecas necessárias instaladas no seu ambiente Python:

```bash
pip install pandas pytrends matplotlib openpyxl
```

### 2. Rodar o Pipeline de Coleta e Tratamento
Execute o script principal para coletar os dados do Google Trends, cruzar com a POF/IBGE e exportar a base consolidada:

```bash
python scripts/ingestao_e_pipeline_trends.py
```

### 3. Gerar os Gráficos de Comparação
Para gerar as visualizações de barras sobrepostas comparando demanda física e busca digital:

```bash
python scripts/gerar_graficos.py
```

---

## 📊 Principais Features Calculadas

* **`popularidade_media`:** Média anual do volume de buscas ($0$ a $100$) para a palavra-chave.
* **`popularidade_pico`:** Pontuação máxima de busca atingida durante a série histórica de 12 meses.
* **`fator_sazonalidade`:** Razão entre a Popularidade de Pico e a Média ($	ext{Pico} / 	ext{Média}$), indicando o quão dependente de datas festivas é o produto.
* **`evento_associado`:** Mapeamento inteligente do mês de pico com o calendário promocional (considerando a antecedência de intenção de compra do consumidor).
