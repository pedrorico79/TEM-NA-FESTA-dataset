# Tendências dos produtos do sistema no Google Trends

Este repositório coleta os produtos ativos e inativos não deletados do Tem na Festa pela API do backend e consulta o nome cadastrado de cada produto no Google Trends. O coletor não usa a planilha `aquisicao_alimentar_por_unidade_da_federacao.xls`; ela permanece apenas como arquivo histórico e não é incluída no pacote da Lambda.

## Fluxo de dados

1. A API do sistema devolve os produtos ativos e inativos não deletados (`id`, `nome`, `precoVenda` e `ativo`); a consulta não envia o filtro `ativos=true`.
2. O coletor consulta até quatro produtos por lote junto de uma referência comum (padrão: `Bolo de Chocolate`) para obter séries na mesma escala, em São Paulo (`BR-SP`).
3. O índice `interesse_relativo_referencia` é a razão produto/referência × 100. Ele ajuda a comparar os lotes, mas continua sendo um índice relativo do Trends, não volume absoluto de buscas.
4. O coletor grava o CSV bruto e as representações JSON Lines no layout bronze.
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
├── catalogo/env=local/data_coleta=AAAA-MM-DD/catalogo.jsonl
└── rejeicoes/env=local/data_coleta=AAAA-MM-DD/rejeicoes.jsonl
```

`catalogo.jsonl` contém todos os produtos e seu status ativo/inativo. `serie.jsonl` e `resumo.jsonl` incluem `produto_ativo`, o termo de referência e as métricas relativas para os produtos com dados; itens sem dados no Trends ficam nas rejeições como aviso, mas continuam preservados no catálogo.

## Execução na AWS

A Lambda `tnf-<env>-tendencias` coleta os produtos ativos pela API do backend, lê email e senha do SSM e grava o mesmo layout no bucket bronze. O Glue atualmente cataloga `tendencias_produtos_resumo`, `tendencias_produtos_serie` e `tendencias_produtos_rejeicoes`. Antes de publicar esta alteração na AWS, a infraestrutura deve atualizar os schemas Glue para `produto_ativo` e cadastrar o novo caminho `catalogo`; o Athena disponibiliza a visão `vw_tendencias_produtos_atual`.

Na infraestrutura, rode `infra dataset run dev` ou `infra dataset run prod` para iniciar uma coleta, conforme o ambiente. `infra analytics up` configura o Glue e o Athena; não executa uma coleta.

## Produtos inativos e Google Trends

O endpoint do backend lista ativos e inativos não deletados quando o parâmetro `ativos` é omitido. O coletor preserva `produto_ativo` e gera `catalogo.jsonl`; produtos deletados logicamente continuam fora do catálogo. Na massa de demonstração, Cheesecake, Donut e Cupcake são inativos para exercitar a dashboard comercial.

Os produtos são consultados em lotes com uma referência comum (`TNF_TRENDS_REFERENCIA`, padrão `Bolo de Chocolate`). A série inclui o índice nativo do Trends e `interesse_relativo_referencia`, razão entre produto e referência. A métrica relativa permite comparar lotes com mais cuidado; ela não representa buscas absolutas, vendas ou número de pessoas.

## Saídas locais para Grafana

A execução local também acumula `arquivos-gerados/monitoramento/execucoes_coleta.csv` e `eventos_coleta.csv`. Esses arquivos registram início/fim, status, ambiente, duração, quantidade de produtos ativos/inativos, nível, etapa e mensagem, sem gravar credenciais.

## Google Trends e limites

O PyTrends usa uma interface não oficial. Os lotes reduzem chamadas; se o Google limitar consultas com erro 429, o coletor tenta novamente com pausas progressivas. Um produto sem dados suficientes é registrado como aviso; falhas na API, produtos duplicados ou estrutura inválida bloqueiam a coleta.

## Testes

```bash
python -m pytest tests
```

Os testes simulam a API e o Google Trends; não fazem chamadas externas.
