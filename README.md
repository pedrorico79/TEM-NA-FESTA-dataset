# Tendências dos produtos do sistema no Google Trends

Este repositório coleta os produtos ativos do Tem na Festa pela API do backend e consulta o nome cadastrado de cada produto no Google Trends. O coletor não usa a planilha `aquisicao_alimentar_por_unidade_da_federacao.xls`; ela permanece apenas como arquivo histórico e não é incluída no pacote da Lambda.

## Fluxo de dados

1. A API do sistema devolve os produtos ativos (`id`, `nome` e `precoVenda`).
2. O nome do produto é enviado ao Google Trends para São Paulo (`BR-SP`), sem mapeamento para uma categoria externa.
3. O coletor grava o CSV bruto e as representações JSON Lines no layout bronze.
4. A infraestrutura cadastra as tabelas no Glue e consulta os dados pelo Athena.

## Arquivos principais

- `script/tnf_tendencias.py`: coleta local e handler da Lambda.
- `script/script-tendencias-produtos-TNF.py`: entrada recomendada para a coleta local.
- `script/script-tratamento-TNF.py`: alias de compatibilidade que também executa a coleta dos produtos do sistema.
- `arquivos-gerados/`: saídas locais, ignoradas pelo Git.

## Execução local

Na raiz do projeto:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
```

Preencha `TNF_API_URL`, `TNF_API_EMAIL` e `TNF_API_PASSWORD` no `.env`, com o backend disponível, e execute:

```bash
python ./script/script-tendencias-produtos-TNF.py
```

O arquivo bruto de conveniência será salvo em `arquivos-gerados/tendencias_produtos_sistema.csv`. O contrato bronze também grava:

```text
arquivos-gerados/datasets/tendencias_produtos/
├── bruto/env=local/data_coleta=AAAA-MM-DD/tendencias_produtos_sistema.csv
├── resumo/env=local/data_coleta=AAAA-MM-DD/resumo.jsonl
├── serie/env=local/data_coleta=AAAA-MM-DD/serie.jsonl
└── rejeicoes/env=local/data_coleta=AAAA-MM-DD/rejeicoes.jsonl
```

`serie.jsonl` tem uma linha por produto e mês; `resumo.jsonl` tem métricas por produto. Produtos sem dados suficientes no Trends ficam em rejeições como aviso e não interrompem os demais.

## Execução na AWS

A Lambda `tnf-<env>-tendencias` coleta os produtos ativos pela API do backend, lê email e senha do SSM e grava o mesmo layout no bucket bronze. O Glue cataloga `tendencias_produtos_resumo`, `tendencias_produtos_serie` e `tendencias_produtos_rejeicoes`; o Athena disponibiliza a visão `vw_tendencias_produtos_atual`.

Na infraestrutura, rode `infra dataset run dev` ou `infra dataset run prod` para iniciar uma coleta, conforme o ambiente. `infra analytics up` configura o Glue e o Athena; não executa uma coleta.

## Google Trends e limites

O PyTrends usa uma interface não oficial. Se o Google limitar consultas com erro 429, o coletor tenta novamente com pausas progressivas. Um produto sem dados suficientes é registrado como aviso; falhas na API, produtos duplicados ou estrutura inválida bloqueiam a coleta.

## Testes

```bash
python -m pytest tests
```

Os testes simulam a API e o Google Trends; não fazem chamadas externas.
