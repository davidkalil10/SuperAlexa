# -*- coding: utf-8 -*-
"""Super Alexa - Custom Skill que conversa com uma IA (Groq, API compativel com OpenAI)
e mantem memoria da conversa no DynamoDB (persistent attributes).

Fluxo: Alexa (voz->texto) -> este Lambda -> Groq -> texto -> Alexa (texto->voz).
"""
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from xml.sax.saxutils import escape

import ask_sdk_core.utils as ask_utils
from ask_sdk_core.dispatch_components import (
    AbstractExceptionHandler,
    AbstractRequestHandler,
)
from ask_sdk_core.skill_builder import CustomSkillBuilder
from ask_sdk_dynamodb.adapter import DynamoDbAdapter

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ----------------------------------------------------------------------------
# Configuracao
# ----------------------------------------------------------------------------
BASE_URL = os.environ.get("AI_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
# Mesma cascata de modelos usada no Auto News Watch (sobrescreva com AI_MODELS="a,b,c").
DEFAULT_MODELS = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
    "allam-2-7b",
]
MODELS = [m.strip() for m in os.environ.get("AI_MODELS", "").split(",") if m.strip()] or DEFAULT_MODELS

# Memoria: guardamos TUDO no DynamoDB ate este teto de bytes (item maximo = 400 KB);
# so ao estourar o teto descartamos as mensagens mais antigas.
MAX_STORED_BYTES = int(os.environ.get("MAX_STORED_BYTES", str(300 * 1024)))
# Para a IA enviamos apenas as mensagens mais recentes que cabem neste orcamento de caracteres
# (evita estourar contexto/cota e mantem a resposta rapida).
CONTEXT_CHAR_BUDGET = int(os.environ.get("CONTEXT_CHAR_BUDGET", "12000"))
MAX_TOKENS = int(os.environ.get("AI_MAX_TOKENS", "500"))
# A Alexa derruba a resposta apos ~8s; deixamos margem.
TOTAL_BUDGET_SECONDS = float(os.environ.get("AI_TOTAL_BUDGET", "7.0"))
MAX_SPEECH_CHARS = 1500

SYSTEM_PROMPT = (
    "Voce e a Super Alexa, uma assistente de voz inteligente, simpatica e direta. "
    "Responda sempre em portugues do Brasil, de forma curta e natural para ser falada em voz alta "
    "(no maximo 3 ou 4 frases, a menos que o usuario peca mais detalhes). "
    "NAO use markdown, listas com marcadores, asteriscos, emojis, tabelas ou links. "
    "Se precisar enumerar, use frases corridas. Use o historico da conversa para manter o contexto. "
    "Se a fala do usuario parecer curta ou incompleta (uma palavra, um nome, 'e ele?'), trate como "
    "continuacao do assunto anterior. "
    "NUNCA invente fatos. Se nao conhecer uma pessoa, empresa, jogo ou assunto especifico, ou nao tiver "
    "certeza, diga com honestidade que nao tem informacao confiavel sobre isso e peca mais detalhes. "
    "Voce nao tem acesso a internet nem a fontes em tempo real; se perguntarem de onde veio uma informacao, "
    "explique que vem do seu treinamento e pode estar desatualizada ou incorreta. "
    "Voce NAO controla dispositivos, musicas, alarmes ou casa inteligente; se pedirem isso, "
    "explique gentilmente que nesta skill voce so conversa."
)

RESET_PHRASES = (
    "nova conversa",
    "apagar historico",
    "apagar memoria",
    "limpar historico",
    "limpar memoria",
    "zerar historico",
    "zerar memoria",
    "esquecer tudo",
    "esqueca tudo",
    "comecar de novo",
)

MSG_WELCOME = "Modo super ativado! Pode falar, sobre o que vamos conversar?"
STOP_WORDS = {"parar", "para", "pare", "sair", "tchau", "cancelar", "cancela", "encerrar", "ate logo", "chega", "fechar"}
HELP_WORDS = {"ajuda", "me ajuda", "socorro"}
MSG_REPROMPT = "Pode continuar, estou ouvindo."
MSG_HELP = (
    "Eu sou uma assistente com inteligencia artificial. Faca qualquer pergunta, por exemplo: "
    "me explique como funciona um buraco negro. Para apagar minha memoria, diga: nova conversa. "
    "Para sair, diga: parar."
)
MSG_RESET = "Pronto, apaguei a memoria. Vamos comecar uma nova conversa!"
MSG_BYE = "Ate logo!"
MSG_ERROR = "Desculpe, tive um problema para pensar na resposta. Pode repetir?"
MSG_TIMEOUT = "Desculpe, a resposta demorou demais. Pode perguntar de novo?"
MSG_NO_KEY = "A chave da inteligencia artificial nao esta configurada. Verifique a configuracao da skill."


