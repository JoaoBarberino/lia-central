# Diário de bordo — Case técnico Liga IA UFSCar

> Rascunho mantido durante o desenvolvimento. Revise com suas palavras antes de entregar.

## Dom 27/09 — Leitura e decisões iniciais

**O que fiz**
- Li a pasta 00 (comece aqui), o enunciado, a especificação técnica, o guia da Drive API e os dados de teste.
- Mapeei os cenários que a banca vai testar: carga inicial, arquivo novo, edição, mudança de prazo (ACT-101), tarefa nova, ideia vaga e planilha homônima vazia.

**Decisões**
- **Fonte oficial:** importar a planilha apontada pelo `INDEX.md` uma única vez para um banco próprio. Depois disso, o banco é oficial e tudo que chega do Drive vira sugestão ou pendência. Motivo: uma regra só resolve ata nova, planilha editada e planilha vazia.
- **Stack:** Python (é a linguagem que mais domino), FastAPI, SQLite e páginas renderizadas no servidor. Evitei um frontend separado para ter menos peças para explicar.
- **Sincronização:** varredura periódica da pasta (3 min) em vez de `changes.list`. Para uma pasta pequena, é mais simples e já reconcilia o estado inteiro.

**Observações dos dados**
- A ACT-104 tem dois responsáveis (`Ana; Davi`), então o banco precisa de uma relação N:N.
- Os documentos têm cabeçalho (`status: ativo | parcial | deprecated`). Uso esses campos na regra de autoridade.
- A planilha vazia tem outra aba (`Ata`) e outro ID, então não deve ser tratada como o registro.

## Seg 28/09 — Núcleo e interface

**O que fiz**
- Criei a chave do Gemini e o projeto no Google Cloud com a Drive API ativada. Descobri que não precisa de cartão: é só não ativar o teste gratuito. _(completar: OAuth em modo Testing, escopo `drive.readonly`, pasta de teste)_
- Implementei a sincronização, os extratores, o registro oficial vinculado por `file_id`, as sugestões com revisão e a validação da saída da IA.
- Criei testes automatizados para os cenários da seção 9 da especificação, rodando sobre uma pasta local que simula o Drive.
- Montei a interface: Minhas atividades, Todas, Sugestões, Novidades, Pendências, Fontes, Comece aqui e Estado da sincronização.

**Decisões**
- **Vínculo do registro por ID do Drive, não pelo nome.** Renomear mantém a autoridade; um arquivo com o mesmo nome e outro ID não herda nada.
- **Edição da planilha oficial é comparada com a versão anterior da planilha, não com o banco.** Se eu comparasse com o banco, toda edição na planilha tentaria desfazer as decisões aprovadas na aplicação (ex.: a planilha ainda diz 05/10 para a ACT-101 depois de aprovada a mudança para 07/10).
- **Falha nunca é "vazio":** se a listagem falha, nada é marcado como removido; se um arquivo falha, a última versão boa continua valendo.
- **Validação da IA em código:** a evidência precisa estar no texto, a data precisa estar escrita, o ID precisa existir e os responsáveis precisam ser membros citados.

**Problemas encontrados**
- Um teste falhou de forma intermitente: duas atas processadas na mesma rodada saíam em ordem diferente a cada execução. Corrigi ordenando por papel e depois por nome, para o processamento ser determinístico.
- Ao rodar no Windows, o app nem iniciou: `ZoneInfoNotFoundError: America/Sao_Paulo`. O Windows não traz a base de fusos horários que o Python usa (no Linux ela vem do sistema, por isso os testes passavam no ambiente de desenvolvimento). Resolvi adicionando o pacote `tzdata` ao `requirements.txt`. Lição: testar a instalação do zero em outro sistema operacional, como a banca vai fazer.
- Primeiro teste real com o Drive: conexão OK, 4 atividades importadas e visão da Ana correta. Mas a ata de 01/10 não foi analisada: o Gemini (`gemini-3.8-flash`) respondeu **HTTP 503, "high demand"** nas 3 tentativas. O sistema se comportou como planejado (o documento foi lido, nenhuma sugestão foi inventada e a análise fica pendente para a próxima rodada), mas a mensagem na tela era o JSON cru do erro. Mudanças: (1) um **modelo reserva** (`gemini-3.5-flash-lite`) usado só quando o principal está sobrecarregado ou sem cota; erro de configuração, como chave inválida, não troca de modelo; (2) uma mensagem legível: "Documento lido, mas a IA não respondeu... nova tentativa na próxima sincronização".
- Os timestamps em segundos faziam um evento "empatar" com o marco do resumo pessoal. Passei a usar milissegundos.

## Ter 29/09 — _(a preencher)_

## Uma decisão que mudei depois de ver o modelo errar
_(a preencher com um caso real observado nos testes com o Gemini)_
