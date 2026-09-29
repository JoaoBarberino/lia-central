# Central LIA — contexto e atividades a partir do Google Drive

Protótipo para o case técnico da Liga IA UFSCar. A aplicação acompanha uma pasta do Google Drive, organiza as atividades da Liga, sugere mudanças a partir de atas (com evidência e revisão humana) e mostra a cada membro o que mudou para ele.

> **Avaliar em 3 minutos, sem conta Google:** seção 4, "Sem Drive". Três comandos rodam a Central com os dados fictícios do pacote.

---

## 1. Arquitetura em linguagem simples

```
Google Drive (pasta de teste)            Aplicação (um processo Python)                    Pessoa
┌──────────────────────┐   a cada 3 min  ┌───────────────────────────────┐   navegador   ┌──────────┐
│ INDEX.md, atas, .xlsx│ ──────────────► │ 1. Sincronização               │ ◄───────────► │ Ana, Bruno│
│ Google Docs, PDF...  │   (só leitura)  │ 2. Extratores (md, xlsx, Docs…) │               │ Carla,Davi│
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
   - O `.docx` do pacote é lido direto. Para testar também o Google Docs nativo: abra o `.docx` no Drive e use **Arquivo → Salvar como Documentos Google** (o "Abrir com" apenas edita o `.docx`, sem converter). Não deixe as duas versões na pasta ao mesmo tempo, para não duplicar a mesma ata.
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

Abra http://localhost:8000, escolha uma pessoa de demonstração, abra **Estado da sincronização** no menu, clique em **Conectar Google Drive** e autorize. A página mostra a pasta conectada, o horário da última atualização e o resultado de cada verificação.

**Sem Drive (avaliação rápida, sem conta Google e sem chave de IA):** não precisa de `.env`. Sem ele, a Central lê a pasta local `amostra/` e a IA fica desligada.

```bash
mkdir amostra
cp tests/dados/01_CARGA_INICIAL/* amostra/          # Windows: copy tests\dados\01_CARGA_INICIAL\* amostra\
uvicorn central.app:app --port 8000
```

A pasta `amostra/` faz o papel do Drive: copie para ela um arquivo de `tests/dados/02_ADICIONAR_DEPOIS_DA_CARGA` ou `03_CONFLITO` e clique em **Atualizar agora** (ou espere a verificação automática). Arquivos `.gdoc` com texto simulam Google Docs nativos. Sem a IA, as atas são lidas, mas não geram sugestões; a edição de uma planilha oficial gera sugestões mesmo assim, porque essa comparação não usa IA.

**Testes automatizados:**

```bash
python -m pytest -q
```

## 5. Processo de sincronização

- **Automática:** uma thread em segundo plano roda a cada `SYNC_INTERVAL_SECONDS` (padrão: 180 s, bem abaixo da meta de 15 min). Em falha, espera mais a cada tentativa (até 10 min) e volta ao normal no primeiro sucesso.
- **Manual:** botão "Atualizar agora", para demonstração e depuração.
- **Onde ver:** a página **Estado da sincronização** mostra a pasta conectada (nome, link e ID), a restrição "só esta pasta e as subpastas", a última atualização bem-sucedida e, em cada verificação, quantos arquivos foram lidos, ficaram sem mudança, não foram processados (formato) ou deram erro.
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
| Google Slides | `files.export` em `text/plain` | texto dos slides |
| `.docx` (Word) | download + leitura do XML interno (sem biblioteca extra) | parágrafos, quebras e tabelas (uma linha por linha da tabela); lido como Google Docs |
| `.pptx` (PowerPoint) | download + leitura do XML de cada slide | texto em ordem, com "Slide N" |
| `.csv` | download + `csv` (detecta `,` `;` ou tab; UTF-8 ou Windows-1252) | vira planilha de uma aba: com colunas de registro, é "planilha parecida com a oficial" e vira pendência |
| `.txt` | download | como o `.md` |
| PDF com texto | `pdfplumber` | — |
| PDF escaneado, imagens (`.png`, `.jpg`, `.webp`, `.heic`) | não lidos automaticamente | "não processado", com o motivo (OCR fora do escopo). Nada é inventado. Opcional: **Transcrever com IA** (abaixo) |
| `.doc`, `.ppt` (formatos antigos) e outros | não lidos | "não processado", com o motivo e como resolver (ex.: "salve como .docx") |
| Vídeo, áudio, arquivo compactado, Formulários e Desenhos Google | não lidos e **nem baixados** | "não processado", com o motivo |

Toda leitura acima é **determinística, sem IA**: o texto que entra é exatamente o do arquivo. Arquivo corrompido ou protegido por senha vira erro visível em **Pendências**, sem apagar a última versão boa.

## 7. IA: onde entra e como é controlada

- **Leitura assistida de atas:** o modelo recebe a ata (marcada como **dado**, entre delimitadores aleatórios), a lista de membros e as atividades atuais. Ele devolve JSON no contrato da especificação (`kind`, `target_activity_id`, `owners`, `due_date`, `next_step`, `evidence`, `uncertainties`).
- **Validação determinística antes de mostrar qualquer coisa:**
  - a evidência precisa existir **literalmente** no documento;
  - o ID da atividade precisa existir;
  - a data precisa ser ISO válida **e estar escrita no documento** (datas inferidas viram incerteza);
  - os responsáveis precisam ser membros conhecidos citados no texto;
  - campos iguais ao valor oficial são descartados, o que evita sugestão vazia e duplicata.
- **Hipóteses** ("talvez", sem dono nem decisão) viram `no_action`. Além do que o modelo diz, a própria Central barra a proposta cujo trecho só fala em possibilidade ("talvez", "poderíamos", "quem sabe") sem nenhuma decisão: hipótese nunca vira sugestão. Se o trecho tem uma decisão e um "talvez" no meio, a sugestão segue com um ponto para conferir.
- **Responsáveis:** quem já é responsável e continua na lista do modelo é mantido, mesmo sem ser citado no documento; se a proposta tira alguém, isso aparece como ponto para conferir.
- **Reconhecimento de ata:** pelo cabeçalho `data_da_reuniao`, pelo nome ou título ("ata", "minuta", ou "reunião" com data, como "Reunião Growth 10-10") ou pelo corpo (quem participou + o que foi decidido). O nome do arquivo não precisa começar com "Ata".
- **Tudo que foi deixado de fora** aparece em "O que a IA leu e deixou de fora": instrução para a IA ignorada, ideia sem decisão, nada novo, descartada na checagem.
- **Novidades dos documentos** (o resumo pessoal, "o que mudou para mim"): montado a partir dos registros, **sem IA**, para que todo fato venha de um registro com link. Separa mudanças confirmadas, sugestões ainda não oficiais (com o selo "incerto" quando há pontos para conferir) e conflitos aguardando decisão. Se nada mudou, a página diz isso.

### Recursos além do obrigatório

O case lista como fora do escopo obrigatório perguntas livres, OCR e envio de mensagens. Os recursos abaixo são opcionais e seguem as regras do case: nada vale sem revisão humana, nada é inventado e nenhuma mensagem sai da Central.

- **"Isso ainda está valendo?"** (`DIAS_SEM_NOVIDADE` no `.env`, padrão 14; 0 desliga): atividade aberta sem nenhuma novidade nesse período (nenhuma mudança, sugestão aceita ou confirmação) ganha o selo "Sem novidade há N dias" e aparece no topo de **Minhas atividades** do responsável com três respostas: **Continua valendo** (registra no histórico e recomeça a contagem, sem mudar nada no quadro), **Atualizar** (abre a edição) e **Já terminou** (marca como concluída). Só os responsáveis ou quem aprova sugestões podem responder. Atividade com sugestão aguardando revisão não entra, porque já tem novidade chegando.
- **Transcrever com IA** (PDF escaneado e imagem): o case pede que esses arquivos apareçam como "não processados", e eles continuam assim. Na página do documento, uma pessoa pode clicar em **Transcrever com IA**: o Gemini recebe o arquivo e devolve só o texto visível, copiado literalmente (sem resumir nem completar; o que não dá para ler vira `[ilegível]`). O resultado fica como **rascunho, ao lado do original**, e não vale nada até uma pessoa conferir, corrigir se precisar e clicar em **A transcrição confere**. Só então o texto entra na Central pelo mesmo caminho de qualquer documento, e a sugestão passa por revisão. Onde o documento aparece, ele leva a marca "transcrito pela IA, conferido por Carla". A transcrição vale só para aquela versão do arquivo.
- **Pergunte à Central** (no "Comece aqui", depois da primeira ação, do propósito e das frentes): resposta curta **com o trecho e o link do documento de origem**. A IA recebe os documentos lidos da pasta, o quadro de atividades (fonte oficial) e as sugestões pendentes (marcadas como não oficiais), sempre como dados entre marcas aleatórias. Cada trecho citado é conferido literalmente no documento: se nenhum confere, a resposta não aparece e a Central diz "não encontrei". Documentos substituídos só entram com aviso; documentos indisponíveis não entram. Com a IA desligada ou fora do ar, cai numa busca simples por palavras. A mesma pergunta com os mesmos documentos é respondida da memória por até 1 hora. As perguntas não ficam gravadas no banco; só o uso de tokens (`llm_calls`, propósito `pergunta`).
- **Busca e filtros:** em Minhas atividades, Todas as atividades (responsável, frente, situação, prazo) e Documentos, onde a busca também procura **dentro do conteúdo** e mostra o trecho encontrado.

**Retirado de propósito:** avisos e um bot no Discord chegaram a ser construídos (ramo `extra-discord` do repositório). Saíram da entrega porque a especificação põe "notificações a pessoas, envio de mensagens" fora do escopo (§7) e diz que a IA não envia mensagens (§1). As mensagens levariam trechos de atas a pessoas que talvez não tenham acesso ao arquivo no Drive. Para voltar, seria preciso respeitar as permissões de cada arquivo (seção 10).

## 8. Custo estimado por uso

A IA é chamada em três situações: quando uma **ata nova ou editada** chega (ou alguém pede nova análise), quando alguém faz uma pergunta em **Pergunte à Central** e quando alguém pede **Transcrever com IA**. Planilhas, índice e demais documentos não passam pelo modelo. Cada chamada fica registrada na tabela `llm_calls` e os totais aparecem em **Estado da sincronização**.

**Medição real (28/09/2026, 8 análises de atas com `gemini-3.6-flash`):** 10.544 tokens de entrada e 1.688 de saída, ou seja, **~1.300 de entrada e ~210 de saída por ata**. A entrada inclui as regras, a lista de membros, as atividades atuais e o texto da ata.

| Cenário | Custo por ata | 20 atas/mês |
|---|---|---|
| Camada gratuita do Gemini (usada no protótipo) | US$ 0 | US$ 0 |
| `gemini-3.6-flash` pago (US$ 0,75 / 1M entrada, US$ 3,75 / 1M saída, até 31/12/2026) | ~US$ 0,0018 | ~US$ 0,04 |
| O mesmo modelo com o preço de 2027 (o dobro) | ~US$ 0,0036 | ~US$ 0,07 |
| `gemini-3.1-flash-lite` pago (US$ 0,25 / US$ 1,50) | ~US$ 0,0006 | ~US$ 0,01 |

**Perguntas e transcrições (estimativa, não medida):**

| Uso | Tokens por uso | Custo por uso | Mês típico |
|---|---|---|---|
| Pergunta (`gemini-3.1-flash-lite`) | ~2.400 de entrada (regras + todos os documentos da pasta de teste) e ~150 de saída | ~US$ 0,0008 | 100 perguntas: ~US$ 0,08 |
| Transcrição de uma página ou foto (`gemini-3.6-flash`) | ~500 de entrada (imagem + regras) e ~200 de saída | ~US$ 0,001 | 10 páginas: ~US$ 0,01 |

A entrada da pergunta cresce com o tamanho da pasta, porque todos os documentos lidos vão no pedido. Com dezenas de documentos longos, o próximo passo seria mandar só os trechos relevantes (busca antes da IA).

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
- PDF escaneado e imagem não são lidos automaticamente (OCR fora do escopo do case); a transcrição depende de uma pessoa conferir.
- "Pergunte à Central" manda ao modelo todos os documentos lidos da pasta a cada pergunta nova.
- No modo pasta local (testes), o identificador do arquivo é o número do arquivo no disco: se um arquivo for apagado e outro criado em seguida, o sistema pode confundir os dois. No Drive o ID é estável.

## 10. Antes de usar dados reais

- Autenticação real (Google Sign-In) e autorização por pessoa, respeitando as permissões de cada arquivo no Drive. Uma pessoa não pode ver trechos de um arquivo ao qual não tem acesso, nem por meio de resumos da IA.
- Tokens em um gerenciador de segredos ou criptografados. HTTPS obrigatório.
- Verificação do app OAuth pelo Google (`drive.readonly` é escopo restrito).
- Política de retenção: apagar automaticamente o texto guardado quando uma fonte for removida ou o acesso for revogado (hoje ele fica marcado como indisponível; ver a seção 12).
- "Pergunte à Central" e o resumo pessoal teriam de usar só os documentos que aquela pessoa pode abrir no Drive.
- Revisar os termos do provedor de IA (uso de dados para treino) e preferir um plano pago, sem retenção.

## 11. Ferramentas de IA usadas

- **No desenvolvimento:** Claude (Anthropic), como par de programação para arquitetura, código, testes e revisão de requisitos. Todas as decisões foram revisadas e testadas.
- **No produto:** Gemini API, em três lugares, sempre com validação e revisão humana:
  1. leitura assistida de atas (sugestões de criar ou alterar atividades);
  2. Pergunte à Central (resposta com trecho conferido no documento);
  3. Transcrever com IA (texto de PDF escaneado ou imagem, que só vale depois de conferido).

  Não há agentes, embeddings nem MCP: não eram necessários para os critérios do case.

## 12. Cache: o que fica guardado e como apagar

- **Onde:** tudo fica em `data/central.db` (SQLite, fora do Git): metadados de cada arquivo, o texto extraído de cada versão lida, atividades, sugestões, histórico, pendências e o token OAuth.
- **Apagar tudo:** pare o site e apague `data/central.db`. Na próxima execução, a Central lê a pasta de novo do zero (o Drive precisa ser conectado de novo).
- **Revogar o acesso ao Drive:** em **Estado da sincronização**, **Desconectar e revogar acesso** apaga o token e revoga a autorização no Google.
- **Arquivo removido do Drive ou sem acesso:** a fonte fica **indisponível**, abre uma pendência e suas sugestões pendentes ficam desatualizadas. O texto antigo não entra mais nas respostas do "Pergunte à Central" e, onde aparece, vem marcado como possivelmente desatualizado, nunca como confirmado.
- **Respostas guardadas do "Pergunte à Central":** ficam só na memória, por até 1 hora, e deixam de valer assim que qualquer documento ou o quadro muda. Reiniciar o site apaga todas.
