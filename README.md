# Super Alexa

Skill customizada da Alexa que conversa com uma IA (**Groq**, API compatível com OpenAI) por voz, com **memória persistente** (DynamoDB) e comando para **limpar a memória**.

```
Voz -> Alexa (ASR) -> Lambda -> Groq -> texto -> Alexa (TTS) -> voz
```

## Como usar
- Abrir: **"Alexa, ativar modo super"** (invocação: `modo super`; "Alexa" não é permitido no nome de invocação).
- Perguntar: comece a frase com um verbo/pergunta: *"me explique..."*, *"me diga..."*, *"quero saber..."*, *"o que é..."*, *"como..."*, *"quem é..."*. A Alexa não aceita falas 100% livres, por isso o modelo usa essas frases-gatilho (ver `skill-package/interactionModels/custom/pt-BR.json`; adicione mais à vontade).
- A skill mantém o microfone aberto após cada resposta; fica em silêncio ~8s e a sessão fecha, mas o histórico continua salvo.
- Limpar memória: **"nova conversa"**, **"limpar memória"**, **"apagar histórico"**, **"esquecer tudo"**, **"zerar memória"**. Também funciona se dito dentro de uma pergunta (interceptação de texto).
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
