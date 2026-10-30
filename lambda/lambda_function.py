# -*- coding: utf-8 -*-
"""Super Alexa - Custom Skill que conversa com IA (Gemini Web Anonimo/Autenticado ou Groq),
com pesquisa na web, alternancia de provedor por voz e memoria persistente no DynamoDB.
"""
import asyncio
import concurrent.futures
import html
import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

# Garante que boto3 encontre uma regiao padrao mesmo se o runtime nao tiver injetado
if "AWS_DEFAULT_REGION" not in os.environ and "AWS_REGION" not in os.environ:
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

import ask_sdk_core.utils as ask_utils
from ask_sdk_core.dispatch_components import (
    AbstractExceptionHandler,
    AbstractRequestHandler,
)
from ask_sdk_core.skill_builder import CustomSkillBuilder

try:
    from ask_sdk_dynamodb.adapter import DynamoDbAdapter
except Exception as e:
    DynamoDbAdapter = None

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ----------------------------------------------------------------------------
# Configuracao
# ----------------------------------------------------------------------------
DEFAULT_PROVIDER = os.environ.get("AI_DEFAULT_PROVIDER", "groq").strip().lower()  # "groq" (rapido) ou "gemini"
BASE_URL = os.environ.get("AI_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
DEFAULT_MODELS = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
    "openai/gpt-oss-safeguard-20b",
    "allam-2-7b",
]
MODELS = [m.strip() for m in os.environ.get("AI_MODELS", "").split(",") if m.strip()] or DEFAULT_MODELS

# Memoria: guardamos TUDO no DynamoDB ate este teto de bytes (item maximo = 400 KB);
# so ao estourar o teto descartamos as mensagens mais antigas.
MAX_STORED_BYTES = int(os.environ.get("MAX_STORED_BYTES", str(300 * 1024)))
# Para a IA enviamos as mensagens recentes completas; so reduzimos no fallback se bater 429.
CONTEXT_CHAR_BUDGET = int(os.environ.get("CONTEXT_CHAR_BUDGET", "3500"))
MAX_CONTEXT_TURNS = int(os.environ.get("MAX_CONTEXT_TURNS", "8"))  # ultimas 8 mensagens (4 trocas completas)
MAX_TOKENS = int(os.environ.get("AI_MAX_TOKENS", "600"))
# A Alexa derruba a resposta apos ~8s; deixamos margem.
TOTAL_BUDGET_SECONDS = float(os.environ.get("AI_TOTAL_BUDGET", "7.2"))
GEMINI_BUDGET_SECONDS = float(os.environ.get("GEMINI_BUDGET", "5.8"))
MAX_SPEECH_CHARS = 1500

# Busca web automatica por baixo dos panos (DuckDuckGo Lite, ~0.4s).
WEB_SEARCH_ENABLED = os.environ.get("AI_SEARCH_ENABLED", "1") != "0"
WEB_SEARCH_TIMEOUT = float(os.environ.get("AI_WEB_SEARCH_TIMEOUT", "2.0"))
WEB_SEARCH_MAX_RESULTS = int(os.environ.get("AI_WEB_SEARCH_RESULTS", "6"))

# Cooldown de modelos com 429 (persiste enquanto o Lambda estiver "quente").
_cooldown_until = {}
DEFAULT_COOLDOWN = 60.0
_last_engine_used = None  # registra exatamente quem gerou a ultima resposta (ex: "Google Gemini" ou "Groq (qwen3.8-27b)")
_last_topic_by_user = {}  # guarda o ultimo assunto pesquisado mesmo se um turno anterior der erro

# Tarefas de pesquisa longa em andamento por usuario: {user_id: {"future": fut, "question": str, "provider": str, "started": float}}
_pending_tasks = {}
_executor = None
_request_start_time = 0.0
_lambda_context = None
PROGRESSIVE_WAIT_SECONDS = 2.0
MAX_LAMBDA_SAFE_SECONDS = 5.2  # margem segura para NUNCA estourar os 8s do container AWS Lambda (mesmo em cold start)
CONTINUE_PHRASES = (
    "continuar", "continue", "pode continuar", "pronto", "ok", "pode falar",
    "diga", "me diga", "fale", "e ai", "terminou", "ja terminou", "ler resposta",
)
MSG_PROGRESSIVE = "Só um instante, estou pesquisando..."
MSG_HOLD = "A pesquisa está bem completa, diga: continuar, para eu ler a resposta!"


def _current_br_date():
    now_br = datetime.now(timezone(timedelta(hours=-3)))
    return now_br.strftime("%d/%m/%Y"), now_br.year


def get_system_prompt():
    hoje_str, ano_atual = _current_br_date()
    return (
        f"Você é a Super Alexa, uma assistente de voz inteligente, simpática e direta. "
        f"DATA ATUAL: Hoje é {hoje_str} (ano de {ano_atual}). Seu treinamento original parou no passado, "
        f"portanto NUNCA diga que eventos ou eleições de {ano_atual} ainda não ocorreram ou que as últimas foram em 2022. "
        f"Responda SEMPRE em português do Brasil com acentuação ortográfica correta e completa "
        f"(use á, é, í, ó, ú, ã, õ, ç, ê, ô corretamente para a síntese de voz), "
        f"de forma curta e natural para ser falada em voz alta (no máximo 2 ou 3 frases curtas, a menos que o usuário peça mais detalhes). "
        f"NÃO use markdown, listas, asteriscos, emojis, tabelas ou links, e NÃO tente chamar ferramentas/tools externas. "
        f"Reconstrua frases que tiveram a primeira palavra cortada pela Alexa usando o histórico da conversa, "
        f"mas trate um assunto novo do zero sem misturar com o tema anterior. "
        f"PRIORIDADE DE FATOS: Baseie-se SEMPRE nos [Resultados de pesquisa na web] fornecidos na mensagem "
        f"para responder sobre fatos recentes, eleições (incluindo 1º e 2º turno), notícias, preços de imóveis, pessoas ou jogos. "
        f"NUNCA invente nomes, candidatos ou números que não estejam nos resultados da pesquisa."
    )


SYSTEM_PROMPT = get_system_prompt()

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

