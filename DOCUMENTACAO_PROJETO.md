# Super Alexa - Documentação de Arquitetura, Aprendizados e Decisões

Este documento registra formalmente a arquitetura da skill **Super Alexa**, as decisões técnicas acordadas, o histórico de aprendizados e as regras mandatórias que devem ser seguidas por qualquer desenvolvedor ou assistente de IA.

---

## 1. Princípios e Regras Invioláveis do Projeto

1. **Permanência na Skill Atual:** 
   * **NUNCA** sugerir ou tentar migrar para uma nova skill ou outro hosting. Trabalhamos exclusivamente na skill existente no Alexa Developer Console (Alexa-Hosted).
2. **Fallback Silencioso e Resiliente:**
   * A Alexa **JAMAIS** deve falar termos técnicos ou jargões de erro (como `TypeError`, `ValueError`, `ImportError`, `AuthError`, etc.) no ouvido do usuário.
   * Se o Gemini demorar além do orçamento ou encontrar qualquer instabilidade, o sistema captura a exceção em log (`logger.warning`) e entrega a resposta imediatamente através do **Groq + DuckDuckGo**. O usuário sempre recebe uma resposta rápida, útil e em português.
3. **Código Limpo, sem Monkey-Patches Invasivos:**
   * **NUNCA** injetar monkey-patches no carregador de módulos do Python (`SourceFileLoader`), reescrever AST em tempo de execução ou injetar bytecode.
   * O código deve permanecer simples, legível, auditável e compatível com compilação direta via `py_compile`.
4. **Isolamento e Segurança de Segredos:**
   * O arquivo `lambda/secrets.json` nunca é enviado ao Git (protegido por `.gitignore`).
   * No console da Alexa, o arquivo `secrets.json` é criado/editado diretamente na pasta `lambda`.
5. **Registro Contínuo:**
   * Toda mudança estrutural ou novo aprendizado deve ser registrado neste documento.

---

## 2. Visão Geral da Arquitetura

```
               [Usuário na Alexa]
                       │
                 (Fala / Áudio)
                       ▼
       [AWS Lambda - Alexa-Hosted]
                       │
       ┌───────────────┴───────────────┐
       ▼                               ▼
 [Google Gemini Web]             [Groq API]
 (HanaokaYuzu/Gemini-API)       (OpenAI-compatível)
 • Autenticado via cookies      • Fallback ultra-rápido (~1s)
 • 3.8 Flash, Pro, Lite, Argon  • GPT-OSS 120B / Qwen
 • Histórico salvo na conta     • Busca Web DuckDuckGo Lite
       │                               │
       └───────────────┬───────────────┘
                       ▼
        [Persistência no DynamoDB]
        • Histórico de até 400 KB
        • Estado: provedor ativo e preferência de histórico
```

### Componentes Principais

* **`lambda/lambda_function.py`**:
  * Orquestrador de requisições da Alexa (`LaunchRequest`, `ChatIntent`, `CatchAllIntent`, etc.).
  * Mecanismo de **Progressive Response** (avisa o usuário que está pesquisando caso passe de 2.2s).
  * Mecanismo de **Refresh/Continuação** automática de perguntas longas.
  * Integração híbrida Gemini + Groq com alternância por comandos de voz.
  * Busca em tempo real no DuckDuckGo Lite.
  * Persistência de contexto no DynamoDB.
* **`lambda/secrets.json`**:
  * Contém `GROQ_API_KEY`, `GEMINI_1PSID`, `GEMINI_1PSIDTS`, `GEMINI_SAVE_HISTORY` e `GEMINI_THINKING`.
* **`skill-package/interactionModels/custom/pt-BR.json`**:
  * Modelo de interação da Alexa com as frases de ativação (`modo super`), troca de provedor (`usar gemini`, `usar groq`), alternância de histórico (`salvar histórico no gemini`, `modo temporário`) e perguntas de status (`qual modelo você está usando`).

---

## 3. Comandos de Voz Reconhecidos

| Ação | Exemplos de Frases |
|---|---|
| **Ativação** | *"Alexa, modo super"*, *"Alexa, abrir modo super"* |
| **Trocar para Gemini** | *"Usar Gemini"*, *"Mudar para o Gemini"*, *"Modo Gemini"* |
| **Trocar para Groq** | *"Usar Groq"*, *"Mudar para o Groq"*, *"Modo Groq"* |
| **Ativar histórico no Gemini** | *"Salvar histórico no Gemini"*, *"Gravar conversas no Gemini"* |
| **Modo Temporário no Gemini** | *"Usar Gemini anônimo"*, *"Gemini sem histórico"*, *"Modo temporário"* |
| **Verificar Status / IA Ativa** | *"Qual modelo você está usando?"*, *"Que IA você é?"*, *"Qual o cérebro ativo?"* |
| **Limpar Memória** | *"Nova conversa"*, *"Esquecer tudo"*, *"Começar de novo"* |
| **Encerrar** | *"Parar"*, *"Tchau"*, *"Encerrar"* |

