# featbench

Uma ferramenta para **gerar benchmarks próprios de agentes de código a partir dos seus repositórios**,
seguindo a ideia do artigo *"How Far I Believe We Can Go With AI Replacing Software Developers"*:

> tire um *snapshot* da aplicação antes de uma feature, peça ao agente para implementá-la e verifique
> se a implementação satisfaz os testes que você já tinha — e use *fitness functions* para medir
> performance e respeito à arquitetura.

O featbench:

1. **analisa** um ou mais repositórios e **sugere features** que você talvez queira usar como benchmark;
2. para cada feature, **diz se a implementação original foi boa ou caótica** (🟢 clean / 🟡 mixed / 🔴 chaotic),
   explicando o porquê;
3. **gera benchmarks** que avaliam um agente contra aquela feature em três eixos:
   - **regras de negócio**: os testes da feature, escondidos do agente;
   - **arquitetura**: fitness functions, como regra de dependência entre camadas, complexidade,
     tamanho do diff, novas dependências e localização dos arquivos;
   - **performance**: tempo/métricas com baseline calibrado contra a implementação de referência.

Os benchmarks são **agnósticos ao agente**: o featbench só prepara o workspace e avalia o resultado.
Qualquer agente, skill ou pessoa pode trabalhar no meio do caminho.

---

## Instalação

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'        # depende só de PyYAML (pytest para os testes)
featbench --help
```

Requisitos: Python ≥ 3.11 e git.

## Demonstração em 1 minuto

O repositório de exemplo reproduz o caso do artigo (Login, Sign up, Account Closure, Account Reopening),
com uma feature propositalmente caótica:

```bash
python examples/make_demo_repo.py /tmp/bankapp
featbench analyze /tmp/bankapp -o /tmp/fb/analysis.json
```

```
  #  feature                    source  quality         fit  rec  main issue / note
  1  bankapp.account-reopening  scope   🟢 clean    100   90  ✔    clean reference: its metrics are a good baseline
  2  bankapp.login              scope   🟢 clean    100   90  ✔    clean reference: its metrics are a good baseline
  3  bankapp.signup             merge   🟢 clean    100   90  ✔    clean reference: its metrics are a good baseline
  4  bankapp.account-closure    scope   🔴 chaotic   48   60  ✔    4 of 6 commits inside the feature were fixes
  5  bankapp.statements         scope   🟡 mixed     70   15       no test files were added/changed with the feature
```

```bash
featbench generate /tmp/fb/analysis.json -o /tmp/fb/benchmarks --select account-closure,login
featbench calibrate /tmp/fb/benchmarks

featbench prepare /tmp/fb/benchmarks/bankapp.account-closure -w /tmp/fb/ws
#   ... aqui QUALQUER agente trabalha em /tmp/fb/ws usando /tmp/fb/ws/.featbench/TASK.md como prompt ...
python examples/agents/clean_closure.py /tmp/fb/ws          # agente simulado
featbench evaluate -w /tmp/fb/ws --agent clean-agent --cost 0.12 -o /tmp/fb/results
featbench report /tmp/fb/results
```

```
| agent        | benchmarks | accepted | total | business_rules | architecture | performance | cost (USD) |
| clean-agent  | 1          | 1/1      | 1.00  | 1.00           | 1.00         | 1.00        | 0.12       |
| sloppy-agent | 1          | 1/1      | 0.88  | 1.00           | 0.60         | 1.00        | 0.27       |
| lazy-agent   | 1          | 0/1      | 0.49  | 0.38           | 1.00         | 0.00        | 0.68       |
```

O agente "sloppy" acerta o comportamento, mas importa infraestrutura no domínio, adiciona uma dependência
e deixa um `breakpoint()`. Os testes comportamentais sozinhos não pegariam nada disso; as fitness functions pegam.

---

## Fluxo de trabalho

| comando | o que faz |
|---|---|
| `featbench analyze REPO...` | sugere features, avalia a qualidade de cada uma e grava `analysis.json` e `analysis.md` |
| `featbench generate analysis.json --select ...` | cria uma pasta de benchmark por feature (`--recommended`, `--top N`, `--all`, `--select ids,#n`) |
| `featbench calibrate DIR` | roda a **implementação de referência** (o *baseline* humano) e o **snapshot puro**, grava `calibration.json` e avisa se o benchmark estiver mal formado |
| `featbench prepare BENCH -w WS` | cria o workspace do agente: árvore do snapshot, **sem histórico git** (o agente não consegue "colar" do `git log`) e sem os testes ocultos |
| `featbench evaluate -w WS --agent NOME` | avalia o workspace (numa cópia), injeta os testes ocultos, roda os checks e grava o resultado em JSON |
| `featbench report RESULTS` | leaderboard por agente, por benchmark e por papel (backend/frontend/SRE), com custo por tarefa aceita |
| `featbench checks` | lista os tipos de check e seus parâmetros |
| `featbench validate DIR` | valida os `benchmark.yaml` |