SWITCH_TO_GEMINI = (
    "usar gemini", "usar o gemini", "mudar para gemini", "mudar para o gemini",
    "trocar para gemini", "trocar para o gemini", "ativar gemini", "ativar o gemini",
    "modo gemini", "ligar gemini",
    # Formas com a primeira palavra cortada pela Alexa ("mudar {query}", "usar {query}", "ir {query}")
    "para gemini", "para o gemini", "pro gemini", "o gemini", "no gemini", "pelo gemini", "gemini",
    "google gemini", "para google gemini", "o google gemini",
)
SWITCH_TO_GROQ = (
    "usar groq", "usar o groq", "mudar para groq", "mudar para o groq",
    "trocar para groq", "trocar para o groq", "ativar groq", "ativar o groq",
    "modo groq", "ligar groq",
    "usar grok", "usar o grok", "mudar para grok", "trocar para grok", "ativar grok", "modo grok",
    "usar brock", "mudar para brock", "usar glock", "mudar para glock",
    # Formas com a primeira palavra cortada pela Alexa
    "para groq", "para o groq", "pro groq", "o groq", "no groq", "pelo groq", "groq",
    "para grok", "para o grok", "pro grok", "o grok", "no grok", "pelo grok", "grok",
    "para brock", "para glock",
)
STATUS_PHRASES = (
    "qual inteligencia artificial", "qual ia", "qual modelo", "qual provedor",
    "qual inteligencia voce esta usando", "qual modelo voce esta usando", "quem e voce",
    "qual modelo voce e", "qual seu modelo", "qual o seu modelo", "qual o modelo",
    "que modelo voce usa", "que modelo voce esta usando", "qual inteligencia voce e",
    "qual e o modelo", "qual e o provedor", "qual e a ia", "qual e a inteligencia",
)
GEMINI_HISTORY_ON_PHRASES = (
    "salvar historico no gemini", "salvar no gemini", "ativar historico do gemini",
    "ativar historico no gemini", "com historico no gemini", "gravar historico no gemini",
    "desativar chat temporario", "desativar modo temporario", "ativar gemini na minha conta",
    "ativar o gemini na minha conta", "usar gemini na minha conta", "usar minha conta no gemini",
    "salvar historico", "com historico", "gravar historico", "modo conta", "modo logado",
)
GEMINI_HISTORY_OFF_PHRASES = (
    "nao salvar historico no gemini", "nao salvar no gemini", "desativar historico do gemini",
    "desativar historico no gemini", "sem historico no gemini", "chat temporario",
    "chat temporario no gemini", "modo temporario", "modo temporario no gemini",
    "ativar gemini anonimo", "ativar o gemini anonimo", "gemini anonimo", "usar gemini anonimo",
    "modo anonimo", "modo anonimo no gemini", "ativar modo anonimo", "ativar o modo anonimo",
    "sem historico", "nao salvar historico", "nao salvar", "anonimo",
)

MSG_WELCOME = "Modo super ativado! Pode falar, sobre o que vamos conversar?"
LAUNCH_PHRASES = {
    "modo super", "abrir modo super", "abra modo super", "abra o modo super",
    "iniciar modo super", "ativar modo super", "comecar modo super", "super alexa",
}
STOP_WORDS = {
    "parar", "para", "pare", "sair", "tchau", "cancelar", "cancela",
    "encerrar", "encerra", "terminar", "termina", "ate logo", "chega", "fechar", "basta",
}
HELP_WORDS = {"ajuda", "me ajuda", "socorro"}
MSG_REPROMPT = "Pode continuar, estou ouvindo."
MSG_HELP = (
    "Eu sou uma assistente com inteligência artificial e pesquisa na web. Faça qualquer pergunta "
    "e continue a conversa normalmente. Você pode dizer: usar gemini, ou usar groq, para escolher o cérebro. "
    "Para apagar minha memória, diga: nova conversa. Para sair, diga: parar."
)
MSG_RESET = "Pronto, apaguei a memória. Vamos começar uma nova conversa!"
MSG_BYE = "Até logo!"
MSG_ERROR = "Desculpe, tive um problema para pensar na resposta. Pode repetir?"
MSG_TIMEOUT = "Desculpe, a resposta demorou demais. Pode perguntar de novo?"
MSG_NO_KEY = "A chave da inteligência artificial não está configurada. Verifique a configuração da skill."


