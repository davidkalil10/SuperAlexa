# Super Alexa

Skill customizada da Alexa que conversa com **Google Gemini Web (`HanaokaYuzu/Gemini-API`)** ou **Groq** por voz, com **pesquisa na web integrada**, **alternância de IA por comando de voz** e **memória persistente** (DynamoDB).

```
Voz -> Alexa (ASR) -> Lambda -> Gemini Web (Google Search nativo) OU Groq (+ DuckDuckGo) -> Alexa (TTS) -> Voz
```

## Como usar
- Abrir: **"Alexa, abrir modo super"** (no momento em que a skill abre, o cliente do Gemini já é aquecido automaticamente em segundo plano).
- Conversar: fale naturalmente ou comece com frases como *"me explique..."*, *"quero saber..."*, *"o que é..."*, *"sobre..."*, *"é um..."*, *"e..."*, ou palavras curtas como *"Ronin"*.
- **Alternar entre Gemini e Groq por voz:**
  - **"usar gemini"** / **"mudar para gemini"** (usa o Google Gemini com pesquisa Google nativa; funciona direto em modo anônimo sem precisar de chave ou cookies!).
  - **"usar groq"** / **"mudar para groq"** (usa a cascata ultra-rápida do Groq + pesquisa DuckDuckGo).
  - **"qual inteligência artificial"** / **"qual modelo"** (informa qual cérebro está ativo no momento).
  - *Nota:* Se o Gemini estiver ativo e demorar mais de ~5,8s (ou se ainda estiver aquecendo), a skill faz **fallback automático** para o Groq na mesma chamada para nunca estourar o limite de 8s da Alexa!
- Limpar memória: **"nova conversa"**, **"limpar memória"**, **"apagar histórico"**, **"esquecer tudo"**, **"zerar memória"**.
- Sair: **"parar"**.

## Estrutura
```
lambda/lambda_function.py     # handlers + cliente Groq + memória
lambda/requirements.txt
lambda/secrets.example.json   # copie para secrets.json (ignorado no git)
skill-package/skill.json
skill-package/interactionModels/custom/pt-BR.json
```

## Configuração (Alexa-hosted Python, gratuito)
1. [Alexa Developer Console](https://developer.amazon.com/alexa/console/ask) → **Create Skill** → idioma **Portuguese (BR)**, modelo **Custom**, hosting **Alexa-hosted (Python)**.
2. Importe este repositório (**Import skill** com a URL do Git) ou cole os arquivos nas abas Build/Code.
3. Na aba **Code**, crie `lambda/secrets.json` (não vai para o git) com sua chave:
   ```json
   { "GROQ_API_KEY": "gsk_..." }
   ```
4. **Build Model** → **Deploy** → teste na aba **Test** (modo *Development*) ou no seu Echo (mesma conta).

> O Alexa-hosted já provisiona o DynamoDB e define `DYNAMODB_PERSISTENCE_TABLE_NAME`/`DYNAMODB_PERSISTENCE_REGION`. Fora dele, defina essas variáveis e `GROQ_API_KEY` no Lambda.

## Ajustes (variáveis de ambiente opcionais)
| Variável | Padrão |
|---|---|
| `AI_BASE_URL` | `https://api.groq.com/openai/v1` (troque para qualquer API OpenAI-compatível) |
| `AI_MODELS` | `openai/gpt-oss-120b,qwen/qwen3.8-27b,openai/gpt-oss-20b,allam-2-7b` (cascata de fallback) |
| `MAX_STORED_BYTES` | `307200` (300 KB: guarda tudo no DynamoDB; só descarta as mais antigas ao passar do teto) |
| `CONTEXT_CHAR_BUDGET` | `12000` (só as mensagens mais recentes que cabem nisso vão para a IA) |
| `AI_MAX_TOKENS` | `500` |
| `AI_TOTAL_BUDGET` | `7.0` s (a Alexa corta em ~8s) |

## Limitações
Só conversa: não controla casa inteligente, Spotify, alarmes ou timers.