### Como as features são descobertas

Em ordem de prioridade, a partir do histórico git:

1. **branches mergeadas / PRs** (`Merge pull request #12 from org/feature/login`). Branches de bugfix e
   branches de integração longas (`next`, `develop`, mais de 150 commits) são ignoradas;
2. **escopos de conventional commits** (`feat(login): …`, `fix(login): …`);
3. **IDs de ticket** (`ABC-123 …`);
4. commits `feat:` sem escopo;
5. *fallback* por **módulo/diretório**, quando o histórico não segue nenhuma convenção.

### Como a qualidade é avaliada (clean × chaotic)

Cada penalidade vira um motivo legível no relatório. Os pesos ficam em `featbench/analysis/quality.py` (`PENALTIES`).

| sinal | de onde vem |
|---|---|
| sem testes | nenhum arquivo de teste mudou junto com a feature |
| fix-on-fix | proporção de commits de correção *dentro* da feature |
| retrabalho | commits de fix nos 30 dias seguintes que mexem nos arquivos que a feature escreveu |
| reverts | `revert` relacionados |
| *shotgun surgery* | mudanças espalhadas por muitas áreas |
| *thrash* | muitas linhas apagadas em relação às adicionadas |
| complexidade / funções longas | só nas funções que a feature **tocou** (exato em Python, heurístico nas outras linguagens) |
| violação de camadas | imports **introduzidos** pela feature que quebram a regra de dependência |
| commit gigante | difícil de revisar (lockfiles e código gerado não contam) |

Separada da qualidade, a **aptidão para benchmark** (`fit`) considera se há testes (sem eles não dá para
verificar regras de negócio), o tamanho e se o snapshot é viável. Uma feature **caótica** ainda pode ser um
ótimo benchmark: os testes dela continuam valendo para as regras de negócio, mas os limiares de arquitetura e
performance vêm do repositório, e não dela. O `calibrate` mostra exatamente onde a referência falha.

### Snapshot

| estratégia | quando | como |
|---|---|---|
| `base_commit` | commits da feature contíguos ou branch mergeada | árvore do commit anterior à feature |
| `revert_commits` | commits da feature intercalados com outros | árvore da referência com os commits da feature revertidos |
| `remove_paths` | feature desde o primeiro commit / fallback por módulo | árvore da referência sem os caminhos da feature |

---

## Anatomia de um benchmark

```
benchmarks/
├── README.md                 índice da suíte
├── plugins/                  checks customizados (todo *.py é carregado)
│   └── example_plugin.py
└── bankapp.account-closure/
    ├── benchmark.yaml        definição (editável à mão)
    ├── task.md               o "ticket": a única spec que o agente vê
    ├── clarifications.md     perguntas e respostas oficiais para o agente
    ├── hidden/               testes injetados só na avaliação
    └── calibration.json      gerado pelo `calibrate`
```