def _load_secrets():
    path = os.path.join(os.path.dirname(__file__), "secrets.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _load_api_key():
    key = os.environ.get("GROQ_API_KEY") or os.environ.get("AI_API_KEY")
    if key:
        return key.strip()
    data = _load_secrets()
    return (data.get("GROQ_API_KEY") or data.get("AI_API_KEY") or "").strip() or None


def _clean_cookie_val(val):
    if not val or not isinstance(val, str):
        return None
    v = val.strip().strip("'\"").replace("\r", "").replace("\n", "").strip()
    if not v or v.lower().startswith(("cole_", "seu_", "sua_", "insira_")):
        return None
    return v


def _load_gemini_cookies():
    data = _load_secrets()
    psid = (
        os.environ.get("GEMINI_1PSID")
        or os.environ.get("__Secure-1PSID")
        or data.get("GEMINI_1PSID")
        or data.get("Secure_1PSID")
        or data.get("__Secure-1PSID")
    )
    psidts = (
        os.environ.get("GEMINI_1PSIDTS")
        or os.environ.get("__Secure-1PSIDTS")
        or data.get("GEMINI_1PSIDTS")
        or data.get("Secure_1PSIDTS")
        or data.get("__Secure-1PSIDTS")
    )
    return _clean_cookie_val(psid), _clean_cookie_val(psidts)


def _load_gemini_config():
    """Carrega configuracoes opcionais do Gemini (modelo preferido, nivel de raciocinio e historico na conta)."""
    data = _load_secrets()
    preferred_model = (os.environ.get("GEMINI_MODEL") or data.get("GEMINI_MODEL") or "").strip()
    thinking = (os.environ.get("GEMINI_THINKING") or data.get("GEMINI_THINKING") or "medium").strip().lower()
    extended_thinking = thinking in ("extended", "high", "deep", "2", "true", "alto", "profundo")

    raw_save = os.environ.get("GEMINI_SAVE_HISTORY")
    if raw_save is None:
        raw_save = data.get("GEMINI_SAVE_HISTORY", True)
    if isinstance(raw_save, bool):
        save_history = raw_save
    else:
        save_history = str(raw_save).strip().lower() not in ("0", "false", "no", "nao", "off")

    return {
        "preferred_model": preferred_model,
        "extended_thinking": extended_thinking,
        "thinking_label": "raciocínio estendido" if extended_thinking else "raciocínio médio",
        "save_history": save_history,
    }


# ----------------------------------------------------------------------------
# Cliente Gemini Web (HanaokaYuzu/Gemini-API)
# ----------------------------------------------------------------------------
_gemini_loop = None
_gemini_client = None
_gemini_ready = False
_gemini_authenticated = False
_gemini_init_task = None
_gemini_init_started = 0.0
_gemini_lock = threading.Lock()
_gemini_chat_meta_by_user = {}  # {user_id: {"metadata": [cid, rid, rcid], "model_id": str}}


def _ensure_gemini_loop():
    global _gemini_loop
    if _gemini_loop is None:
        _gemini_loop = asyncio.new_event_loop()
        t = threading.Thread(target=_gemini_loop.run_forever, daemon=True)
        t.start()
    return _gemini_loop


async def _init_gemini_async():
    global _gemini_client, _gemini_ready, _gemini_authenticated, _gemini_init_task, _gemini_init_started
    if _gemini_ready and _gemini_client is not None:
        return
    if _gemini_init_task is not None and not _gemini_init_task.done():
        if (time.monotonic() - _gemini_init_started) <= 16.0:
            await _gemini_init_task
            return
        try:
            _gemini_init_task.cancel()
        except Exception:
            pass
        _gemini_init_task = None

    async def _do_init():
        global _gemini_client, _gemini_ready, _gemini_authenticated
        from gemini_webapi import GeminiClient
        from gemini_webapi.constants import AccountStatus

        class _FastGeminiClient(GeminiClient):
            """Otimiza o init() executando apenas _fetch_user_status (descobre modelos da conta em ~0.3s)."""

            async def _init_rpc(self) -> None:
                await self._fetch_user_status()

            async def _sync_activity(self) -> None:
                self.last_activity_time = time.time()

        psid, psidts = _load_gemini_cookies()
        client = None
        is_auth = False

        if psid:
            try:
                cand = _FastGeminiClient(psid, psidts)
                await cand.init(timeout=15, auto_close=False, auto_refresh=True)
                if getattr(cand, "account_status", None) == AccountStatus.AVAILABLE:
                    client = cand
                    is_auth = True
                    models_info = [f"{m.display_name} ({m.model_name})" for m in (cand.list_models() or []) if m.is_available]
                    logger.info("Gemini Web autenticado na conta com sucesso! Modelos disponiveis: %s", models_info)
                else:
                    logger.warning(
                        "Cookies do Gemini nao autenticaram (status=%s); usando modo anonimo como fallback.",
                        getattr(cand, "account_status", None),
                    )
                    await cand.close()
            except Exception as e:
                logger.warning("Falha ao inicializar Gemini com cookies (%s); usando modo anonimo.", e)

        if client is None:
            client = _FastGeminiClient()
            await client.init(timeout=15, auto_close=False, auto_refresh=False)
            is_auth = False
            logger.info("Gemini Web inicializado em modo anonimo (sem cookies).")

        _gemini_client = client
        _gemini_authenticated = is_auth
        _gemini_ready = True

    _gemini_init_started = time.monotonic()
    _gemini_init_task = asyncio.create_task(_do_init())
    try:
        await _gemini_init_task
    except Exception:
        _gemini_ready = False
        _gemini_init_task = None
        raise


def warmup_gemini_background():
    """Dispara a inicializacao do Gemini em paralelo durante uma requisicao ativa."""
    if _gemini_ready and _gemini_client is not None:
        return
    try:
        loop = _ensure_gemini_loop()
        asyncio.run_coroutine_threadsafe(_init_gemini_async(), loop)
    except Exception as e:
        logger.warning("Aquecimento do Gemini em background falhou: %s", e)


def warmup_gemini(max_wait=5.0):
    """Aguarda a inicializacao do Gemini ate max_wait segundos sem cancelar se ainda estiver dentro do prazo normal (~8s)."""
    global _gemini_ready, _gemini_init_task
    if _gemini_ready and _gemini_client is not None:
        return True
    try:
        loop = _ensure_gemini_loop()
        fut = asyncio.run_coroutine_threadsafe(_init_gemini_async(), loop)
        fut.result(timeout=max_wait)
        return True
    except concurrent.futures.TimeoutError:
        logger.warning("Aquecimento do Gemini ainda em andamento apos %.1fs (continua em background).", max_wait)
        return False
    except Exception as e:
        logger.warning("Aquecimento do Gemini falhou: %s", e)
        _gemini_ready = False
        _gemini_init_task = None
        return False


def _select_gemini_candidates(client, preferred_model=""):
    """Retorna a lista ordenada de modelos disponiveis na conta, priorizando o 3.8 Flash."""
    avail = [m for m in (client.list_models() or []) if getattr(m, "is_available", True)]
    if not _gemini_authenticated:
        if avail:
            m0 = avail[0]
            return [(None, f"{m0.display_name or m0.model_name}, anônimo")]
        return [(None, "anônimo")]

    if not avail:
        return [(None, "padrão da conta")]

    pref = (preferred_model or "").strip().lower()

    def _priority(m):
        text_all = f"{m.display_name} {m.model_name} {' '.join(getattr(m, 'aliases', []))}".lower()
        is_lite = "lite" in text_all
        is_flash = "flash" in text_all and not is_lite
        is_38 = "3.8" in text_all or "3-8" in text_all
        is_pro = "pro" in text_all
        if pref and (pref == m.model_name.lower() or pref == m.display_name.lower() or pref in text_all):
            return 0
        if is_flash and is_38:
            return 1  # Prioridade #1: 3.8 Flash
        if is_flash:
            return 2  # Prioridade #2: Flash principal da conta
        if is_pro:
            return 3  # Prioridade #3: Pro
        if is_lite:
            return 4  # Prioridade #4: Flash-Lite
        return 5

    ordered = sorted(avail, key=_priority)
    return [(m, m.display_name or m.model_name) for m in ordered]


def build_gemini_prompt(history, user_text, search_query="", web_snippets="", has_active_chat=False):
    ctx = ""
    # Se ja estamos continuando um ChatSession ativo na conta do Gemini, o proprio Gemini ja possui o historico
    if history and not has_active_chat:
        lines = []
        for m in history[-6:]:
            role = "Usuário" if m.get("role") == "user" else "Assistente"
            lines.append(f"{role}: {m.get('content', '')}")
        ctx = "Histórico recente da conversa:\n" + "\n".join(lines) + "\n\n"
    search_block = ""
    if web_snippets:
        search_block = f"[Resultados de pesquisa na web para \"{search_query}\":\n{web_snippets}]\n\n"
    return (
        f"{get_system_prompt()}\n\n"
        f"{ctx}"
        f"{search_block}"
        f"Nova fala do usuário (pode ter tido a primeira palavra cortada pela Alexa): {user_text}\n\n"
        "Responda agora em português do Brasil (com acentuação correta) em no máximo 2 ou 3 frases curtas para voz, sem markdown:"
    )


def ask_gemini(
    history,
    user_text,
    timeout=11.0,
    search_query="",
    web_snippets="",
    user_id="default",
    save_gemini_history=None,
    gemini_chat_metadata=None,
):
    """Chama o Gemini Web (anonimo ou autenticado na conta com selecao de modelo e controle de historico)."""
    global _gemini_ready, _gemini_init_task, _last_engine_used
    t0 = time.monotonic()
    loop = _ensure_gemini_loop()
    if not _gemini_ready or _gemini_client is None:
        if not warmup_gemini(max_wait=min(4.2, max(2.0, timeout - 2.2))):
            raise AIError("GEMINI_NOT_WARMED")

    cfg = _load_gemini_config()
    should_save_history = cfg["save_history"] if save_gemini_history is None else bool(save_gemini_history)
    extended_thinking = cfg["extended_thinking"]
    thinking_label = cfg["thinking_label"]

    candidates = _select_gemini_candidates(_gemini_client, cfg["preferred_model"])
    last_err = None

    for model_obj, model_label in candidates:
        remaining = timeout - (time.monotonic() - t0)
        if remaining < 1.2:
            break

        model_id = getattr(model_obj, "model_id", "") if model_obj is not None else ""
        chat_session = None
        has_active_chat = False

        if _gemini_authenticated and should_save_history:
            mem_entry = _gemini_chat_meta_by_user.get(user_id) or {}
            saved_meta = mem_entry.get("metadata") or gemini_chat_metadata
            saved_model_id = mem_entry.get("model_id") or model_id
            # So reutiliza o ChatSession se o modelo for o mesmo (evita ErrorCode.MODEL_INCONSISTENT = 1050)
            if saved_meta and isinstance(saved_meta, list) and any(saved_meta) and saved_model_id == model_id:
                chat_session = _gemini_client.start_chat(metadata=list(saved_meta), model=model_obj)
                has_active_chat = bool(getattr(chat_session, "cid", ""))
            else:
                chat_session = _gemini_client.start_chat(model=model_obj)

        prompt = build_gemini_prompt(
            history,
            user_text,
            search_query=search_query,
            web_snippets=web_snippets,
            has_active_chat=has_active_chat,
        )

        for chat_attempt in range(2):
            remaining = timeout - (time.monotonic() - t0)
            if remaining < 1.0:
                break
            try:
                coro = _gemini_client.generate_content(
                    prompt,
                    model=model_obj,
                    chat=chat_session,
                    temporary=(not should_save_history) if _gemini_authenticated else False,
                    extended_thinking=extended_thinking,
                )
                fut = asyncio.run_coroutine_threadsafe(coro, loop)
                resp = fut.result(timeout=remaining)
                text = (getattr(resp, "text", None) or "").strip()
                text = re.sub(r"<[A-Za-z]+[^>]*>.*?</[A-Za-z]+>", "", text, flags=re.S)
                text = re.sub(r"<[A-Za-z]+[^>]*/>", "", text).strip()
                if not text:
                    raise AIError("resposta vazia do Gemini")

                if _gemini_authenticated:
                    if should_save_history and chat_session is not None:
                        _gemini_chat_meta_by_user[user_id] = {
                            "metadata": list(getattr(chat_session, "metadata", [])[:3]),
                            "model_id": model_id,
                        }
                    hist_tag = "histórico salvo" if should_save_history else "chat temporário"
                    _last_engine_used = f"Google Gemini ({model_label}, {thinking_label}, {hist_tag})"
                else:
                    _last_engine_used = f"Google Gemini ({model_label})"
                return text
            except concurrent.futures.TimeoutError:
                raise AIError("GEMINI_TIMEOUT")
            except Exception as e:
                last_err = e
                err_name = type(e).__name__
                # Se a sessao de chat salva na conta ficou inconsistente/antiga, tenta de novo criando um chat novo
                if has_active_chat and chat_attempt == 0:
                    logger.warning("ChatSession anterior do Gemini falhou (%s); iniciando novo chat na conta.", e)
                    chat_session = _gemini_client.start_chat(model=model_obj)
                    has_active_chat = False
                    prompt = build_gemini_prompt(
                        history,
                        user_text,
                        search_query=search_query,
                        web_snippets=web_snippets,
                        has_active_chat=False,
                    )
                    continue
                logger.warning("Modelo Gemini '%s' falhou (%s: %s).", model_label, err_name, e)
                break

    if last_err is not None:
        # Se nao for erro apenas de modelo/limite, reinicia o cliente na proxima chamada
        if type(last_err).__name__ not in ("UsageLimitExceededError", "ModelInvalidError"):
            _gemini_ready = False
            _gemini_init_task = None
        raise AIError(f"GEMINI_ERR: {type(last_err).__name__}")
    raise AIError("TIMEOUT")


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
    """So limpa a memoria se a frase inteira for um comando curto de reset (evita falsos positivos)."""
    norm = _normalize(text)
    if len(norm.split()) > 5:
        return False
    return any(norm == p or norm.endswith(" " + p) for p in RESET_PHRASES)


def detect_provider_switch(text):
    """So troca de provedor se for um comando curto explicito."""
    norm = _normalize(text)
    if len(norm.split()) > 6:
        return None
    if any(norm == p or norm.endswith(" " + p) for p in SWITCH_TO_GEMINI):
        return "gemini"
    if any(norm == p or norm.endswith(" " + p) for p in SWITCH_TO_GROQ):
        return "groq"
    return None


def detect_gemini_history_switch(text):
    """Detecta comando de voz para ativar/desativar o salvamento de historico na conta do Google Gemini."""
    norm = _normalize(text)
    if len(norm.split()) > 10:
        return None
    for p in GEMINI_HISTORY_OFF_PHRASES:
        if norm == p:
            return False
        if len(p.split()) >= 2 and p in norm:
            return False
        if f" {p} " in f" {norm} ":
            return False
    for p in GEMINI_HISTORY_ON_PHRASES:
        if norm == p:
            return True
        if len(p.split()) >= 2 and p in norm:
            return True
        if f" {p} " in f" {norm} ":
            return True
    return None


def is_status_command(text):
    """Detecta perguntas sobre qual IA/modelo/provedor esta sendo usado (mesmo sem a palavra 'qual')."""
    norm = _normalize(text)
    if len(norm.split()) > 10:
        return False
    if any(norm == p or p in norm for p in STATUS_PHRASES):
        return True
    has_target = any(w in norm.split() for w in ("modelo", "ia", "provedor", "cerebro")) or (
        "inteligencia artificial" in norm
    )
    has_verb = any(
        w in norm
        for w in ("usando", "usada", "usado", "ativo", "ativa", "atual", "voce e", "voce usa", "qual", "que")
    )
    return has_target and (has_verb or len(norm.split()) <= 3)


def clean_for_speech(text):
    """Normaliza acentuacao (NFC), remove markdown/emojis/citacoes/tags e escapa para SSML."""
    text = unicodedata.normalize("NFC", text or "")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"<[A-Za-z]+[^>]*>.*?</[A-Za-z]+>", "", text, flags=re.S)
    text = re.sub(r"<[A-Za-z]+[^>]*/>", "", text)
    text = re.sub(r"\u3010[^\u3011]*\u3011", "", text)
    text = re.sub(r"\[?\d+†[^\s\]]+\]?", "", text)
    text = re.sub(r"\[\d+\]", "", text)
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
# Pesquisa na Web Automatica (DuckDuckGo Lite)
# ----------------------------------------------------------------------------
CONVERSATIONAL_NOISE_RE = re.compile(
    r"^\s*(?:estou de acordo|de acordo|concordo|discordo|entendi|ok|certo|beleza|legal|pois e|"
    r"outro tema(?: agora)?|outro assunto(?: agora)?|mudando de assunto|por falar nisso|"
    r"voce acha que|acha que|o que voce acha|o que acha|sera que|me diga se|quero saber se|"
    r"vale a pena pagar num|vale a pena pagar em um|vale a pena comprar um|vale a pena|"
    r"isso esta incorreto|isso esta errado|esta incorreto|esta equivocado|esta errado|voce errou|nao foi isso|nao e isso|"
    r"ja ocorreram|ja ocorreu|ja aconteceram|ja aconteceu|ja teve|ja passaram|"
    r"faca uma pesquisa completa sobre|faca uma pesquisa sobre|faca uma pesquisa|"
    r"pesquise novamente o que eu pedi|pesquise novamente|pesquise de novo|pesquise melhor|procure melhor|procure novamente|"
    r"pesquise na internet sobre|pesquise na internet|pesquise na web sobre|pesquise na web|"
    r"pesquise sobre|pesquise|busque sobre|busque|procure sobre|procure|"
    r"quero saber sobre|quero saber as|quero saber os|quero saber de|quero saber|"
    r"e qual foi a|e qual foi o|qual foi a|qual foi o|e quais foram|quais foram|e como foi|"
    r"me fale sobre|fale sobre|me diga sobre|me diga|me explique|o que eu pedi|novamente|de novo)\b[\s,!?.:-]*",
    re.I,
)


