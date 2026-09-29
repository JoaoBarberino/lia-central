# Central LIA — contexto e atividades a partir do Google Drive

Protótipo para o case técnico da Liga IA UFSCar. A aplicação acompanha uma pasta do Google Drive, organiza as atividades da Liga, sugere mudanças a partir de atas (com evidência e revisão humana) e mostra a cada membro o que mudou para ele.

> **Status:** em desenvolvimento (entrega em 04/10/2026). Seções marcadas com _(a completar)_ serão fechadas com as medições reais.

---

## 1. Arquitetura em linguagem simples

```
Google Drive (pasta de teste)            Aplicação (um processo Python)                    Pessoa
┌──────────────────────┐   a cada 3 min  ┌───────────────────────────────┐   navegador   ┌──────────┐
│ INDEX.md, atas, .xlsx│ ──────────────► │ 1. Sincronização               │ ◄───────────► │ Ana, Bruno│
│ Google Docs, PDF...  │   (só leitura)  │ 2. Extratores (md/xlsx/Docs/PDF)│               │ Carla,Davi│
└──────────────────────┘                 │ 3. Regra de autoridade          │               └──────────┘
                                          │ 4. IA propõe → validação → fila │
                                          │ 5. Revisão humana → registro    │
                                          │    oficial + histórico (SQLite) │
                                          └───────────────────────────────┘
```

1. **Sincronização** (`central/sync.py`): lista a pasta e as subpastas, compara o `version` de cada arquivo com o que já foi visto e só baixa o que mudou.
2. **Extratores** (`central/extractors.py`): transformam cada formato em texto ou tabela e leem o cabeçalho dos documentos (`status: ativo`, `substituido_por:`...).
3. **Regra de autoridade** (`central/authority.py`, `central/registry.py`): decide o papel de cada arquivo (índice, registro oficial, ata, histórico, planilha concorrente...). Na interface, o registro oficial aparece como "quadro de atividades".
4. **IA** (`central/ai.py`): lê atas novas ou editadas e **propõe** criar ou alterar atividades. O código valida cada proposta antes de mostrá-la.
5. **Registro oficial** (`central/activities.py`, `central/suggestions.py`): só muda por edição na interface ou por sugestão aprovada. Toda mudança gera um evento com antes/depois, autor, motivo e fonte.

Stack: Python 3.11, FastAPI, Jinja2 (páginas renderizadas no servidor, sem build de JavaScript), SQLite, Google Drive API v3, Gemini API.

## 2. Fonte oficial das atividades (a decisão central)

**Regra única:** a planilha apontada pelo `INDEX.md` é importada **uma vez** para o banco da aplicação. **Depois da importação, o banco é a fonte oficial.** Qualquer coisa que chegue do Drive depois (ata nova, planilha editada, planilha vazia) passa por revisão humana.

Precedência (a mesma do `INDEX.md` do pacote):

1. Decisão humana aprovada na aplicação.
2. Proposta vinda de ata nova: fica **pendente** e nunca se aplica sozinha.
3. Estado atual documentado (`ESTADO-ATUAL.md`): contexto, não altera tarefas.
4. Registro inicial apontado pelo índice: base da primeira importação.
5. Arquivo antigo, homônimo ou sem autoridade confirmada: nunca anula o registro. Vira pendência visível.

Detalhes que sustentam a regra:

- **Vínculo por ID, não por nome.** No primeiro vínculo guardamos o `file_id` da planilha. Renomear mantém a autoridade. Uma planilha nova com o mesmo nome (outro ID) não herda nada.
- **Momento da importação e vínculo com a fonte:** cada atividade importada tem um evento "Importação inicial" e uma referência à linha da planilha (`Atividades!linha N`), com o hash da versão lida.
- **Edição posterior do .xlsx oficial:** comparamos a versão nova **com a versão anterior da planilha** (não com o banco). Só as células que mudaram viram sugestões, com a célula como evidência (`Atividades!D2: '2026-10-05' → '2026-10-07'`). Por isso uma planilha desatualizada não desfaz uma decisão aprovada na aplicação.
- **Linhas removidas ou planilha esvaziada:** nada é apagado. Vira pendência para uma pessoa decidir.
- **Planilha concorrente** (ex.: `Ata - copia vazia.xlsx`): tem as colunas de um registro, mas não é a fonte apontada. Vira pendência "Planilha concorrente" e as atividades continuam intactas. Ser mais recente não dá autoridade a um arquivo.