```yaml
id: bankapp.account-closure
role: backend                       # agrupa o relatório por papel (frontend, backend, SRE...)
snapshot: {strategy: revert_commits, reference_commit: bd0df95, revert_commits: [...]}
hidden_files:
  - {source: hidden/tests/test_closure.py, target: tests/test_closure.py}
setup: []                           # ex.: npm install
scoring:
  weights: {business_rules: 0.5, architecture: 0.3, performance: 0.2}
  gate: {category: business_rules, min_score: 1.0}   # "aceitável pelo sistema"
checks:
  - id: br-test_closure
    category: business_rules
    type: test_command
    weight: 8
    params: {run: '{python} -m pytest -q --junitxml={junit_xml} tests/test_closure.py'}
  - id: arch-layers
    type: layer_dependencies
    params:
      layers:                       # de dentro para fora
        - {name: domain, globs: ['**/domain/**']}
        - {name: application, globs: ['**/application/**']}
        - {name: infrastructure, globs: ['**/infrastructure/**']}
  - id: perf-test-suite
    type: command_duration
    params: {run: '...', repeat: 3, max_seconds: auto, tolerance: 1.5}
```

**Pontuação:** cada check vale de 0 a 1 (crédito parcial, por exemplo 6/8 testes = 0.75). A nota de cada
categoria é a média ponderada dos checks dela, e o total é a média ponderada das categorias. O *gate* diz se a
entrega é **aceitável**. Checks sem nada para medir ficam como `skip` e não contam, e um workspace sem nenhuma
mudança tira 0.

### Tipos de check incluídos

| categoria | tipo | para quê |
|---|---|---|
| regras de negócio | `test_command` | roda testes; crédito parcial via JUnit XML ou saída do pytest/jest/go |
| qualquer | `command` | qualquer script em qualquer linguagem: exit code 0 = passou (contract tests, e2e, smoke…) |
| arquitetura | `layer_dependencies` | regra de dependência (Clean/Hexagonal): camada interna não importa camada externa |
| arquitetura | `forbidden_imports` | ex.: o domínio não pode importar o ORM |
| arquitetura | `complexity`, `function_length`, `file_length` | só no código novo ou alterado pelo agente |
| arquitetura | `diff_size` | o PR é fácil de revisar? (orçamento = 2× o diff da referência) |
| arquitetura | `file_placement` | arquivos novos nos lugares certos |
| arquitetura | `no_new_dependencies` | nada de novas libs sem autorização (pip, npm, go, cargo, maven, gradle…) |
| arquitetura | `forbidden_patterns` | debug esquecido, segredos hard-coded… nas linhas adicionadas |
| performance | `command_duration` | mediana de N execuções ≤ limite absoluto ou `auto` (baseline × tolerância) |
| performance | `command_metric` | qualquer métrica de load test/profiler (k6, locust, wrk…), via JSON ou regex |

Placeholders disponíveis nos comandos: `{python}`, `{workspace}`, `{benchmark_dir}` e `{junit_xml}`.

---

## Estendendo

**1. Editar o YAML.** Tudo é declarativo: mude pesos, limiares, desabilite um check (`enabled: false`),
crie categorias novas (por exemplo `security`, desde que tenha peso em `scoring.weights`) ou troque o gate.

**2. Checks via comando.** Qualquer coisa que rode no terminal vira um check:

```yaml
- id: br-http-contract
  category: business_rules
  type: command
  weight: 2
  params: {run: "bash {benchmark_dir}/scripts/contract_test.sh"}
- id: perf-p95
  type: command_metric
  params: {run: "k6 run --summary-export=/dev/stdout load.js",
           json_path: "metrics.http_req_duration.p(95)", threshold: 250, direction: lower}
```

**3. Novos tipos de check (plugins).** Basta criar um arquivo `.py` em `benchmarks/plugins/` (ou em
`<benchmark>/plugins/`, ou em diretórios listados na variável `FEATBENCH_PLUGINS`):