def _extract_search_keywords(text):
    """Limpa muletas de conversa, correcoes ('esta incorreto', 'ja ocorreram') e comandos ('pesquise') da consulta."""
    t = (text or "").strip()
    for _ in range(8):
        norm = _normalize(t)
        m = CONVERSATIONAL_NOISE_RE.match(norm)
        if not m:
            break
        words = len(m.group(0).strip().split())
        t = " ".join(t.split()[words:]).strip(" ,!?.:-")
    # Tambem limpa sufixos de comando no final da frase (ex: "..., pesquise", "..., pesquise novamente")
    t = re.sub(
        r"[\s,!?.:-]+\b(?:pesquise(?: novamente| de novo| melhor)?|procure(?: novamente| melhor)?|o que eu pedi)\s*[!?.]*$",
        "",
        t,
        flags=re.I,
    ).strip(" ,!?.:-")

    norm_t = _normalize(t)
    if not norm_t:
        return ""

    # Se falar de apto/imovel/m2, adiciona 'preco metro quadrado' e remove o preco do proprio usuario
    if any(w in norm_t for w in ("apto", "apartamento", "imovel", "casa")) and (
        "m2" in norm_t or "metro" in norm_t or "mil" in norm_t or "k" in norm_t.split()
    ):
        t = re.sub(r",?\s*(?:por\s+|de\s+)?(?:r\$\s*)?\d+\s*(?:mil(?:hao|hoes)?|k)\b", "", t, flags=re.I).strip(
            " ,!?.:-"
        )
        if "preco" not in norm_t and "valor" not in norm_t:
            t = f"preco metro quadrado {t}"

    # Se perguntar sobre "turnos" no plural, expande para "primeiro turno e segundo turno"
    if "turnos" in norm_t.split() and "primeiro" not in norm_t:
        t = re.sub(r"\b(?:os\s+)?turnos\b", "primeiro turno e segundo turno", t, flags=re.I)

    # Se perguntar sobre "ultimas", "atual", "hoje", "eleicoes" sem citar um ano, anexa o ano atual (ex: 2026)
    _, ano_atual = _current_br_date()
    if not re.search(r"\b(?:19|20)\d{2}\b", norm_t) and any(
        w in norm_t.split()
        for w in ("ultimas", "ultima", "atuais", "atual", "hoje", "agora", "recentes", "recente", "eleicoes", "eleicao")
    ):
        t = re.sub(r"\b(?:das\s+|dos\s+|nas\s+|nos\s+|as\s+|os\s+)?(?:ultimas|ultima|últimeas|últimas|última)\b", "", t, flags=re.I)
        t = re.sub(r"\s+", " ", f"{t} {ano_atual}").strip()
    return t.strip()


