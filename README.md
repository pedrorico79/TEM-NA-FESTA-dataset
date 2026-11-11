# Analytics de Confeitaria: Consumo Real vs. Demanda Digital (SP)

Este projeto gera um dataset para análise do mercado de confeitaria no Estado de
São Paulo. O script combina dados de consumo da POF/IBGE com o interesse de busca
dos últimos 12 meses no Google Trends.

## Tecnologias utilizadas

- Python 3.10+
- Pandas para leitura, limpeza e transformação dos dados
- PyTrends para consulta ao Google Trends
- xlrd para leitura da planilha `.xls` do IBGE
- Requests para consumir a API do sistema
- python-dotenv para carregar a configuração local

## Estrutura do projeto

```text
.
├── bases-externas/
│   ├── aquisicao_alimentar_por_unidade_da_federacao.xls
│   └── feriados_nacionais_2026.csv
├── script/
│   ├── script-tratamento-TNF.py
│   └── script-tendencias-produtos-TNF.py
├── arquivos-gerados/
│   └── .gitkeep
├── .env.example
├── .gitignore
└── README.md
```

A pasta `arquivos-gerados` é mantida no repositório pelo arquivo `.gitkeep`, mas
os CSVs produzidos pelo script são ignorados pelo Git.

## Como executar

Os comandos devem ser executados na pasta raiz do projeto.

### 1. Criar o ambiente virtual

```bash
python -m venv .venv
```

### 2. Ativar o ambiente virtual

No PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

No Git Bash:

```bash
source .venv/Scripts/activate
```

### 3. Instalar as dependências

```bash
python -m pip install -r requirements-dev.txt
```

### 4. Executar o script

```bash
python ./script/script-tratamento-TNF.py
```

A coleta possui intervalos entre as consultas e pode levar alguns minutos. Ao
final, o seguinte arquivo será criado ou atualizado:

```text
arquivos-gerados/produtos_confeitaria_popularidade_mensal.csv
```

## Coleta dos produtos cadastrados no sistema

O segundo script consulta os produtos ativos do backend e utiliza diretamente o
nome cadastrado como termo de pesquisa no Google Trends.

Copie `.env.example` para `.env` e preencha a URL e as credenciais locais. O
arquivo `.env` contém informações sensíveis e é ignorado pelo Git.

Com o backend em execução, rode:

```bash
python ./script/script-tendencias-produtos-TNF.py
```

O resultado será salvo em:

```text
arquivos-gerados/tendencias_produtos_sistema.csv
```

Produtos inativos não são coletados. Um produto sem retorno no Google Trends é
registrado no log e ignorado, permitindo que os demais sejam processados. O log
da execução fica em `arquivos-gerados/pipeline-tendencias.log`.

O novo CSV possui exatamente 12 colunas mensais: o mês atual, ainda parcial, e
os 11 meses anteriores.

## Dados gerados

O CSV contém:

- produto e consumo per capita em São Paulo, segundo a POF/IBGE;
- popularidade mensal do termo no Google Trends;
- média e pico de popularidade;
- mês de maior interesse;
- fator de sazonalidade;
- evento comercial associado ao mês de pico.

O fator de sazonalidade é calculado dividindo a popularidade de pico pela
popularidade média do produto.

## Falhas do Google Trends

O PyTrends utiliza uma interface não oficial e o Google pode limitar as consultas
temporariamente, retornando o erro `429`. Nessa situação, o script faz até três
tentativas com intervalos progressivos.

Se todas as tentativas falharem, a execução é interrompida e o CSV existente não
é substituído por um resultado incompleto. Aguarde alguns minutos antes de tentar
novamente.

## Validação e rejeições

As duas coletas aplicam as regras de validação da "Especificação de Base de
Dados" (seção 4.2): estrutural, identificação, domínio, integridade, temporal,
mapeamento, negócio e coleta externa. Na base da POF todas são bloqueantes: se
uma falhar, nada é gravado e o CSV anterior fica intacto. Na base dos produtos
do sistema, um produto sem dados no Google Trends não bloqueia; ele vai para as
rejeições como aviso. Cada execução grava também o arquivo de rejeições
(`rejeicoes.jsonl`), com produto, regra, motivo, severidade e data.

Além do CSV de antes, a execução grava em `arquivos-gerados/datasets/` o mesmo
layout usado na AWS: o arquivo bruto, o resumo e a série mensal em JSON Lines,
sob a partição `data_coleta=AAAA-MM-DD`.

## Execução na AWS (Lambda)

A lógica fica nos módulos `script/tnf_pof_trends.py` e `script/tnf_tendencias.py`,
cada um com o `main()` da execução local e um `handler(event, context)` para a
Lambda. A infraestrutura (`TEM-NA-FESTA-infraestrutura`, spec 10) empacota este
repositório numa tag fixa, com as dependências de `requirements-lambda.txt`, e
grava as coletas no bucket bronze. Na Lambda, a URL da API vem de variável de
ambiente e o email e a senha vêm do SSM, nunca de `.env`.

## Testes

```bash
python -m pytest tests
```

Os testes não usam a rede: o Google Trends e a API são simulados, e a planilha
da POF é a de `bases-externas/`.