---

## 4. Como Funciona a Integração com o Google Gemini

1. **Autenticação:**
   * Utiliza os cookies `__Secure-1PSID` e `__Secure-1PSIDTS` da conta Google do usuário.
   * Quando autenticado com sucesso, o status da conta é `1000 (AVAILABLE)`.
2. **Salvamento de Histórico na Conta:**
   * O cliente inicia um `ChatSession` vinculado ao `GeminiClient`.
   * Quando `temporary=False` (padrão quando `GEMINI_SAVE_HISTORY=true`), o Google Gemini gera um `cid` (Chat ID) e salva o histórico diretamente na conta do usuário no [gemini.google.com](https://gemini.google.com).
   * **Importante sobre a interface Web do Google:** A barra lateral do site do Gemini no navegador não atualiza via WebSocket automaticamente para mensagens vindas de fora; o usuário deve **pressionar F5 (recarregar página)** para que o chat apareça na lista.
3. **Identificação por Voz:**
   * O status command informa se está conectado na conta Google, quais modelos estão disponíveis (ex: `3.8 Flash`), o nível de raciocínio e se o histórico está sendo salvo ou se está em modo temporário.
   * Informa com total transparência o motor real que gerou a última resposta (Gemini ou Groq).

---

## 5. Log de Decisões e Aprendizados

### [30/10/2026 ~17:28] - Versão Estável Comprovada (Commit `ac46b83` / `556f606`)
* **Situação:** Sistema funcionando com resposta rápida, alternância fluida entre Groq e Gemini, identificação detalhada por voz e persistência no DynamoDB.
* **Decisão:** Manter a arquitetura com 1305 linhas, sem manipulação de AST ou compiladores dinâmicos.
* **Resultado:** Usuário confirmou funcionamento excelente e resposta de qualidade.

### [30/10/2026 ~21:35] - Descarte de Monkey-Patches e Reversão para Versão Estável
* **O que falhou:** Tentativas de sobrescrever métodos internos do `gemini_webapi` (`_FastGeminiClient`), importar classes que não existiam no runtime local, criar fallbacks com `GeminiClient()` vazio (que causa `ValueError: secure_1psid must be provided`), e expor mensagens técnicas na fala da Alexa.
* **Aprendizado Crítico:**
  1. `GeminiClient()` **nunca** deve ser chamado sem cookies caso a dependência `browser-cookie3` não exista. Se não houver cookies, o sistema deve apenas registrar o log e delegar para o Groq.
  2. A fala da Alexa deve ser sempre amigável e natural. Erros internos devem ir para os logs do CloudWatch (`logger.warning`), nunca para o SSML falado.
  3. A estabilidade do fluxo de voz é a prioridade número 1.
* **Ação Executada:** Código revertido integralmente para o estado estável de `ac46b83`, restaurando a fluidez da skill.

### [30/10/2026 ~22:25] - Diagnóstico de Gravação no Histórico do Gemini e Correção do Cold Start
* **Diagnóstico da Causa Raiz:**
  * O teste local provou que os cookies salvos em `secrets.json` funcionam 100% e criam conversas reais no `gemini.google.com` (ex: IDs `c_ef647d5facb9d94e` e `c_92c7d8ef07b71a30`).
  * Porém, nas conversas pela Alexa, as perguntas estavam sendo respondidas pelo Groq (`A última resposta foi gerada por Groq (gpt-oss-120b)`), e por isso nada era gravado no histórico do Google.
  * O motivo do fallback prematuro para o Groq:
    1. O `warmup_gemini` em `ask_gemini` estava limitado a apenas `min(4.2s, ...)`. Como o handshake de inicialização do cliente na AWS Lambda leva ~5.8s em cold start, o timeout estourava e disparava imediatamente o fallback silencioso para o Groq.
    2. O `LaunchRequestHandler` não disparava `warmup_gemini_background()` na abertura da skill, desperdiçando os 4 segundos em que a Alexa fala as boas-vindas para já deixar o Gemini aquecido.
    3. No comando de status (`is_status_command`), se o Gemini ainda estivesse finalizando a conexão, ele não aguardava e exibia o rótulo de fallback.
* **Ajustes Implementados:**
  1. **Aquecimento no `LaunchRequest`:** Adicionado `warmup_gemini_background()` ao `LaunchRequestHandler.handle()`. Enquanto a Alexa fala a saudação inicial, o handshake com o Gemini é concluído em segundo plano.
  2. **Orçamento Realista de Warmup:** Ajustado o teto do `warmup_gemini` em `ask_gemini` de `4.2s` para `min(6.8s, max(2.5, timeout - 1.5))`, permitindo que o Gemini conclua sua inicialização sem abortar prematuramente.
  3. **Aguardar no Status:** No comando de status sobre modelo ativo, se o Gemini estiver aquecendo, o sistema aguarda até 5s para reportar o status real de conexão da conta com os modelos disponíveis.
  4. **Proteção Total contra Vazamento de Erros:** Exceções não tratadas e `AIError` genéricos agora devolvem estritamente a mensagem padrão amigável (`MSG_ERROR`), garantindo que nomes de classes técnicas do Python nunca sejam pronunciados pela Alexa.
  5. **Compatibilidade Defensiva de Enums:** Imports de `AccountStatus` e verificação de status (`1000` / `AVAILABLE`) protegidos contra diferenças de versão no Lambda.

### [30/10/2026 ~23:00] - Correção da Validação de Autenticação dos Cookies e Ciclo de Vida do Token
* **Diagnóstico da Causa Raiz:**
  * Testamos os cookies do `secrets.json` em tempo real contra o endpoint do Google e identificamos que o token `GEMINI_1PSIDTS` havia expirado (`Account status: UNAUTHENTICATED - 1016`).
  * No código, a condição `is_available` continha `or getattr(cand, "running", False)`. Como o cliente define `_running = True` mesmo quando a sessão não autentica (1016), o código marcava incorretamente a sessão como autenticada e tentava enviar com os cookies expirados. O Google rejeitava a chamada na geração, forçando fallback para o Groq.
* **Ajustes Implementados:**
  1. Remoção do `getattr(cand, "running", False)` da verificação de disponibilidade: a sessão só é considerada autenticada se o `account_status` for estritamente `1000 (AVAILABLE)`.
  2. Fallback limpo: se os cookies expirarem, o cliente com cookies é devidamente fechado (`cand.close()`) e um cliente anônimo limpo é instanciado. O cliente anônimo limpo responde à pergunta com sucesso sem cair no Groq.
  3. Para gravação no histórico do `gemini.google.com`, basta o usuário colar os cookies atuais extraídos do navegador no `secrets.json`.

### [30/10/2026 ~23:25] - Otimização de Roteamento de Modelo e Eliminação Definitiva do "Inicializando"
* **Diagnóstico da Causa Raiz:**
  1. Com os tokens atualizados pelo usuário, a conta autenticou com status `1000 (AVAILABLE)`.
  2. Ao perguntar sobre o modelo ativo logo após a abertura, o `warmup_gemini` na checagem de status aguardava no máximo `5.0s`. Como o handshake do Google leva ~6.0s, o timeout de 5s estourava e devolvia o texto temporário *"Google Gemini (... inicializando)"*.
  3. Ao fazer a pergunta *"qual a capital do brasil"*, o código enviava um header explícito de modelo (`x-goog-ext-525001261-jspb`) que fazia o Google demorar mais de 10 segundos para responder, estourando o tempo seguro do Lambda e caindo no Groq.
* **Ajustes Implementados:**
  1. **Tempo de Warmup do Status:** Aumentado de 5.0s para 7.0s em `is_status_command`. Agora a inicialização de 6.0s conclui a tempo e responde: *"Google Gemini conectado na sua conta (usando 3.8 Flash...)"*.
  2. **Roteamento Rápido sem Header Pesado:** Quando nenhum modelo customizado for especificado em `GEMINI_MODEL`, o primeiro candidato passa `model=None`, permitindo que o Google use o modelo Flash nativo da conta. O tempo de resposta caiu de 10.0s para **3.6s**, terminando bem antes de qualquer corte da Alexa e gravando o histórico normalmente.
* **Resultado Comprovado em Teste:**
  * `ask_gemini` respondeu com sucesso: *"A capital do Brasil é Brasília, localizada no Distrito Federal..."*
  * Motor: `Google Gemini (3.8 Flash, raciocínio médio, histórico salvo)`
  * Status: `Google Gemini conectado na sua conta (usando 3.8 Flash, raciocínio médio, salvando histórico no seu Gemini). Modelos liberados na sua conta: 3.8 Flash, 3.5 Flash Lite, 3.1 Pro, 4 Argon.`