def _dedupe_words(text):
    seen = set()
    out = []
    for w in text.split():
        lw = _normalize(w)
        if lw and lw in seen and (lw.isdigit() or (len(lw) > 4 and lw != "turno")):
            continue
        if lw:
            seen.add(lw)
        out.append(w)
    return " ".join(out)


def build_search_query(history, user_text, fallback_topic=""):
    """Monta a consulta de pesquisa combinando o contexto recente quando a fala e curta ou apenas 'pesquise'."""
    q = _extract_search_keywords(user_text)
    if len(q.split()) <= 5:
        prev_clean = []
        if history:
            for m in history:
                if m.get("role") == "user":
                    kw = _extract_search_keywords(m.get("content", ""))
                    if kw:
                        prev_clean.append(kw)
        if fallback_topic:
            fb_kw = _extract_search_keywords(fallback_topic)
            if fb_kw and (not prev_clean or prev_clean[-1] != fb_kw):
                prev_clean.append(fb_kw)
        if prev_clean:
            recent = prev_clean[-1]
            # Se o usuario especificou um novo ano (ex: "2026"), substitui anos antigos no contexto
            new_years = re.findall(r"\b(?:19|20)\d{2}\b", q)
            if new_years:
                recent = re.sub(r"\b(?:19|20)\d{2}\b", new_years[-1], recent)
            q = _dedupe_words(f"{recent} {q}".strip())
    return (q or (user_text or "").strip())[:180]


_search_cache = {}
_last_nonempty_snippets = ""
_DDG_UAS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)


def fetch_web_snippets(query, timeout=WEB_SEARCH_TIMEOUT, max_results=WEB_SEARCH_MAX_RESULTS):
    """Busca trechos atualizados no DuckDuckGo Lite (~0.4s) com cache e retentativa anti-202."""
    global _last_nonempty_snippets
    if not WEB_SEARCH_ENABLED or not query or len(query.strip()) < 2:
        return ""
    norm_q = _normalize(query)
    cached = _search_cache.get(norm_q)
    if cached and (time.monotonic() - cached[0]) < 300.0:
        return cached[1]

    url = "https://lite.duckduckgo.com/lite/"
    data = urllib.parse.urlencode({"q": query, "kl": "br-pt"}).encode("utf-8")
    for attempt in range(2):
        try:
            ua = _DDG_UAS[(hash(norm_q) + attempt) % len(_DDG_UAS)]
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "User-Agent": ua,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept-Language": "pt-BR,pt;q=0.9",
                    "Referer": "https://lite.duckduckgo.com/",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=max(0.8, timeout / (attempt + 1))) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            titles = re.findall(r"<a rel=\"nofollow\" href=\"[^\"]+\" class='result-link'>(.*?)</a>", raw, re.S)
            snippets = re.findall(r"<td class='result-snippet'>(.*?)</td>", raw, re.S)
            out = []
            for idx, s in enumerate(snippets[:max_results]):
                t_clean = html.unescape(re.sub(r"<[^>]+>", "", titles[idx])).strip() if idx < len(titles) else ""
                s_clean = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()
                if s_clean:
                    out.append(f"- {t_clean}: {s_clean}" if t_clean else f"- {s_clean}")
            if out:
                res = "\n".join(out)
                _search_cache[norm_q] = (time.monotonic(), res)
                _last_nonempty_snippets = res
                return res
            if attempt == 0:
                time.sleep(0.25)
        except Exception as e:
            logger.warning("Busca web DuckDuckGo falhou (%s): %s", query, e)
    return _last_nonempty_snippets


# ----------------------------------------------------------------------------
# Cliente da IA (Groq + Orquestrador Gemini/Groq)
# ----------------------------------------------------------------------------
class AIError(Exception):
    pass


def _post_chat(model, messages, api_key, timeout, max_tokens=MAX_TOKENS, reasoning_effort="medium"):
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.4,
    }
    if "gpt-oss" in model and reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
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


def _in_cooldown(model):
    return _cooldown_until.get(model, 0) > time.monotonic()


def _start_cooldown(model, http_error):
    wait = DEFAULT_COOLDOWN
    try:
        wait = min(float(http_error.headers.get("retry-after", DEFAULT_COOLDOWN)), 300.0)
    except (TypeError, ValueError, AttributeError):
        pass
    _cooldown_until[model] = time.monotonic() + wait
    logger.warning("Modelo %s em cooldown por %.0fs (429)", model, wait)