```python
from featbench.checks import Check, CheckContext, CheckResult, register

@register
class NoSqlInControllers(Check):
    type_name = "no_sql_in_controllers"
    default_category = "architecture"
    description = "Controllers must not contain raw SQL."

    def run(self, ctx: CheckContext) -> CheckResult:
        bad = [p for p in ctx.changed_paths()
               if "/controllers/" in p and "SELECT " in (ctx.read(p) or "")]
        return self.passed() if not bad else self.failed(0.0, "raw SQL in controllers", files=bad)
```

O `CheckContext` oferece: `changes` (arquivos e linhas alterados pelo agente), `read()`, `read_snapshot()`,
`all_files()`, `run_command()`, `calibration` e `calibrating`. Um check que grava `result.calibration =
{"baseline": x}` durante o `calibrate` recebe esse valor de volta nas avaliações seguintes.

**4. Novas linguagens e heurísticas.** Imports, métricas de função, padrões de teste e nomes de camadas ficam
em `featbench/langs.py` e `featbench/analysis/profile.py` (`LAYER_ALIASES`), como tabelas simples.

---

## Integrando com qualquer agente

O featbench não chama agente nenhum. Um loop genérico fica assim:

```bash
for b in benchmarks/*/; do
  ws=$(mktemp -d)/ws
  featbench prepare "$b" -w "$ws"
  start=$(date +%s)
  # >>> substitua pela chamada do seu agente; o prompt é o TASK.md <<<
  my-agent --cwd "$ws" --prompt-file "$ws/.featbench/TASK.md"
  featbench evaluate -w "$ws" --agent my-agent@v3 \
      --agent-seconds $(( $(date +%s) - start )) --cost "$COST_USD" -o results/
done
featbench report results/ -o report.md
```

Assim dá para comparar agentes, versões de um mesmo agente, skills e até pessoas na mesma régua, e medir
**custo por tarefa aceita**. O agente pode (e deve) fazer perguntas: registre as respostas oficiais em
`clarifications.md`, para que todos os avaliados recebam a mesma informação.

---

## Limitações e cuidados

- **Revise o `task.md`.** Ele é gerado a partir de mensagens de commit e nomes de teste, e deve ser reescrito
  como o ticket que um desenvolvedor receberia.
- **Os testes da referência definem o "comportamento correto".** Se a feature não tinha testes, o check de
  regra de negócio é criado desabilitado: escreva os testes.
- **Performance via duração da suíte de testes é só um proxy.** Troque por um load test real (`command_metric`)
  quando tiver um.
- **Métricas de complexidade fora de Python são heurísticas** (regex + chaves). Servem para comparar agentes
  entre si no mesmo repositório, não como verdade absoluta.
- **Classificar qualidade é uma heurística**, com motivos explícitos. Use-a como sugestão e ajuste
  `PENALTIES` ao seu contexto.
- **Segurança:** `evaluate` e `calibrate` executam os comandos do `benchmark.yaml` e o código produzido pelo
  agente. Rode dentro de um container ou VM quando avaliar agentes não confiáveis.
- O `setup` (por exemplo `npm install`) roda a cada avaliação, numa cópia do workspace (sem `node_modules`).

## Estrutura do código

```
featbench/
├── cli.py                 comandos
├── analysis/
│   ├── profile.py         stack, comando de testes, camadas, limiares (p90/p95) do repo
│   ├── features.py        descoberta de features no histórico git
│   └── quality.py         clean × chaotic + aptidão para benchmark
├── generate.py            gera benchmark.yaml, task.md, testes ocultos
├── workspace.py           snapshot isolado (sem histórico)
├── runner.py              evaluate / calibrate / pontuação
├── report.py              leaderboard
├── checks/                framework + checks de negócio, arquitetura e performance
├── langs.py · deps.py · globs.py · gitutil.py
examples/
├── make_demo_repo.py      repo de demonstração (o exemplo do artigo)
└── agents/                três "agentes" simulados: clean, sloppy, lazy
tests/                     testes unitários + ponta a ponta (`pytest`)
```
# benchmark-generator
