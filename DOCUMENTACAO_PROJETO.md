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