def ask_groq(history, user_text, deadline=None, search_query="", web_snippets=""):
    """Enriquece a pergunta com pesquisa DuckDuckGo e chama a cascata do Groq."""
    global _last_engine_used
    api_key = _load_api_key()
    if not api_key:
        raise AIError("NO_KEY")

    deadline = deadline or (time.monotonic() + TOTAL_BUDGET_SECONDS)

    if not search_query:
        search_query = build_search_query(history, user_text)
    if not web_snippets and WEB_SEARCH_ENABLED and (deadline - time.monotonic()) > 2.2:
        web_snippets = fetch_web_snippets(
            search_query, timeout=min(WEB_SEARCH_TIMEOUT, deadline - time.monotonic() - 1.2)
        )

    effective_question = user_text
    if not _extract_search_keywords(user_text) and search_query:
        effective_question = f"{user_text} (Assunto: {search_query})"

    augmented_user = effective_question
    if web_snippets:
        augmented_user = (
            f"Fala do usuário (pode ter tido a primeira palavra cortada pela Alexa): \"{effective_question}\"\n\n"
            f"[Resultados de pesquisa na web para \"{search_query}\":\n{web_snippets}]"
        )

    messages = [{"role": "system", "content": get_system_prompt()}] + history + [
        {"role": "user", "content": augmented_user}
    ]

    candidates = [m for m in MODELS if not _in_cooldown(m)] + [m for m in MODELS if _in_cooldown(m)]

    last_err = None
    timed_out = False
    for model in candidates:
        remaining = deadline - time.monotonic()
        if remaining < 0.6:
            break
        try:
            ans = _post_chat(model, messages, api_key, timeout=min(remaining, 4.5))
            _last_engine_used = f"Groq ({model.split('/')[-1]})"
            return ans
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                _start_cooldown(model, e)
            logger.warning("Modelo %s HTTP %s", model, e.code)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            timed_out = timed_out or "timed out" in str(e).lower()
            last_err = e
            logger.warning("Modelo %s falhou: %s", model, e)
        except (AIError, KeyError, IndexError, ValueError) as e:
            last_err = e
            logger.warning("Modelo %s resposta invalida: %s", model, e)

    # Segunda passada compacta (menos tokens reservados) caso todos tenham batido 429 por limite de TPD/TPM
    if (deadline - time.monotonic()) > 1.4:
        time.sleep(0.5)
        compact_user = effective_question
        if web_snippets:
            compact_user = f"Pergunta: \"{effective_question}\"\n[Pesquisa web:\n{web_snippets[:650]}]"
        compact_msgs = [{"role": "system", "content": get_system_prompt()}] + history[-2:] + [
            {"role": "user", "content": compact_user}
        ]
        for model in sorted(MODELS, key=lambda m: _cooldown_until.get(m, 0)):
            remaining = deadline - time.monotonic()
            if remaining < 0.6:
                break
            try:
                ans = _post_chat(
                    model, compact_msgs, api_key, timeout=min(remaining, 3.5), max_tokens=200, reasoning_effort="low"
                )
                _last_engine_used = f"Groq ({model.split('/')[-1]})"
                return ans
            except Exception as e:
                last_err = e
    raise AIError("TIMEOUT" if timed_out else f"FAILED: {last_err}")


def ask_ai(history, user_text, **kwargs):
    """Orquestra a chamada usando o provedor escolhido ('gemini' ou 'groq') com fallback automatico."""
    global _last_engine_used
    provider = kwargs.get("provider") or DEFAULT_PROVIDER
    budget = kwargs.get("budget") or 14.0  # permite pesquisas profundas de ate 14s com refresh!
    deadline = kwargs.get("deadline") or (time.monotonic() + budget)
    fallback_topic = kwargs.get("fallback_topic") or ""
    user_id = kwargs.get("user_id") or "default"
    save_gemini_history = kwargs.get("save_gemini_history")
    gemini_chat_metadata = kwargs.get("gemini_chat_metadata")

    # Se o provedor for Gemini, dispara o aquecimento em paralelo com a busca DuckDuckGo
    if provider == "gemini":
        warmup_gemini_background()

    search_query = build_search_query(history, user_text, fallback_topic=fallback_topic)
    web_snippets = ""
    if WEB_SEARCH_ENABLED and (deadline - time.monotonic()) > 3.0:
        web_snippets = fetch_web_snippets(
            search_query, timeout=min(WEB_SEARCH_TIMEOUT, deadline - time.monotonic() - 2.0)
        )

    effective_question = user_text
    if not _extract_search_keywords(user_text) and search_query:
        effective_question = f"{user_text} (Assunto: {search_query})"

    if provider == "gemini":
        try:
            # Reserva pelo menos 3.2s do orcamento para o fallback Groq caso o Gemini falhe!
            gem_timeout = max(4.0, min(11.5, deadline - time.monotonic() - 3.2))
            return ask_gemini(
                history,
                effective_question,
                timeout=gem_timeout,
                search_query=search_query,
                web_snippets=web_snippets,
                user_id=user_id,
                save_gemini_history=save_gemini_history,
                gemini_chat_metadata=gemini_chat_metadata,
            )
        except Exception as e:
            logger.warning("Gemini falhou/demorou (%s); fazendo fallback automatico para Groq.", e)
            try:
                return ask_groq(
                    history,
                    effective_question,
                    deadline=deadline,
                    search_query=search_query,
                    web_snippets=web_snippets,
                )
            except Exception as groq_e:
                raise AIError(f"Gemini ({e}) e fallback Groq ({groq_e})")
    else:
        try:
            return ask_groq(
                history,
                effective_question,
                deadline=deadline,
                search_query=search_query,
                web_snippets=web_snippets,
            )
        except AIError as e:
            if str(e) != "NO_KEY" and (deadline - time.monotonic()) > 2.5:
                try:
                    ans = ask_gemini(
                        history,
                        effective_question,
                        timeout=deadline - time.monotonic(),
                        search_query=search_query,
                        web_snippets=web_snippets,
                        user_id=user_id,
                        save_gemini_history=save_gemini_history,
                        gemini_chat_metadata=gemini_chat_metadata,
                    )
                    _last_engine_used = f"{_last_engine_used or 'Google Gemini'} (fallback)"
                    return ans
                except Exception:
                    pass
            raise


def _get_executor():
    global _executor
    if _executor is None:
        _executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    return _executor


def send_progressive_speech(handler_input, speech_text=MSG_PROGRESSIVE):
    """Envia fala intermediaria ('Estou pesquisando...') para a Alexa enquanto a IA processa."""
    try:
        sys_ctx = handler_input.request_envelope.context.system
        api_endpoint = getattr(sys_ctx, "api_endpoint", None)
        api_token = getattr(sys_ctx, "api_access_token", None)
        req_id = getattr(handler_input.request_envelope.request, "request_id", None)
        if not (api_endpoint and api_token and req_id):
            return
        payload = {
            "header": {"requestId": req_id},
            "directive": {"type": "VoicePlayer.Speak", "speech": speech_text},
        }
        req = urllib.request.Request(
            f"{api_endpoint.rstrip('/')}/v1/directives",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=1.2):
            pass
    except Exception as e:
        logger.debug("Progressive response ignorado: %s", e)


