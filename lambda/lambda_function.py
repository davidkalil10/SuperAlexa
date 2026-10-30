# -*- coding: utf-8 -*-
"""Super Alexa - Custom Skill que conversa com IA (Gemini Web Anonimo/Autenticado ou Groq),
com pesquisa na web, alternancia de provedor por voz e memoria persistente no DynamoDB.
"""
import asyncio
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
DEFAULT_PROVIDER = os.environ.get("AI_DEFAULT_PROVIDER", "gemini").strip().lower()  # "gemini" (padrao) ou "groq"
BASE_URL = os.environ.get("AI_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
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
# Para a IA enviamos apenas as mensagens mais recentes que cabem neste orcamento de caracteres.
CONTEXT_CHAR_BUDGET = int(os.environ.get("CONTEXT_CHAR_BUDGET", "12000"))
MAX_TOKENS = int(os.environ.get("AI_MAX_TOKENS", "500"))
# A Alexa derruba a resposta apos ~8s; deixamos margem.
TOTAL_BUDGET_SECONDS = float(os.environ.get("AI_TOTAL_BUDGET", "7.2"))
GEMINI_BUDGET_SECONDS = float(os.environ.get("GEMINI_BUDGET", "5.8"))
MAX_SPEECH_CHARS = 1500

# Busca web automatica por baixo dos panos para o Groq (DuckDuckGo Lite, ~0.4s).
WEB_SEARCH_ENABLED = os.environ.get("AI_SEARCH_ENABLED", "1") != "0"
WEB_SEARCH_TIMEOUT = float(os.environ.get("AI_WEB_SEARCH_TIMEOUT", "2.2"))
WEB_SEARCH_MAX_RESULTS = int(os.environ.get("AI_WEB_SEARCH_RESULTS", "5"))

# Cooldown de modelos com 429 (persiste enquanto o Lambda estiver "quente").
_cooldown_until = {}
DEFAULT_COOLDOWN = 60.0
_last_engine_used = None  # registra exatamente quem gerou a ultima resposta (ex: "Google Gemini" ou "Groq (gpt-oss-120b)")

SYSTEM_PROMPT = (
    "Você é a Super Alexa, uma assistente de voz inteligente, simpática e direta. "
    "Responda SEMPRE em português do Brasil com acentuação ortográfica correta e completa "
    "(use sempre á, é, í, ó, ú, ã, õ, ç, ê, ô corretamente para que a síntese de voz pronuncie bem), "
    "de forma curta e natural para ser falada em voz alta (no máximo 2 ou 3 frases curtas, a menos que o usuário peça mais detalhes). "
    "NÃO use markdown, listas com marcadores, asteriscos, emojis, tabelas ou links. "
    "Se precisar enumerar, use frases corridas. Use o histórico da conversa para manter o contexto. "
    "IMPORTANTE SOBRE FRASES CORTADAS E CONTINUAÇÕES: A Alexa às vezes remove a primeira palavra da frase "
    "do usuário (como 'é um', 'pesquise', 'quero saber', 'sobre', 'como') ou o usuário responde com uma "
    "palavra solta ou complemento curto (ex: 'Ronin', 'um jogo j2me', 'e ele?'). Se a fala atual tiver relação "
    "com o assunto anterior, reconstrua o sentido combinando com o histórico; mas se for uma pergunta sobre "
    "uma nova pessoa ou novo assunto sem relação com o anterior, trate como um assunto novo do zero e NÃO "
    "misture com o tema antigo! "
    "IMPORTANTE SOBRE PESQUISA NA WEB: Use os dados de pesquisa na web fornecidos (ou sua busca Google nativa) "
    "para responder sobre fatos recentes, eleições, notícias, pessoas, empresas ou jogos específicos com precisão. "
    "NUNCA invente fatos. Se não encontrar informação confiável, diga isso com honestidade e peça mais detalhes. "
    "Você NÃO controla dispositivos, músicas, alarmes ou casa inteligente; se pedirem isso, "
    "explique gentilmente que nesta skill você só conversa."
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

SWITCH_TO_GEMINI = (
    "usar gemini", "usar o gemini", "mudar para gemini", "mudar para o gemini",
    "trocar para gemini", "trocar para o gemini", "ativar gemini", "ativar o gemini",
    "modo gemini", "ligar gemini",
)
SWITCH_TO_GROQ = (
    "usar groq", "usar o groq", "mudar para groq", "mudar para o groq",
    "trocar para groq", "trocar para o groq", "ativar groq", "ativar o groq",
    "modo groq", "ligar groq",
    # variacoes comuns de transcricao da Alexa para "groq"
    "usar grok", "usar o grok", "mudar para grok", "trocar para grok", "ativar grok", "modo grok",
    "usar brock", "mudar para brock", "usar glock", "mudar para glock",
)
STATUS_PHRASES = (
    "qual inteligencia artificial", "qual ia", "qual modelo", "qual provedor",
    "qual inteligencia voce esta usando", "qual modelo voce esta usando", "quem e voce",
)

MSG_WELCOME = "Modo super ativado! Pode falar, sobre o que vamos conversar?"
STOP_WORDS = {"parar", "para", "pare", "sair", "tchau", "cancelar", "cancela", "encerrar", "ate logo", "chega", "fechar"}
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


def _load_gemini_cookies():
    psid = os.environ.get("GEMINI_1PSID")
    psidts = os.environ.get("GEMINI_1PSIDTS")
    if not psid:
        data = _load_secrets()
        psid = data.get("GEMINI_1PSID") or data.get("Secure_1PSID")
        psidts = data.get("GEMINI_1PSIDTS") or data.get("Secure_1PSIDTS")
    return (psid.strip() if psid else None), (psidts.strip() if psidts else None)


# ----------------------------------------------------------------------------
# Cliente Gemini Web (HanaokaYuzu/Gemini-API) com aquecimento em background
# ----------------------------------------------------------------------------
_gemini_loop = None
_gemini_client = None
_gemini_ready = False
_gemini_lock = threading.Lock()


def _ensure_gemini_loop():
    global _gemini_loop
    if _gemini_loop is None:
        _gemini_loop = asyncio.new_event_loop()
        t = threading.Thread(target=_gemini_loop.run_forever, daemon=True)
        t.start()
    return _gemini_loop


async def _init_gemini_async():
    global _gemini_client, _gemini_ready
    from gemini_webapi import GeminiClient

    psid, psidts = _load_gemini_cookies()
    client = GeminiClient(psid, psidts) if psid else GeminiClient()
    await client.init(timeout=15, auto_close=False, auto_refresh=bool(psid))
    _gemini_client = client
    _gemini_ready = True
    logger.info("Gemini Web inicializado com sucesso (autenticado=%s).", bool(psid))


def warmup_gemini(max_wait=6.2):
    """Inicializa o Gemini ANTES do Lambda retornar (a AWS congela threads em background apos o retorno!)."""
    if _gemini_ready:
        return True
    with _gemini_lock:
        if _gemini_ready:
            return True
        try:
            loop = _ensure_gemini_loop()
            fut = asyncio.run_coroutine_threadsafe(_init_gemini_async(), loop)
            fut.result(timeout=max_wait)
            return True
        except Exception as e:
            logger.warning("Aquecimento do Gemini falhou/expirou: %s", e)
            return False


def build_gemini_prompt(history, user_text):
    ctx = ""
    if history:
        lines = []
        for m in history[-8:]:
            role = "Usuário" if m.get("role") == "user" else "Assistente"
            lines.append(f"{role}: {m.get('content', '')}")
        ctx = "Histórico recente da conversa:\n" + "\n".join(lines) + "\n\n"
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"{ctx}"
        f"Nova fala do usuário (pode ter tido a primeira palavra cortada pela Alexa): {user_text}\n\n"
        "Responda agora em português do Brasil (com acentuação correta) em no máximo 2 ou 3 frases curtas para voz, sem markdown:"
    )


def ask_gemini(history, user_text, timeout=GEMINI_BUDGET_SECONDS):
    """Chama o Gemini Web (que ja usa a Busca Google nativa)."""
    t0 = time.monotonic()
    loop = _ensure_gemini_loop()
    if not _gemini_ready:
        if not warmup_gemini(max_wait=max(1.5, timeout - 3.0)):
            raise AIError("GEMINI_NOT_WARMED")

    remaining = timeout - (time.monotonic() - t0)
    if remaining < 1.2:
        raise AIError("TIMEOUT")

    prompt = build_gemini_prompt(history, user_text)
    fut = asyncio.run_coroutine_threadsafe(_gemini_client.generate_content(prompt), loop)
    resp = fut.result(timeout=remaining)
    text = (getattr(resp, "text", None) or "").strip()
    text = re.sub(r"<[A-Za-z]+[^>]*>.*?</[A-Za-z]+>", "", text, flags=re.S)
    text = re.sub(r"<[A-Za-z]+[^>]*/>", "", text).strip()
    if not text:
        raise AIError("resposta vazia do Gemini")
    return text


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


def is_status_command(text):
    """Detecta perguntas sobre qual IA/modelo/provedor esta sendo usado (mesmo sem a palavra 'qual')."""
    norm = _normalize(text)
    if len(norm.split()) > 8:
        return False
    if any(norm == p or norm.endswith(p) for p in STATUS_PHRASES):
        return True
    has_target = any(w in norm.split() for w in ("modelo", "ia", "provedor", "cerebro")) or (
        "inteligencia artificial" in norm
    )
    has_verb = any(
        w in norm
        for w in ("usando", "usada", "usado", "ativo", "ativa", "atual", "voce e", "voce usa")
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
# Pesquisa na Web Automatica para o Groq (DuckDuckGo Lite)
# ----------------------------------------------------------------------------
STRIP_SEARCH_WORDS = (
    "pesquise na internet sobre ", "pesquise na internet ", "pesquise na web sobre ", "pesquise na web ",
    "na internet sobre ", "na internet ", "na web sobre ", "na web ",
    "pesquise sobre ", "pesquise ", "busque sobre ", "busque ", "procure sobre ", "procure ",
    "quero saber sobre ", "quero saber ", "me fale sobre ", "fale sobre ", "me diga sobre ", "me explique ",
)


CONVERSATIONAL_NOISE_RE = re.compile(
    r"^\s*(?:estou de acordo|de acordo|concordo|discordo|entendi|ok|certo|beleza|legal|pois e|"
    r"outro tema(?: agora)?|outro assunto(?: agora)?|mudando de assunto|por falar nisso|"
    r"voce acha que|acha que|o que voce acha|o que acha|sera que|me diga se|quero saber se|"
    r"vale a pena pagar num|vale a pena pagar em um|vale a pena comprar um|vale a pena|"
    r"esta equivocado|esta errado|isso esta errado|pesquise melhor|procure melhor|"
    r"pesquise na internet sobre|pesquise na internet|pesquise sobre|pesquise|busque sobre|busque|"
    r"procure sobre|procure|quero saber sobre|quero saber|me fale sobre|fale sobre|me explique)\b[\s,!?.:-]*",
    re.I,
)


def _extract_search_keywords(text):
    """Limpa muletas de conversa do inicio da frase para gerar uma consulta de busca limpa."""
    t = (text or "").strip()
    # Remove acentos temporariamente so para casar regex, ou aplica direto e limpa
    for _ in range(5):
        norm = _normalize(t)
        m = CONVERSATIONAL_NOISE_RE.match(norm)
        if not m:
            break
        words = len(m.group(0).strip().split())
        t = " ".join(t.split()[words:]).strip(" ,!?.:-")
    # Se falar de apto/imovel/m2, adiciona 'preco metro quadrado' e remove o preco do proprio usuario
    # (ex: "350 mil") da busca para nao enviesar os resultados do buscador!
    norm_t = _normalize(t)
    if any(w in norm_t for w in ("apto", "apartamento", "imovel", "casa")) and (
        "m2" in norm_t or "metro" in norm_t or "mil" in norm_t
    ):
        t = re.sub(r",?\s*(?:por\s+|de\s+)?(?:r\$\s*)?\d+\s*mil(?:hao|hoes)?\b", "", t, flags=re.I).strip(" ,!?.:-")
        if "preco" not in norm_t and "valor" not in norm_t:
            t = f"preco metro quadrado {t}"
    return t or (text or "").strip()


def build_search_query(history, user_text):
    """Monta a consulta de pesquisa combinando o contexto recente quando a fala e curta."""
    q = _extract_search_keywords(user_text)
    if len(q.split()) <= 6 and history:
        prev_users = [_extract_search_keywords(m.get("content", "")) for m in history if m.get("role") == "user"]
        if prev_users:
            recent = " ".join(prev_users[-2:])
            q = f"{recent} {q}".strip()
    return q[:180]


def fetch_web_snippets(query, timeout=WEB_SEARCH_TIMEOUT, max_results=WEB_SEARCH_MAX_RESULTS):
    """Busca trechos atualizados no DuckDuckGo Lite (~0.4s) sem consumir cota do Groq."""
    if not WEB_SEARCH_ENABLED or not query or len(query.strip()) < 2:
        return ""
    try:
        url = "https://lite.duckduckgo.com/lite/"
        data = urllib.parse.urlencode({"q": query}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        titles = re.findall(r"<a rel=\"nofollow\" href=\"[^\"]+\" class='result-link'>(.*?)</a>", raw, re.S)
        snippets = re.findall(r"<td class='result-snippet'>(.*?)</td>", raw, re.S)
        out = []
        for idx, s in enumerate(snippets[:max_results]):
            t_clean = html.unescape(re.sub(r"<[^>]+>", "", titles[idx])).strip() if idx < len(titles) else ""
            s_clean = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()
            if s_clean:
                out.append(f"- {t_clean}: {s_clean}" if t_clean else f"- {s_clean}")
        return "\n".join(out)
    except Exception as e:
        logger.warning("Busca web DuckDuckGo falhou (%s): %s", query, e)
        return ""


# ----------------------------------------------------------------------------
# Cliente da IA (Groq + Orquestrador Gemini/Groq)
# ----------------------------------------------------------------------------
class AIError(Exception):
    pass


def _post_chat(model, messages, api_key, timeout):
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": MAX_TOKENS,
        "temperature": 0.5,
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


def ask_groq(history, user_text, deadline=None):
    """Enriquece a pergunta com pesquisa DuckDuckGo e chama a cascata do Groq."""
    api_key = _load_api_key()
    if not api_key:
        raise AIError("NO_KEY")

    deadline = deadline or (time.monotonic() + TOTAL_BUDGET_SECONDS)

    search_query = build_search_query(history, user_text)
    web_snippets = ""
    if WEB_SEARCH_ENABLED and (deadline - time.monotonic()) > 2.8:
        web_snippets = fetch_web_snippets(
            search_query, timeout=min(WEB_SEARCH_TIMEOUT, deadline - time.monotonic() - 1.5)
        )

    augmented_user = user_text
    if web_snippets:
        augmented_user = (
            f"Fala do usuario (pode ter tido a primeira palavra cortada pela Alexa): \"{user_text}\"\n\n"
            f"[Resultados de pesquisa na web para \"{search_query}\":\n{web_snippets}]"
        )

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [
        {"role": "user", "content": augmented_user}
    ]

    candidates = [m for m in MODELS if not _in_cooldown(m)] + [m for m in MODELS if _in_cooldown(m)]

    last_err = None
    timed_out = False
    for model in candidates:
        remaining = deadline - time.monotonic()
        if remaining < 0.7:
            break
        try:
            ans = _post_chat(model, messages, api_key, timeout=min(remaining, 5.5))
            global _last_engine_used
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
    raise AIError("TIMEOUT" if timed_out else f"FAILED: {last_err}")


def ask_ai(history, user_text, **kwargs):
    """Orquestra a chamada usando o provedor escolhido ('gemini' ou 'groq') com fallback automatico."""
    global _last_engine_used
    provider = kwargs.get("provider") or DEFAULT_PROVIDER
    deadline = kwargs.get("deadline") or (time.monotonic() + TOTAL_BUDGET_SECONDS)

    if provider == "gemini":
        try:
            gem_timeout = min(GEMINI_BUDGET_SECONDS, max(2.0, deadline - time.monotonic() - 1.4))
            ans = ask_gemini(history, user_text, timeout=gem_timeout)
            _last_engine_used = "Google Gemini"
            return ans
        except Exception as e:
            logger.warning("Gemini falhou/demorou (%s); fazendo fallback automatico para Groq.", e)
            return ask_groq(history, user_text, deadline=deadline)
    else:
        try:
            return ask_groq(history, user_text, deadline=deadline)
        except AIError as e:
            if str(e) != "NO_KEY" and (deadline - time.monotonic()) > 3.5:
                try:
                    ans = ask_gemini(history, user_text, timeout=deadline - time.monotonic())
                    _last_engine_used = "Google Gemini (fallback)"
                    return ans
                except Exception:
                    pass
            raise


# ----------------------------------------------------------------------------
# Memoria e Preferencias no DynamoDB
# ----------------------------------------------------------------------------
def get_user_state(handler_input):
    attrs = handler_input.attributes_manager.persistent_attributes
    history = list(attrs.get("history", []))
    provider = attrs.get("provider") or DEFAULT_PROVIDER
    return history, provider


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
    chosen, total = [], 0
    for msg in reversed(history):
        total += len(msg.get("content", ""))
        if total > CONTEXT_CHAR_BUDGET and chosen:
            break
        chosen.append(msg)
    chosen.reverse()
    while chosen and chosen[0].get("role") != "user":
        chosen.pop(0)
    return chosen


def save_user_state(handler_input, history=None, provider=None):
    mgr = handler_input.attributes_manager
    attrs = mgr.persistent_attributes
    if history is not None:
        attrs["history"] = trim_for_storage(history)
    if provider is not None:
        attrs["provider"] = provider
    mgr.persistent_attributes = attrs
    mgr.save_persistent_attributes()


def get_history(handler_input):
    return get_user_state(handler_input)[0]


def save_history(handler_input, history):
    save_user_state(handler_input, history=history)


def clear_history(handler_input):
    save_user_state(handler_input, history=[])


# ----------------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------------
def _ask(handler_input, speech, reprompt=MSG_REPROMPT):
    return handler_input.response_builder.speak(speech).ask(reprompt).response


def _converse(handler_input, text):
    """Fluxo principal: processa uma fala do usuario."""
    text = (text or "").strip()
    if not text:
        return _ask(handler_input, "Não entendi. Pode repetir?")

    norm = _normalize(text)
    if norm in STOP_WORDS:
        return handler_input.response_builder.speak(MSG_BYE).set_should_end_session(True).response
    if norm in HELP_WORDS:
        return _ask(handler_input, MSG_HELP)
    if is_reset_command(text):
        clear_history(handler_input)
        return _ask(handler_input, MSG_RESET)

    # Troca de provedor por comando de voz ("usar gemini" / "usar groq")
    new_provider = detect_provider_switch(text)
    if new_provider:
        save_user_state(handler_input, provider=new_provider)
        if new_provider == "gemini":
            warmup_gemini(max_wait=5.5)
            return _ask(handler_input, "Pronto! Mudei o cérebro principal para o Google Gemini. Pode perguntar!")
        return _ask(handler_input, "Pronto! Mudei o cérebro principal para o Groq. Pode perguntar!")

    history, provider = get_user_state(handler_input)

    if is_status_command(text):
        nome = "Google Gemini" if provider == "gemini" else "Groq"
        detalhe = f" A última resposta foi gerada por {_last_engine_used}." if _last_engine_used else ""
        return _ask(
            handler_input,
            f"O cérebro principal configurado é o {nome}.{detalhe} Para trocar, diga: usar gemini, ou usar groq.",
        )

    context = select_context(history)
    try:
        answer = ask_ai(context, text, provider=provider)
    except AIError as e:
        logger.error("Falha na IA: %s", e)
        msg = MSG_NO_KEY if str(e) == "NO_KEY" else (MSG_TIMEOUT if str(e) == "TIMEOUT" else MSG_ERROR)
        return _ask(handler_input, msg)

    history += [
        {"role": "user", "content": text},
        {"role": "assistant", "content": answer},
    ]
    save_user_state(handler_input, history=history)
    return _ask(handler_input, clean_for_speech(answer))


class LaunchRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        _, provider = get_user_state(handler_input)
        if provider == "gemini":
            warmup_gemini(max_wait=5.8)
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
sb.add_request_handler(ChatIntentHandler())
sb.add_request_handler(CatchAllIntentHandler())
sb.add_request_handler(ResetHistoryIntentHandler())
sb.add_request_handler(HelpIntentHandler())
sb.add_request_handler(CancelOrStopIntentHandler())
sb.add_request_handler(FallbackIntentHandler())
sb.add_request_handler(SessionEndedRequestHandler())
sb.add_exception_handler(CatchAllExceptionHandler())

lambda_handler = sb.lambda_handler()