## 3. Credenciais e pasta do Drive

1. Crie um projeto no Google Cloud (não precisa de faturamento) e ative a **Google Drive API**.
2. **Google Auth Platform → Branding:** nome do app e e-mail de suporte.
3. **Audience:** *External*, deixe em **Testing** e adicione sua conta em **Test users**.
4. **Data Access:** adicione só o escopo `https://www.googleapis.com/auth/drive.readonly`.
5. **Clients → Create client → Web application:**
   - Origem JavaScript: `http://localhost:8000`
   - URI de redirecionamento: `http://localhost:8000/auth/callback`
6. Crie a pasta de teste no seu Drive, coloque os arquivos de `01_CARGA_INICIAL` e copie o ID da pasta (a parte da URL depois de `/folders/`).
   - Para os testes com Google Docs: envie o `.docx`, abra-o no Drive e use **Arquivo → Salvar como Documentos Google** (o "Abrir com" apenas edita o `.docx`, sem converter). Depois apague o `.docx`.
7. Copie `.env.example` para `.env` e preencha `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `DRIVE_TEST_FOLDER_ID` e `SESSION_SECRET`.
8. Para a IA, crie uma chave em https://aistudio.google.com e coloque em `GEMINI_API_KEY`. Sem chave, o app funciona, mas as atas ficam só indexadas, sem sugestões.

**Por que `drive.readonly`:** o case exige detectar arquivos colocados direto na pasta. `drive.file` só enxerga arquivos criados ou escolhidos pelo app. O escopo de leitura vale para a conta toda, então **o código restringe a leitura à pasta configurada e às subpastas** (nunca busca na conta inteira).

Apps em modo *Testing* perdem a autorização após 7 dias. Se a sincronização falhar por credencial, use "Conectar Google Drive" de novo.

## 4. Instalação e execução

Requisitos: Python 3.11+.

```bash
git clone https://github.com/JoaoBarberino/lia-central.git
cd lia-central
python -m venv .venv
# Windows: .venv\Scripts\activate    |   macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env  → depois edite o .env
uvicorn central.app:app --port 8000
```

> **Sempre que abrir um terminal novo:** entre na pasta do projeto e ative o ambiente (`.venv\Scripts\activate` no Windows) antes de rodar o app.

Abra http://localhost:8000, escolha uma pessoa de demonstração, clique no horário de atualização no topo (ou abra `/sincronizacao`), depois em **Conectar Google Drive**, e autorize.

**Sem Drive (avaliação rápida):** use `SOURCE_MODE=local` e `LOCAL_FOLDER=./amostra` com uma cópia de `tests/dados/01_CARGA_INICIAL`. A aplicação trata a pasta como se fosse o Drive. Arquivos `.gdoc` com texto simulam Google Docs nativos.

**Testes automatizados:**

```bash
python -m pytest -q
```

## 5. Processo de sincronização

- **Automática:** uma thread em segundo plano roda a cada `SYNC_INTERVAL_SECONDS` (padrão: 180 s, bem abaixo da meta de 15 min). Em falha, espera mais a cada tentativa (até 10 min) e volta ao normal no primeiro sucesso.
- **Manual:** botão "Atualizar agora", para demonstração e depuração.
- **Listagem:** `files.list` com `'<pasta>' in parents and trashed = false`, percorrendo todas as páginas e subpastas (com conjunto de pastas visitadas).
- **Detecção de mudanças:** compara o `version` do Drive. Quando muda, baixa e calcula o **hash do conteúdo extraído**. Mesmo hash significa nada a processar (idempotência: o mesmo evento duas vezes não gera sugestão nem tarefa duplicada).
- **Renomeado:** mesmo `file_id`, novo nome. Atualiza o nome, não reprocessa e preserva a autoridade.
- **Editado:** nova versão guardada em `source_versions`. A página da fonte mostra o diff. Sugestões pendentes da versão antiga ficam **desatualizadas**.
- **Removido ou sem acesso:** a fonte fica **indisponível**, abre uma pendência e suas sugestões pendentes ficam desatualizadas. Nada é apagado.
- **Falha de leitura de um arquivo:** marca só aquele arquivo com erro e mantém a última versão boa. **Nunca é tratada como arquivo vazio.**
- **Falha na listagem (rede, credencial):** a rodada inteira falha e **nada** é marcado como removido. A barra de status mostra o último sucesso.
- **Por que varredura e não `changes.list`:** para uma pasta pequena, a varredura é mais simples de explicar e já reconcilia o estado inteiro. `changes.list` acompanha a conta toda e exige filtrar pela árvore da pasta. Seria o próximo passo para pastas grandes.

## 6. Formatos suportados

| Formato | Como é lido | Observação |
|---|---|---|
| `.md` | download (`alt=media`) + leitura do cabeçalho | títulos e metadados preservados |
| `.xlsx` | download + `openpyxl` (todas as abas) | prazo guardado como data ISO |
| Google Docs | `files.export` em `text/plain` | limite de 10 MB por exportação da API |
| Google Sheets | `files.export` em `.xlsx` | mesmo leitor da planilha |
| PDF com texto | `pdfplumber` | PDF escaneado aparece como "não processado" (OCR fora do escopo) |
| `.docx` e outros | não lidos | aparecem como "não processado", com o motivo (ex.: "converta para Google Docs") |

## 7. IA: onde entra e como é controlada

- **Leitura assistida de atas:** o modelo recebe a ata (marcada como **dado**, entre delimitadores aleatórios), a lista de membros e as atividades atuais. Ele devolve JSON no contrato da especificação (`kind`, `target_activity_id`, `owners`, `due_date`, `next_step`, `evidence`, `uncertainties`).
- **Validação determinística antes de mostrar qualquer coisa:**
  - a evidência precisa existir **literalmente** no documento;
  - o ID da atividade precisa existir;
  - a data precisa ser ISO válida **e estar escrita no documento** (datas inferidas viram incerteza);
  - os responsáveis precisam ser membros conhecidos citados no texto;
  - campos iguais ao valor oficial são descartados, o que evita sugestão vazia e duplicata.
- **Hipóteses** ("talvez", sem dono nem decisão) viram `no_action`. Ficam registradas como "ideia sem decisão" e não entram no quadro.
- **Tudo que foi barrado** aparece na tela de sugestões, por transparência.
- **Resumo pessoal** ("O que mudou"): montado a partir dos registros, separando mudanças confirmadas de propostas pendentes, sempre com link para a fonte.
- **Avisos no Discord** (opcional, `DISCORD_WEBHOOK_URL` no `.env`): a Central manda ao canal da Liga (1) nova sugestão para revisar, (2) sugestão aceita ou rejeitada, com o motivo, (3) prazo amanhã e prazo hoje e (4) atividade parada ("Isso ainda está valendo?", abaixo). Cada aviso tem chave única e sai uma vez só; ao ligar os avisos, o que já existia é marcado como visto sem mandar nada. Se o Discord estiver fora do ar, o aviso fica na fila e é tentado de novo na próxima sincronização. As mensagens desligam menções (`allowed_mentions`), porque o texto vem de documentos e não pode marcar `@everyone`. O envio acontece numa thread, então a tela não espera o Discord. Na página **Atualização com o Drive** há um botão para mandar uma mensagem de teste.
- **"Isso ainda está valendo?"** (`DIAS_SEM_NOVIDADE` no `.env`, padrão 14; 0 desliga): atividade aberta sem nenhuma novidade nesse período (nenhuma mudança, sugestão aceita ou confirmação) ganha o selo "Sem novidade há N dias" e aparece no topo de **Minhas atividades** do responsável com três respostas: **Continua valendo** (registra no histórico e recomeça a contagem, sem mudar nada no quadro), **Atualizar** (abre a edição) e **Já terminou** (marca como concluída). Só os responsáveis ou quem aprova sugestões podem responder. Atividade com sugestão aguardando revisão não entra, porque já tem novidade chegando. Com os avisos ligados, o Discord recebe uma mensagem por atividade parada, uma vez por período. A ideia vem de bases de conhecimento como o Guru, em que cada conteúdo tem alguém que confirma de tempos em tempos que ele ainda vale: aqui, o quadro não morre desatualizado.
- **Pergunte à Central** (no topo do "Comece aqui"): qualquer membro pergunta em português e recebe uma resposta curta **com o trecho e o link do documento de origem**. A IA recebe os documentos da pasta, o quadro de atividades (fonte oficial) e as sugestões pendentes (marcadas como não oficiais), sempre como dados entre marcas aleatórias. Cada trecho citado é conferido literalmente no documento: se nenhum confere, a resposta não aparece e a Central diz "não encontrei". Documentos substituídos só entram com aviso. Com a IA desligada ou fora do ar, cai numa busca simples por palavras (sem IA). Para responder rápido, as perguntas usam modelos sem raciocínio interno primeiro (`GEMINI_QA_MODELS`, padrão `gemini-3.1-flash-lite`), sem espera entre tentativas: se um modelo estiver sobrecarregado, passa direto ao próximo e, no fim, à busca simples. A mesma pergunta com os mesmos documentos é respondida da memória por até 1 hora; qualquer mudança nos documentos ou no quadro invalida a resposta guardada. As perguntas não ficam gravadas no banco; só o uso de tokens fica registrado (`llm_calls`, propósito `pergunta`).

## 8. Custo estimado por uso

A IA só é chamada quando uma **ata nova ou editada** chega (e quando alguém pede nova análise). Planilhas, índice e demais documentos não passam pelo modelo. Cada chamada fica registrada na tabela `llm_calls` e os totais aparecem na página **Atualização com o Drive**.

**Medição real (28/09/2026, 8 análises de atas com `gemini-3.6-flash`):** 10.544 tokens de entrada e 1.688 de saída, ou seja, **~1.300 de entrada e ~210 de saída por ata**. A entrada inclui as regras, a lista de membros, as atividades atuais e o texto da ata.

| Cenário | Custo por ata | 20 atas/mês |
|---|---|---|
| Camada gratuita do Gemini (usada no protótipo) | US$ 0 | US$ 0 |
| `gemini-3.6-flash` pago (US$ 0,75 / 1M entrada, US$ 3,75 / 1M saída, até 31/12/2026) | ~US$ 0,0018 | ~US$ 0,04 |
| O mesmo modelo com o preço de 2027 (o dobro) | ~US$ 0,0036 | ~US$ 0,07 |
| `gemini-3.1-flash-lite` pago (US$ 0,25 / US$ 1,50) | ~US$ 0,0006 | ~US$ 0,01 |

Observações:
- O custo cresce com o número de atividades, porque a lista atual vai no prompt: com 100 atividades, a entrada sobe para ~6.000 tokens, cerca de US$ 0,005 por ata no modelo principal. Para centenas de atividades, o próximo passo seria enviar só as atividades relacionadas à ata (por frente, IDs citados ou busca).
- Falhas 503 e 429 não são cobradas; as novas tentativas usam espera crescente.
- Na camada gratuita, o Google pode usar os dados enviados para melhorar produtos. Por isso o protótipo usa só os dados fictícios do case. Com dados reais, usar o plano pago (ver seção 10).
- Preços consultados em https://ai.google.dev/gemini-api/docs/pricing em 28/09/2026.

## 9. Limitações conhecidas

- A identidade é por **seleção de pessoa de demonstração**, sem login real.
- Todos os membros veem todas as fontes da pasta: o protótipo não replica as permissões por arquivo do Drive.
- O token OAuth fica no banco local (`data/`, fora do Git), sem criptografia em repouso.
- A varredura completa a cada 3 min atende a uma pasta pequena. Pastas grandes pediriam `changes.list`.
- A IA depende de um serviço externo. Se ele estiver fora, as atas ficam indexadas e são analisadas na próxima sincronização.
- `.docx` não é lido diretamente (o case pede Google Docs nativo).

## 10. Antes de usar dados reais

- Autenticação real (Google Sign-In) e autorização por pessoa, respeitando as permissões de cada arquivo no Drive. Uma pessoa não pode ver trechos de um arquivo ao qual não tem acesso, nem por meio de resumos da IA.
- Tokens em um gerenciador de segredos ou criptografados. HTTPS obrigatório.
- Verificação do app OAuth pelo Google (`drive.readonly` é escopo restrito).
- Política de retenção: guardar o mínimo de texto extraído e apagar o cache quando uma fonte for removida ou o acesso for revogado.
- Revisar os termos do provedor de IA (uso de dados para treino) e preferir um plano pago, sem retenção.

## 11. Ferramentas de IA usadas

- **No desenvolvimento:** Claude (Anthropic), como par de programação para arquitetura, código e testes. Todas as decisões foram revisadas e testadas.
- **No produto:** Gemini API, para extrair sugestões de atas.