def is_continue_command(text):
    norm = _normalize(text)
    if not norm:
        return True  # Ex.: "continue {query}" no ChatIntent onde a Alexa engoliu 'continue'
    return norm in CONTINUE_PHRASES


# ----------------------------------------------------------------------------
# Memoria e Preferencias no DynamoDB (com fallback em SessionAttributes)
# ----------------------------------------------------------------------------
def get_user_state(handler_input):
    history = []
    provider = DEFAULT_PROVIDER
    try:
        sess = handler_input.attributes_manager.session_attributes or {}
        if "provider" in sess:
            provider = sess["provider"]
    except Exception:
        sess = {}

    try:
        attrs = handler_input.attributes_manager.persistent_attributes
        history = list(attrs.get("history", []))
        provider = attrs.get("provider") or provider
        return history, provider
    except Exception as e:
        logger.warning("Falha ao ler DynamoDB (usando sessao/padrao): %s", e)
        return history, provider


def get_gemini_prefs(handler_input):
    save_hist = None
    meta = None
    try:
        sess = handler_input.attributes_manager.session_attributes or {}
        save_hist = sess.get("gemini_save_history")
    except Exception:
        pass
    try:
        attrs = handler_input.attributes_manager.persistent_attributes
        if save_hist is None:
            save_hist = attrs.get("gemini_save_history")
        meta = attrs.get("gemini_chat_metadata")
    except Exception:
        pass
    return save_hist, meta


def _size(messages):
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))


def trim_for_storage(history):
    if _size(history) <= MAX_STORED_BYTES:
        return history
    total, keep_from = 2, len(history)
    for i in range(len(history) - 1, -1, -1):
        total += _size(history[i]) + 2
        if total > MAX_STORED_BYTES:
            break
        keep_from = i
    history = history[keep_from:]
    while history and history[0].get("role") != "user":
        history = history[1:]
    return history


def select_context(history):
    recent_slice = history[-MAX_CONTEXT_TURNS:] if MAX_CONTEXT_TURNS else history
    chosen, total = [], 0
    for msg in reversed(recent_slice):
        total += len(msg.get("content", ""))
        if total > CONTEXT_CHAR_BUDGET and chosen:
            break
        chosen.append(msg)
    chosen.reverse()
    while chosen and chosen[0].get("role") != "user":
        chosen.pop(0)
    return chosen


def save_user_state(
    handler_input,
    history=None,
    provider=None,
    gemini_save_history=None,
    gemini_chat_metadata=...,
):
    try:
        sess = handler_input.attributes_manager.session_attributes or {}
        if provider is not None:
            sess["provider"] = provider
        if gemini_save_history is not None:
            sess["gemini_save_history"] = bool(gemini_save_history)
        handler_input.attributes_manager.session_attributes = sess
    except Exception:
        pass

    try:
        mgr = handler_input.attributes_manager
        attrs = mgr.persistent_attributes
        if history is not None:
            attrs["history"] = trim_for_storage(history)
        if provider is not None:
            attrs["provider"] = provider
        if gemini_save_history is not None:
            attrs["gemini_save_history"] = bool(gemini_save_history)
        if gemini_chat_metadata is not ...:
            attrs["gemini_chat_metadata"] = gemini_chat_metadata
        mgr.persistent_attributes = attrs
        mgr.save_persistent_attributes()
    except Exception as e:
        logger.warning("Falha ao salvar estado no DynamoDB: %s", e)


def get_history(handler_input):
    return get_user_state(handler_input)[0]


def save_history(handler_input, history):
    save_user_state(handler_input, history=history)


def clear_history(handler_input):
    user_id = _get_user_id(handler_input)
    _gemini_chat_meta_by_user.pop(user_id, None)
    save_user_state(handler_input, history=[], gemini_chat_metadata=None)


# ----------------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------------
def _ask(handler_input, speech, reprompt=MSG_REPROMPT):
    return handler_input.response_builder.speak(speech).ask(reprompt).response


def _get_user_id(handler_input):
    try:
        return handler_input.request_envelope.context.system.user.user_id or "default"
    except Exception:
        return "default"


def _wait_with_progressive(handler_input, future, req_start):
    """Aguarda a tarefa respeitando o tempo seguro do Lambda (MAX_LAMBDA_SAFE_SECONDS)."""
    elapsed = time.monotonic() - req_start
    wait1 = max(0.2, min(PROGRESSIVE_WAIT_SECONDS, MAX_LAMBDA_SAFE_SECONDS - elapsed - 0.5))
    try:
        return future.result(timeout=wait1), True
    except concurrent.futures.TimeoutError:
        send_progressive_speech(handler_input, MSG_PROGRESSIVE)
        elapsed = time.monotonic() - req_start
        wait2 = max(0.2, MAX_LAMBDA_SAFE_SECONDS - elapsed)
        try:
            return future.result(timeout=wait2), True
        except concurrent.futures.TimeoutError:
            return None, False