def _load_api_key():
    key = os.environ.get("GROQ_API_KEY") or os.environ.get("AI_API_KEY")
    if key:
        return key.strip()
    # Alexa-hosted nao permite variaveis de ambiente: use lambda/secrets.json (fora do git).
    path = os.path.join(os.path.dirname(__file__), "secrets.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return (data.get("GROQ_API_KEY") or data.get("AI_API_KEY") or "").strip() or None
    except (OSError, ValueError):
        return None


# ----------------------------------------------------------------------------
# Utilidades de texto
# ----------------------------------------------------------------------------
def _normalize(text):
    text = (text or "").lower().strip()
    text = (
        text.replace("á", "a").replace("à", "a").replace("â", "a").replace("ã", "a")
        .replace("é", "e").replace("ê", "e").replace("í", "i")
        .replace("ó", "o").replace("ô", "o").replace("õ", "o")
        .replace("ú", "u").replace("ç", "c")
    )
    return re.sub(r"[^a-z0-9 ]", "", text).strip()


def is_reset_command(text):
    norm = _normalize(text)
    return any(norm == p or norm.endswith(p) for p in RESET_PHRASES)


def clean_for_speech(text):
    """Remove markdown/emojis e escapa para SSML."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_#`>~|]+", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    text = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_SPEECH_CHARS:
        cut = text[:MAX_SPEECH_CHARS]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        text = cut[: end + 1] if end > 200 else cut
    return escape(text)


# ----------------------------------------------------------------------------
# Cliente da IA (OpenAI-compatible)
# ----------------------------------------------------------------------------
class AIError(Exception):
    pass


def _post_chat(model, messages, api_key, timeout):
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": MAX_TOKENS,
        "temperature": 0.7,
    }
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "SuperAlexa/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = (data["choices"][0]["message"].get("content") or "").strip()
    if not content:
        raise AIError("resposta vazia")
    return content


def ask_ai(history, user_text):
    """Tenta cada modelo da cascata dentro do orcamento de tempo da Alexa."""
    api_key = _load_api_key()
    if not api_key:
        raise AIError("NO_KEY")

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [
        {"role": "user", "content": user_text}
    ]
    deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
    last_err = None
    timed_out = False
    for model in MODELS:
        remaining = deadline - time.monotonic()
        if remaining < 1.0:
            break
        try:
            return _post_chat(model, messages, api_key, timeout=min(remaining, 6.0))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            timed_out = timed_out or "timed out" in str(e).lower()
            last_err = e
            logger.warning("Modelo %s falhou: %s", model, e)
        except (AIError, KeyError, IndexError, ValueError) as e:
            last_err = e
            logger.warning("Modelo %s resposta invalida: %s", model, e)
    raise AIError("TIMEOUT" if timed_out else f"FAILED: {last_err}")


# ----------------------------------------------------------------------------
# Memoria (DynamoDB)
# ----------------------------------------------------------------------------
def get_history(handler_input):
    attrs = handler_input.attributes_manager.persistent_attributes
    return list(attrs.get("history", []))


def _size(messages):
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))


def trim_for_storage(history):
    """Descarta as mensagens mais antigas somente se passar do teto de bytes."""
    if _size(history) <= MAX_STORED_BYTES:
        return history
    total, keep_from = 2, len(history)
    for i in range(len(history) - 1, -1, -1):
        total += _size(history[i]) + 2
        if total > MAX_STORED_BYTES:
            break
        keep_from = i
    history = history[keep_from:]
    while history and history[0].get("role") != "user":  # nao comeca no meio de uma troca
        history = history[1:]
    return history


def select_context(history):
    """Pega as mensagens mais recentes que cabem no orcamento de caracteres enviado a IA."""
    chosen, total = [], 0
    for msg in reversed(history):
        total += len(msg.get("content", ""))
        if total > CONTEXT_CHAR_BUDGET and chosen:
            break
        chosen.append(msg)
    chosen.reverse()
    while chosen and chosen[0].get("role") != "user":  # comeca sempre numa fala do usuario
        chosen.pop(0)
    return chosen


def save_history(handler_input, history):
    mgr = handler_input.attributes_manager
    attrs = mgr.persistent_attributes
    attrs["history"] = trim_for_storage(history)
    mgr.persistent_attributes = attrs
    mgr.save_persistent_attributes()


def clear_history(handler_input):
    save_history(handler_input, [])


# ----------------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------------
def _ask(handler_input, speech, reprompt=MSG_REPROMPT):
    return (
        handler_input.response_builder.speak(speech).ask(reprompt).response
    )


def _converse(handler_input, text):
    """Fluxo principal: processa uma fala do usuario."""
    text = (text or "").strip()
    if not text:
        return _ask(handler_input, "Nao entendi. Pode repetir?")

    if is_reset_command(text):
        clear_history(handler_input)
        return _ask(handler_input, MSG_RESET)

    history = get_history(handler_input)
    try:
        answer = ask_ai(select_context(history), text)
    except AIError as e:
        logger.error("Falha na IA: %s", e)
        msg = MSG_NO_KEY if str(e) == "NO_KEY" else (MSG_TIMEOUT if str(e) == "TIMEOUT" else MSG_ERROR)
        return _ask(handler_input, msg)

    history += [
        {"role": "user", "content": text},
        {"role": "assistant", "content": answer},
    ]
    save_history(handler_input, history)
    return _ask(handler_input, clean_for_speech(answer))


class LaunchRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        return _ask(handler_input, MSG_WELCOME)


class ChatIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("ChatIntent")(handler_input)

    def handle(self, handler_input):
        query = ask_utils.get_slot_value(handler_input, "query")
        return _converse(handler_input, query)


class CatchAllIntentHandler(AbstractRequestHandler):
    """Pega qualquer fala livre e a repassa ao LLM (ele entende o contexto sozinho)."""

    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("CatchAllIntent")(handler_input)

    def handle(self, handler_input):
        text = ask_utils.get_slot_value(handler_input, "text")
        norm = _normalize(text)
        # O pega-tudo pode capturar comandos basicos: tratamos aqui.
        if norm in STOP_WORDS:
            return handler_input.response_builder.speak(MSG_BYE).set_should_end_session(True).response
        if norm in HELP_WORDS:
            return _ask(handler_input, MSG_HELP)
        return _converse(handler_input, text)


class ResetHistoryIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("ResetHistoryIntent")(handler_input)

    def handle(self, handler_input):
        clear_history(handler_input)
        return _ask(handler_input, MSG_RESET)


class HelpIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("AMAZON.HelpIntent")(handler_input)

    def handle(self, handler_input):
        return _ask(handler_input, MSG_HELP)


class CancelOrStopIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("AMAZON.CancelIntent")(handler_input) or ask_utils.is_intent_name(
            "AMAZON.StopIntent"
        )(handler_input)

    def handle(self, handler_input):
        return handler_input.response_builder.speak(MSG_BYE).set_should_end_session(True).response


class FallbackIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("AMAZON.FallbackIntent")(handler_input)

    def handle(self, handler_input):
        return _ask(
            handler_input,
            "Nao entendi. Fale de novo comecando com sobre, e, me diga ou quero saber.",
        )


class SessionEndedRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_request_type("SessionEndedRequest")(handler_input)

    def handle(self, handler_input):
        reason = getattr(handler_input.request_envelope.request, "reason", None)
        logger.info("Sessao encerrada: %s", reason)
        return handler_input.response_builder.response


class CatchAllExceptionHandler(AbstractExceptionHandler):
    def can_handle(self, handler_input, exception):
        return True

    def handle(self, handler_input, exception):
        logger.exception("Erro nao tratado: %s", exception)
        return _ask(handler_input, MSG_ERROR)


# ----------------------------------------------------------------------------
# Skill
# ----------------------------------------------------------------------------
def _build_persistence_adapter():
    table = os.environ.get("DYNAMODB_PERSISTENCE_TABLE_NAME")
    if not table:
        raise RuntimeError("DYNAMODB_PERSISTENCE_TABLE_NAME nao definido (use Alexa-hosted ou configure a tabela).")
    import boto3

    region = os.environ.get("DYNAMODB_PERSISTENCE_REGION") or os.environ.get("AWS_REGION")
    return DynamoDbAdapter(
        table_name=table,
        create_table=False,
        dynamodb_resource=boto3.resource("dynamodb", region_name=region),
    )


sb = CustomSkillBuilder(persistence_adapter=_build_persistence_adapter())

sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(ResetHistoryIntentHandler())
sb.add_request_handler(ChatIntentHandler())
sb.add_request_handler(CatchAllIntentHandler())
sb.add_request_handler(HelpIntentHandler())
sb.add_request_handler(CancelOrStopIntentHandler())
sb.add_request_handler(FallbackIntentHandler())
sb.add_request_handler(SessionEndedRequestHandler())
sb.add_exception_handler(CatchAllExceptionHandler())

lambda_handler = sb.lambda_handler()
