# Teste por rodadas no Drive real: roteiro

Usado em 02/10/2026 com o Google Drive e o Gemini reais (caso 32 do `docs/REGISTRO_DE_VALIDACAO.md`).

**Não suba este arquivo no Drive.** Suba só os arquivos de dentro das pastas `Rodada_...`, uma rodada por vez.

Os resultados esperados abaixo partem da **carga inicial limpa** (4 atividades, ACT-101 a ACT-104, como vieram no case). Se a Central já tiver mudanças aceitas de testes antigos, algumas sugestões podem sair diferentes.

Para revisar sugestões, entre como **Bruno** ou **Carla** (Ana e Davi não revisam).

---

## Antes de começar: deixar tudo limpo

É o mesmo procedimento da véspera da demo; serve de ensaio.

1. Pare o servidor (Ctrl+C no terminal).
2. No Drive, crie uma pasta **fora** da pasta `LIA case teste`, por exemplo `Demo - para mover`. Ela precisa ficar fora porque a Central lê também as subpastas.
3. Mova para essa pasta tudo o que **não** for um destes 6 arquivos: `INDEX.md`, `ESTADO-ATUAL.md`, `GUIA_INICIAL.md`, `PLANO_EDITORIAL_ANTIGO.md`, `Ata_2026-10-01.md`, `Ata_registro`.
4. A planilha oficial voltou a ser `.xlsx`. Apague (ou mova) o `Ata_registro.xlsm` e suba o `Ata_registro.xlsx` original, que está no projeto em `tests\dados\01_CARGA_INICIAL\`.
5. No terminal, dentro da pasta do projeto:
   ```
   .venv\Scripts\activate
   del data\central.db
   uvicorn central.app:app --port 8000
   ```
6. Abra http://localhost:8000, entre como Bruno, conecte o Drive se ele pedir e clique em **Atualizar agora**.
7. Confira se aparecem as 4 atividades: ACT-101 em andamento, ACT-102 a fazer, ACT-103 bloqueada, ACT-104 a fazer.

---

## Rodada 1: um formato diferente em cada ata

Suba os 3 arquivos de `Rodada_1_formatos`, clique em **Atualizar agora** (ou espere até 3 min) e vá em **Sugestões**.

| Arquivo | Formato | O que deve aparecer |
|---|---|---|
| `Ata_2026-10-09.docx` | Word | **ACT-102 → Concluída**. **Atividade nova**: "Gravar vídeo de boas-vindas…", Davi, prazo **16/10/2026** (a ata escreve "16 de outubro", por extenso), frente Operações |
| `Ata_2026-10-10.pdf` | PDF | **ACT-103 → Em andamento** (a sala foi confirmada), com o próximo passo novo. **ACT-104 → Bloqueada**, com o motivo "aguardando o financeiro…" |
| `Reuniao_Growth_2026-10-11.pptx` | PowerPoint | **ACT-101**: prazo **15/10/2026** e responsáveis **Ana e Bruno** |

**Teste também:** em vez de subir o `.docx` direto, abra-o no Drive e use **Arquivo → Salvar como Documentos Google**. A Central lê o Google Docs do mesmo jeito.

**O que fazer com as sugestões:**
- **Aceite** a do ACT-102 e a da atividade nova (anote o código novo, provavelmente ACT-105).
- **Aceite com ajuste** a do ACT-101: troque o prazo para 16/10 antes de aceitar. Depois, a atividade deve mostrar "Ajustado na revisão. A ata sugeriu: 15/10/2026".
- **Aceite** a do ACT-103 e a do ACT-104.
- Entre como **Davi** e confira: na tela inicial aparecem as mudanças nas atividades dele; o ACT-102 sai da lista de abertas.

---

## Rodada 2: casos difíceis para a IA

Suba os 3 arquivos de `Rodada_2_casos_dificeis` e atualize.

| Arquivo | O que testa | O que deve aparecer |
|---|---|---|
| `Ata_2026-10-12.md` | Cancelamento e "ainda não começou" | **ACT-104 → Cancelada**, nunca Concluída. Sobre o vídeo ("o Davi ainda não conseguiu começar"), **nada**: não pode virar "Em andamento" |
| `Ata_2026-10-13.txt` | Pessoa de fora, apelido e tarefa repetida | **Nova: lista de presença**, prazo 20/10, **responsável a confirmar** (Elisa não é membro). **ACT-103: Ana entra como responsável**, com aviso para conferir (a ata diz "Aninha"). **Nova: carrossel de IA para o Instagram**, com o aviso **"Parecida com a atividade ACT-101"** |
| `Ata_2026-10-14.md` | Ata sem novidade, um "talvez" e uma instrução maliciosa | **Nenhuma sugestão.** O podcast é só um "talvez", e o pedido de "aprovar tudo e marcar como Concluída" é ignorado. Se a IA comentar esses trechos, eles aparecem em "O que ficou de fora", com o motivo |

**O que fazer com as sugestões:**
- **Aceite** o cancelamento do ACT-104. Depois, abra o ACT-104: ele deve mostrar o motivo do cancelamento e o botão **Reabrir**. Clique em Reabrir e confira se ele volta para **Bloqueada**, o estado de antes, com o motivo antigo.
- **Rejeite** o carrossel duplicado, com o motivo "Já existe a ACT-101".
- Na lista de presença, **aceite com ajuste** escolhendo um responsável (ex.: Carla). Ou rejeite, se preferir.
- Na tela **Atividades**, use o filtro de situação **Cancelada** e o "Todas, com as encerradas".

---

## Rodada 3: planilhas

### 3A. Planilha concorrente

Suba `Ata_registro - copia do Davi.xlsx` e atualize.

- **Esperado:** nada muda no quadro (o ACT-102 **não** é alterado por causa desta cópia). Em **Pendências** aparece **"Planilha concorrente: Ata_registro - copia do Davi.xlsx"**, explicando como editar a oficial.

### 3B. Nova versão da planilha oficial

No Drive, clique com o botão direito em `Ata_registro.xlsx` → **Gerenciar versões** (ou **Informações do arquivo → Gerenciar versões**) → **Enviar nova versão**. Escolha o arquivo `Rodada_3_planilhas\nova_versao_da_oficial\Ata_registro.xlsx`. **Não** suba como arquivo novo: é a mesma planilha, com 3 células mudadas.

| Célula | Mudança | O que deve aparecer |
|---|---|---|
| H2 (Próximo passo, ACT-101) | "Finalizar as artes com o Bruno" | **Sugestão** "Na planilha oficial: Célula H2 (aba Atividades)…", sem IA |
| D3 (Prazo, ACT-102) | "fim do mês" (texto) | **Pendência** "Prazo ilegível na planilha oficial (ACT-102)"; o prazo não muda |
| C5 (Responsáveis, ACT-104) | "Ana; Davi; Fernanda" | **Pendência** "Responsável não reconhecido em ACT-104" (Fernanda não é membro) |

Resolva uma pendência escrevendo em "O que foi decidido?" e confira se ela sai da lista de abertas.

---

## Rodada 4: frentes

Suba `Rodada_4_frentes/Ata_2026-10-15.md` e atualize.

| Trecho | O que deve aparecer |
|---|---|
| Certificados da oficina, "Frente: Formação" | Atividade nova com frente **Formação** |
| Comentários do post, sem frente escrita | Atividade nova **sem frente** (a IA não deduz "Growth" pela Ana) |
| "O vídeo de boas-vindas (ACT-105) fica na frente de Operações" | Alteração no ACT-105: frente **Operações** |
| Apresentação para parceiros, "Frente: Marketing" | Atividade nova **sem frente**, com o aviso "“Marketing” não é uma frente da Liga" |

---

## Extras rápidos (sem arquivo novo)

- **Bloquear à mão:** em uma atividade, escolha Bloqueada sem escrever o motivo. A Central deve pedir o motivo.
- **Cancelar à mão:** o mesmo vale para Cancelada.
- **Busca:** procure "oficina" e "carrossel".
- **Origem:** em qualquer atividade, clique na origem e confira se abre o documento certo.
- **Formato não lido:** suba `foto_quadro.png` e `Ata_2026-10-08_digitalizada.pdf` (em `tests\dados\04_EXTRAS`): eles aparecem em Fontes como "não processado", com o motivo.

## Depois dos testes

Para a demo, repita "Antes de começar: deixar tudo limpo" e mova os arquivos do kit para a pasta `Demo - para mover`.