def _converse(handler_input, text):
    """Fluxo principal: processa uma fala do usuario com suporte a refresh de pesquisas longas."""
    req_start = time.monotonic()
    text = (text or "").strip()
    user_id = _get_user_id(handler_input)
    pending = _pending_tasks.get(user_id)
    if not text and not pending:
        return _ask(handler_input, "Não entendi. Pode repetir?")
    norm = _normalize(text)
    if norm in LAUNCH_PHRASES:
        return _ask(handler_input, MSG_WELCOME)
    if norm in STOP_WORDS:
        _pending_tasks.pop(user_id, None)
        return handler_input.response_builder.speak(MSG_BYE).set_should_end_session(True).response
    if norm in HELP_WORDS:
        return _ask(handler_input, MSG_HELP)
    if is_reset_command(text):
        _pending_tasks.pop(user_id, None)
        _last_topic_by_user.pop(user_id, None)
        clear_history(handler_input)
        return _ask(handler_input, MSG_RESET)

    # Alternancia por voz de salvamento de historico na conta do Google Gemini
    gem_hist_switch = detect_gemini_history_switch(text)
    if gem_hist_switch is not None:
        _pending_tasks.pop(user_id, None)
        warmup_gemini_background()
        if not gem_hist_switch:
            _gemini_chat_meta_by_user.pop(user_id, None)
            save_user_state(handler_input, provider="gemini", gemini_save_history=False, gemini_chat_metadata=None)
            return _ask(
                handler_input,
                "Pronto! Ativei o Google Gemini em modo temporário: não salvarei as conversas no seu histórico do Gemini. Pode perguntar!",
            )
        save_user_state(handler_input, provider="gemini", gemini_save_history=True)
        return _ask(
            handler_input,
            "Pronto! Ativei o Google Gemini com salvamento de histórico: suas conversas ficarão salvas na sua conta Google. Pode perguntar!",
        )

    # Troca de provedor por comando de voz ("usar gemini" / "usar groq")
    new_provider = detect_provider_switch(text)
    if new_provider:
        _pending_tasks.pop(user_id, None)
        save_user_state(handler_input, provider=new_provider)
        if new_provider == "gemini":
            warmup_gemini_background()
            return _ask(handler_input, "Pronto! Mudei o cérebro principal para o Google Gemini. Pode perguntar!")
        return _ask(handler_input, "Pronto! Mudei o cérebro principal para o Groq. Pode perguntar!")

    history, provider = get_user_state(handler_input)
    gem_save_hist, gem_chat_meta = get_gemini_prefs(handler_input)

    if is_status_command(text):
        if provider == "gemini":
            psid, _ = _load_gemini_cookies()
            cfg = _load_gemini_config()
            should_save = cfg["save_history"] if gem_save_hist is None else bool(gem_save_hist)
            modo_hist = "salvando histórico no seu Gemini" if should_save else "em modo de chat temporário"

            if _gemini_ready and _gemini_client is not None:
                cands = _select_gemini_candidates(_gemini_client, cfg["preferred_model"])
                top_label = cands[0][1] if cands else "padrão"
                if _gemini_authenticated:
                    all_labels = ", ".join(lbl for _, lbl in cands)
                    nome = (
                        f"Google Gemini conectado na sua conta (usando {top_label}, {cfg['thinking_label']}, "
                        f"{modo_hist}). Modelos liberados na sua conta: {all_labels}"
                    )
                else:
                    motivo = "cookies expiraram ou foram recusados" if psid else "sem cookies configurados"
                    nome = f"Google Gemini em modo anônimo ({top_label}, {motivo})"
            else:
                if psid:
                    pref_label = cfg["preferred_model"] or "priorizando 3.8 Flash"
                    nome = f"Google Gemini com sua conta configurada ({pref_label}, {cfg['thinking_label']}, {modo_hist})"
                else:
                    nome = "Google Gemini em modo anônimo (gratuito, sem cookies no secrets.json)"
        else:
            nome = "Groq (OpenAI gpt-oss-120b e contingência)"

        detalhe = f" A última resposta foi gerada por {_last_engine_used}." if _last_engine_used else ""
        return _ask(
            handler_input,
            f"O cérebro principal configurado é o {nome}.{detalhe} Para trocar, diga: usar gemini, ou usar groq.",
        )

    # Se havia uma pesquisa longa em andamento e o usuario disse "continuar" (ou similar),
    # retomamos a tarefa existente ganhando +8 segundos novos da Alexa!
    if pending and is_continue_command(text):
        fut = pending["future"]
        orig_question = pending["question"]
        try:
            answer, finished = _wait_with_progressive(handler_input, fut, req_start)
            if not finished:
                # Ainda processando: pede mais um 'continuar' para ganhar mais 8s
                return _ask(handler_input, "Ainda estou concluindo a pesquisa, diga: continuar, só mais uma vez!")
            _pending_tasks.pop(user_id, None)
        except Exception as e:
            _pending_tasks.pop(user_id, None)
            logger.error("Falha ao retomar pesquisa longa: %s", e)
            return _ask(handler_input, MSG_ERROR)

        history += [
            {"role": "user", "content": orig_question},
            {"role": "assistant", "content": answer},
        ]
        new_meta = (_gemini_chat_meta_by_user.get(user_id) or {}).get("metadata", ...)
        save_user_state(handler_input, history=history, gemini_chat_metadata=new_meta)
        return _ask(handler_input, clean_for_speech(answer))

    # Nova pergunta: dispara em background com orcamento amplo (14s) e monitora o relogio
    _pending_tasks.pop(user_id, None)
    prev_topic = _last_topic_by_user.get(user_id, "")
    clean_kw = _extract_search_keywords(text)
    if clean_kw:
        _last_topic_by_user[user_id] = build_search_query(history, text, fallback_topic=prev_topic)
    context = select_context(history)
    fut = _get_executor().submit(
        ask_ai,
        context,
        text,
        provider=provider,
        budget=(17.0 if provider == "gemini" else 14.0),
        fallback_topic=prev_topic,
        user_id=user_id,
        save_gemini_history=gem_save_hist,
        gemini_chat_metadata=gem_chat_meta,
    )
    try:
        answer, finished = _wait_with_progressive(handler_input, fut, req_start)
        if not finished:
            # Bateu 5.2s e a IA ainda esta pesquisando: guarda a tarefa e faz o refresh com o usuario!
            _pending_tasks[user_id] = {
                "future": fut,
                "question": text,
                "provider": provider,
                "started": time.monotonic(),
            }
            return _ask(handler_input, MSG_HOLD)
    except AIError as e:
        logger.error("Falha na IA: %s", e)
        msg = MSG_NO_KEY if str(e) == "NO_KEY" else (MSG_TIMEOUT if str(e) == "TIMEOUT" else f"Desculpe, ocorreu o erro: {clean_for_speech(str(e)[:120])}")
        return _ask(handler_input, msg)
    except Exception as e:
        logger.error("Falha inesperada na IA: %s", e)
        return _ask(handler_input, f"Desculpe, ocorreu o erro técnico: {clean_for_speech(type(e).__name__ + ' ' + str(e)[:120])}")

    history += [
        {"role": "user", "content": text},
        {"role": "assistant", "content": answer},
    ]
    new_meta = (_gemini_chat_meta_by_user.get(user_id) or {}).get("metadata", ...)
    save_user_state(handler_input, history=history, gemini_chat_metadata=new_meta)
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
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("CatchAllIntent")(handler_input)

    def handle(self, handler_input):
        text = ask_utils.get_slot_value(handler_input, "text")
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
            "Não consegui ouvir direito. Pode repetir começando com: me diga, ou quero saber?",
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
        return _ask(
            handler_input,
            f"Desculpe, erro interno no handler: {clean_for_speech(type(exception).__name__ + ' ' + str(exception)[:120])}",
        )


# ----------------------------------------------------------------------------
# Skill
# ----------------------------------------------------------------------------
def _build_persistence_adapter():
    if DynamoDbAdapter is None:
        return None
    table = os.environ.get("DYNAMODB_PERSISTENCE_TABLE_NAME")
    if not table:
        logger.warning("DYNAMODB_PERSISTENCE_TABLE_NAME nao definido. Persistencia DynamoDB desativada.")
        return None
    try:
        import boto3

        region = (
            os.environ.get("DYNAMODB_PERSISTENCE_REGION")
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1"
        )
        return DynamoDbAdapter(
            table_name=table,
            create_table=False,
            dynamodb_resource=boto3.resource("dynamodb", region_name=region),
        )
    except Exception as exc:
        logger.warning("Falha ao inicializar DynamoDbAdapter (%s). Continuando sem persistencia.", exc)
        return None


_adapter = _build_persistence_adapter()
sb = CustomSkillBuilder(persistence_adapter=_adapter) if _adapter else CustomSkillBuilder()

sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(ChatIntentHandler())
sb.add_request_handler(CatchAllIntentHandler())
sb.add_request_handler(ResetHistoryIntentHandler())
sb.add_request_handler(HelpIntentHandler())
sb.add_request_handler(CancelOrStopIntentHandler())
sb.add_request_handler(FallbackIntentHandler())
sb.add_request_handler(SessionEndedRequestHandler())
sb.add_exception_handler(CatchAllExceptionHandler())

lambda_handler = sb.lambda_handler()
