import streamlit as st
import gspread
from gspread.exceptions import APIError, WorksheetNotFound
from google.oauth2.service_account import Credentials
import pandas as pd
from datetime import datetime, time, timedelta
import pytz
from fpdf import FPDF
import urllib.parse
import time as time_module
import random
import re
import threading
import logging

# ==========================================================
# CONFIGURAÇÃO DE ACESSO
# ==========================================================
scope = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

SPREADSHEET_NAME = "ListaPresenca"
WS_USUARIOS = "Usuarios"
WS_CONFIG = "Config"
WS_HISTORICO = "Historico"

# Capacidade padrão usada quando a Config ainda não tiver valor definido pelo ADM.
CAPACIDADE_PADRAO_ONIBUS = 38

# Campo criado automaticamente na aba Usuarios para marcar quem possui prioridade de embarque.
PRIORIDADE_HEADER = "PRIORIDADE_LISTA"

# Campo criado automaticamente na aba Usuarios para liberar acesso ao painel ADM com login próprio.
# O acesso mestre @/@ continua existindo e é o único que pode alterar esta permissão.
ADMIN_HEADER = "ACESSO_ADM"

# Valores permitidos nos campos editáveis do cadastro dos usuários.
GRADUACOES_VALIDAS = [
    "TCEL", "MAJ", "CAP", "1º TEN", "2º TEN", "SUBTEN",
    "1º SGT", "2º SGT", "3º SGT", "CB", "SD", "FC COM", "FC TER"
]
ORIGENS_VALIDAS = ["QG", "RMCF", "OUTROS"]

FUSO_BR = pytz.timezone("America/Sao_Paulo")

# ==========================================================
# GIF NO FINAL DA PÁGINA (alteração solicitada)
# ==========================================================
GIF_URL = "https://www.imagensanimadas.com/data/media/425/onibus-imagem-animada-0024.gif"

# ==========================================================
# LOGS E LIMITES DE SEGURANÇA
# ==========================================================
LOGGER = logging.getLogger("rota_nova_iguacu")
if not LOGGER.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    LOGGER.addHandler(_handler)
LOGGER.setLevel(logging.INFO)

HISTORICO_MARGEM_LINHAS = 1000
TROCA_CICLO_COOLDOWN_ERRO = 20.0
TROCA_CICLO_TEMPO_MAXIMO = 90.0
LOCK_MUTACAO_TIMEOUT = 0.25


# ==========================================================
# TELEFONE:
# ==========================================================
def tel_only_digits(s: str) -> str:
    return re.sub(r"\D+", "", str(s or ""))

def tel_format_br(digits: str) -> str:
    """
    Formata 11 dígitos como: (xx) xxxxx.xxxx
    Se tiver menos, retorna o que der sem quebrar.
    """
    d = tel_only_digits(digits)
    if len(d) >= 2:
        ddd = d[:2]
        rest = d[2:]
    else:
        return d

    if len(rest) >= 9:
        p1 = rest[:5]
        p2 = rest[5:9]
        return f"({ddd}) {p1}.{p2}"
    elif len(rest) > 5:
        p1 = rest[:5]
        p2 = rest[5:]
        return f"({ddd}) {p1}.{p2}"
    else:
        return f"({ddd}) {rest}"

def tel_is_valid_11(s: str) -> bool:
    return len(tel_only_digits(s)) == 11


# ==========================================================
# COORDENAÇÃO ENTRE AS SESSÕES DO STREAMLIT
# ==========================================================
class CoordenadorTrocaCiclo:
    """
    Evita que dezenas de sessões tentem arquivar o mesmo ciclo ao mesmo tempo.

    O lock interno é usado apenas para alterar o estado em memória e nunca fica
    preso durante chamadas ao Google Sheets. Assim, quando uma sessão está
    processando a troca, as demais recebem resposta imediata em vez de ficarem
    acumuladas e travarem o processo do Streamlit.
    """

    def __init__(self):
        self._guard = threading.Lock()
        self.em_andamento = False
        self.inicio_monotonic = 0.0
        self.tentar_novamente_apos = 0.0
        self.ultimo_erro = ""

    def tentar_iniciar(self):
        agora = time_module.monotonic()
        with self._guard:
            # Recupera automaticamente um estado abandonado por exceção severa.
            if self.em_andamento and (agora - self.inicio_monotonic) > TROCA_CICLO_TEMPO_MAXIMO:
                LOGGER.warning("Liberando troca de ciclo considerada abandonada.")
                self.em_andamento = False

            if self.em_andamento:
                return False, "EM_ANDAMENTO", 0

            if agora < self.tentar_novamente_apos:
                restante = max(1, int(self.tentar_novamente_apos - agora) + 1)
                return False, "COOLDOWN", restante

            self.em_andamento = True
            self.inicio_monotonic = agora
            return True, "INICIADA", 0

    def finalizar(self, sucesso: bool, erro: str = ""):
        agora = time_module.monotonic()
        with self._guard:
            self.em_andamento = False
            self.inicio_monotonic = 0.0
            self.ultimo_erro = str(erro or "")
            self.tentar_novamente_apos = 0.0 if sucesso else agora + TROCA_CICLO_COOLDOWN_ERRO

    def obter_status(self):
        agora = time_module.monotonic()
        with self._guard:
            if self.em_andamento and (agora - self.inicio_monotonic) <= TROCA_CICLO_TEMPO_MAXIMO:
                return "EM_ANDAMENTO", 0
            if agora < self.tentar_novamente_apos:
                restante = max(1, int(self.tentar_novamente_apos - agora) + 1)
                return "COOLDOWN", restante
            return "LIVRE", 0


@st.cache_resource
def coordenador_troca_ciclo():
    return CoordenadorTrocaCiclo()


@st.cache_resource
def lock_mutacoes_presenca():
    # Serializa somente as mutações efetivas da planilha. Quem não consegue
    # adquirir rapidamente recebe uma mensagem e não fica preso em fila.
    return threading.RLock()


@st.cache_resource
def lock_estrutura_planilhas():
    # Evita criação concorrente de abas/colunas durante a inicialização.
    return threading.RLock()


def adquirir_lock_mutacao(timeout=LOCK_MUTACAO_TIMEOUT):
    lock = lock_mutacoes_presenca()
    adquirido = lock.acquire(timeout=timeout)
    return lock, adquirido


# ==========================================================
# WRAPPER COM RETRY / BACKOFF PARA 429 E ERROS TEMPORÁRIOS
# ==========================================================
def gs_call(
    func,
    *args,
    _max_tries=5,
    _base=0.5,
    _max_sleep=4.0,
    **kwargs
):
    """
    Executa uma chamada ao Google Sheets com backoff para 429/5xx.

    Os parâmetros iniciados por underscore pertencem ao wrapper e não são
    enviados à função do gspread. Em operações que seguram lock, o chamador
    pode usar menos tentativas para evitar retenção prolongada.
    """
    max_tries = max(1, int(_max_tries))
    last_error = None

    for attempt in range(max_tries):
        try:
            return func(*args, **kwargs)
        except APIError as e:
            last_error = e
            msg = str(e)
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            is_429 = (
                status_code == 429
                or "429" in msg
                or "Quota exceeded" in msg
                or "RESOURCE_EXHAUSTED" in msg
            )
            is_5xx = (
                status_code in {500, 502, 503, 504}
                or any(code in msg for code in ["500", "502", "503", "504"])
            )

            if not (is_429 or is_5xx):
                raise

            if attempt >= max_tries - 1:
                break

            sleep_s = (_base * (2 ** attempt)) + random.uniform(0.0, 0.25)
            time_module.sleep(min(sleep_s, _max_sleep))

    raise RuntimeError(
        "Google Sheets temporariamente indisponível ou com excesso de requisições. "
        "Aguarde alguns segundos e tente novamente."
    ) from last_error


# ==========================================================
# CONEXÕES (CACHE_RESOURCE)
# ==========================================================
@st.cache_resource
def conectar_gsheets():
    info = dict(st.secrets["gcp_service_account"])

    # O Streamlit Secrets às vezes guarda a chave com "\\n" literal
    pk = info.get("private_key")
    if isinstance(pk, str):
        info["private_key"] = pk.replace("\\n", "\n")

    creds = Credentials.from_service_account_info(info, scopes=scope)
    return gspread.authorize(creds)

@st.cache_resource
def abrir_documento():
    client = conectar_gsheets()
    return gs_call(client.open, SPREADSHEET_NAME)

@st.cache_resource
def ws_usuarios():
    doc = abrir_documento()
    return gs_call(doc.worksheet, WS_USUARIOS)

@st.cache_resource
def ws_presenca():
    doc = abrir_documento()
    return doc.sheet1

@st.cache_resource
def ws_config():
    doc = abrir_documento()

    with lock_estrutura_planilhas():
        try:
            sheet_c = gs_call(doc.worksheet, WS_CONFIG)
        except WorksheetNotFound:
            # Confere novamente dentro do lock antes de criar.
            try:
                sheet_c = gs_call(doc.worksheet, WS_CONFIG)
            except WorksheetNotFound:
                sheet_c = gs_call(doc.add_worksheet, title=WS_CONFIG, rows="10", cols="5")
                gs_call(
                    sheet_c.update,
                    "A1:B2",
                    [["LIMITE", "CAPACIDADE_ONIBUS"], ["100", str(CAPACIDADE_PADRAO_ONIBUS)]]
                )

        # Garante a capacidade apenas uma vez por inicialização do app.
        cabecalho = gs_call(sheet_c.row_values, 1)
        b1 = str(cabecalho[1]).strip() if len(cabecalho) > 1 else ""
        if b1 != "CAPACIDADE_ONIBUS":
            gs_call(
                sheet_c.update,
                "B1:B2",
                [["CAPACIDADE_ONIBUS"], [str(CAPACIDADE_PADRAO_ONIBUS)]]
            )

        return sheet_c


@st.cache_resource
def ws_historico():
    """
    Aba onde as listas encerradas serão arquivadas antes da limpeza do ciclo.
    Se a aba não existir, ela será criada automaticamente.
    """
    doc = abrir_documento()

    with lock_estrutura_planilhas():
        try:
            sheet_h = gs_call(doc.worksheet, WS_HISTORICO)
        except WorksheetNotFound:
            # Confere novamente dentro do lock antes de criar.
            try:
                sheet_h = gs_call(doc.worksheet, WS_HISTORICO)
            except WorksheetNotFound:
                sheet_h = gs_call(
                    doc.add_worksheet,
                    title=WS_HISTORICO,
                    rows=str(HISTORICO_MARGEM_LINHAS),
                    cols=str(len(HIST_HEADERS))
                )
                gs_call(sheet_h.update, "A1", [HIST_HEADERS])

        headers = [str(h).strip() for h in gs_call(sheet_h.row_values, 1)]
        if headers[:len(HIST_HEADERS)] != HIST_HEADERS:
            gs_call(sheet_h.update, "A1", [HIST_HEADERS])

        return sheet_h


# ==========================================================
# SENHA TEMPORÁRIA (1 acesso) - RECUPERAÇÃO SEGURA
# ==========================================================
TEMP_HEADERS = ["TEMP_SENHA", "TEMP_EXPIRA", "TEMP_USADA"]

# Cabeçalho da aba Historico, criada automaticamente no Google Sheets
HIST_HEADERS = [
    "CICLO_ID", "DATA_CICLO", "EMBARQUE", "ARQUIVADO_EM",
    "DATA_HORA", "QG_RMCF_OUTROS", "GRADUAÇÃO", "NOME", "LOTAÇÃO", "EMAIL",
    "CAPACIDADE_ONIBUS", PRIORIDADE_HEADER
]


def obter_ws_historico_fresco():
    """
    Obtém um objeto Worksheet novo, com metadados atuais da grade.

    Isso é importante porque a aba pode ter sido redimensionada manualmente
    enquanto o app estava no ar, e um objeto guardado em cache pode continuar
    com row_count/col_count antigos até um reboot.
    """
    doc = abrir_documento()
    try:
        sheet_h = gs_call(doc.worksheet, WS_HISTORICO, _max_tries=3)
    except WorksheetNotFound:
        # A criação continua protegida contra duas sessões simultâneas.
        with lock_estrutura_planilhas():
            try:
                sheet_h = gs_call(doc.worksheet, WS_HISTORICO, _max_tries=3)
            except WorksheetNotFound:
                sheet_h = gs_call(
                    doc.add_worksheet,
                    title=WS_HISTORICO,
                    rows=str(HISTORICO_MARGEM_LINHAS),
                    cols=str(len(HIST_HEADERS)),
                    _max_tries=3
                )

    headers = [str(h).strip() for h in gs_call(sheet_h.row_values, 1, _max_tries=3)]
    if headers[:len(HIST_HEADERS)] != HIST_HEADERS:
        gs_call(sheet_h.update, "A1", [HIST_HEADERS], _max_tries=3)

    return sheet_h


def garantir_espaco_historico(sheet_h, linha_inicial: int, qtd_linhas: int):
    """Expande automaticamente a grade antes da gravação do histórico."""
    qtd_linhas = max(0, int(qtd_linhas))
    if qtd_linhas == 0:
        return

    ultima_linha_necessaria = linha_inicial + qtd_linhas - 1
    linhas_atuais = int(getattr(sheet_h, "row_count", 0) or 0)
    colunas_atuais = int(getattr(sheet_h, "col_count", 0) or 0)

    linhas_alvo = linhas_atuais
    colunas_alvo = max(colunas_atuais, len(HIST_HEADERS))

    if linhas_atuais < ultima_linha_necessaria:
        # Acrescenta uma margem ampla para que a expansão não precise ocorrer
        # em toda troca de ciclo.
        linhas_alvo = max(
            ultima_linha_necessaria + HISTORICO_MARGEM_LINHAS,
            linhas_atuais + HISTORICO_MARGEM_LINHAS,
            HISTORICO_MARGEM_LINHAS
        )

    if linhas_alvo != linhas_atuais or colunas_alvo != colunas_atuais:
        LOGGER.info(
            "Expandindo aba Historico: %s x %s -> %s x %s",
            linhas_atuais, colunas_atuais, linhas_alvo, colunas_alvo
        )
        gs_call(
            sheet_h.resize,
            rows=linhas_alvo,
            cols=colunas_alvo,
            _max_tries=3,
            _max_sleep=2.0
        )


def validar_bloco_historico(sheet_h, linha_inicial: int, qtd_linhas: int, ciclo_id: str):
    """Confirma que todas as linhas foram realmente gravadas antes da limpeza."""
    linha_final = linha_inicial + qtd_linhas - 1
    valores = gs_call(
        sheet_h.get,
        f"A{linha_inicial}:L{linha_final}",
        _max_tries=3,
        _max_sleep=2.0
    )

    if len(valores) != qtd_linhas:
        return False

    for linha in valores:
        if not linha or str(linha[0]).strip() != ciclo_id:
            return False

    return True

def _br_now():
    return datetime.now(FUSO_BR)

def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%d/%m/%Y %H:%M:%S")

def _parse_dt(s: str):
    try:
        return FUSO_BR.localize(datetime.strptime(str(s).strip(), "%d/%m/%Y %H:%M:%S"))
    except Exception:
        return None

def gerar_senha_temp(tam: int = 10) -> str:
    # Evita caracteres ambíguos
    alfabeto = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choice(alfabeto) for _ in range(tam))

def ensure_temp_cols(sheet_u):
    """
    Garante colunas TEMP_* na planilha Usuarios:
    TEMP_SENHA | TEMP_EXPIRA | TEMP_USADA
    """
    headers = gs_call(sheet_u.row_values, 1)
    headers = [str(h).strip() for h in headers if str(h).strip() != ""]
    missing = [h for h in TEMP_HEADERS if h not in headers]
    if not missing:
        return {h: headers.index(h) + 1 for h in TEMP_HEADERS}

    new_headers = headers + missing
    try:
        gs_call(sheet_u.update, "A1", [new_headers])
    except Exception:
        gs_call(sheet_u.update, "A1", [new_headers])

    rows = gs_call(sheet_u.get_all_values)
    n_rows = len(rows)
    if n_rows >= 2:
        for h in missing:
            col_idx = new_headers.index(h) + 1
            # bloqueia tokens antigos
            vals = [["SIM"]] * (n_rows - 1) if h == "TEMP_USADA" else [[""]] * (n_rows - 1)
            col_letter = gspread.utils.rowcol_to_a1(1, col_idx).rstrip("1")
            rng_col = f"{col_letter}2:{col_letter}{n_rows}"
            gs_call(sheet_u.update, rng_col, vals)

    return {h: new_headers.index(h) + 1 for h in TEMP_HEADERS}


def ensure_prioridade_col(sheet_u):
    """
    Garante a coluna PRIORIDADE_LISTA na aba Usuarios.
    Valores esperados: SIM ou NAO.
    """
    headers = gs_call(sheet_u.row_values, 1)
    headers = [str(h).strip() for h in headers if str(h).strip() != ""]

    if PRIORIDADE_HEADER in headers:
        return headers.index(PRIORIDADE_HEADER) + 1

    new_headers = headers + [PRIORIDADE_HEADER]
    gs_call(sheet_u.update, "A1", [new_headers])

    rows = gs_call(sheet_u.get_all_values)
    n_rows = len(rows)
    if n_rows >= 2:
        col_idx = new_headers.index(PRIORIDADE_HEADER) + 1
        col_letter = gspread.utils.rowcol_to_a1(1, col_idx).rstrip("1")
        rng_col = f"{col_letter}2:{col_letter}{n_rows}"
        gs_call(sheet_u.update, rng_col, [["NAO"]] * (n_rows - 1))

    return new_headers.index(PRIORIDADE_HEADER) + 1


def prioridade_ativa(valor) -> bool:
    """Converte o valor salvo na planilha em booleano."""
    return str(valor or "").strip().upper() in {"SIM", "S", "TRUE", "VERDADEIRO", "1", "YES", "Y"}


def ensure_admin_col(sheet_u):
    """
    Garante a coluna ACESSO_ADM na aba Usuarios.
    Valores esperados: SIM ou NAO.
    Somente o ADM mestre @/@ deve alterar essa coluna pela interface.
    """
    headers = gs_call(sheet_u.row_values, 1)
    headers = [str(h).strip() for h in headers if str(h).strip() != ""]

    if ADMIN_HEADER in headers:
        return headers.index(ADMIN_HEADER) + 1

    new_headers = headers + [ADMIN_HEADER]
    gs_call(sheet_u.update, "A1", [new_headers])

    rows = gs_call(sheet_u.get_all_values)
    n_rows = len(rows)
    if n_rows >= 2:
        col_idx = new_headers.index(ADMIN_HEADER) + 1
        col_letter = gspread.utils.rowcol_to_a1(1, col_idx).rstrip("1")
        rng_col = f"{col_letter}2:{col_letter}{n_rows}"
        gs_call(sheet_u.update, rng_col, [["NAO"]] * (n_rows - 1))

    return new_headers.index(ADMIN_HEADER) + 1


def admin_acesso_ativo(valor) -> bool:
    """Converte o valor salvo na planilha em permissão de acesso ADM."""
    return str(valor or "").strip().upper() in {"SIM", "S", "TRUE", "VERDADEIRO", "1", "YES", "Y"}


@st.cache_resource
def inicializar_estrutura_usuarios():
    """
    Garante todas as colunas auxiliares da aba Usuarios uma única vez por
    inicialização do app. Antes, três leituras eram feitas em toda execução
    de toda sessão, aumentando muito o consumo da API nos horários de pico.
    """
    with lock_estrutura_planilhas():
        sheet_u = ws_usuarios()
        headers = [str(h).strip() for h in gs_call(sheet_u.row_values, 1)]

        obrigatorias = TEMP_HEADERS + [PRIORIDADE_HEADER, ADMIN_HEADER]
        missing = [h for h in obrigatorias if h not in headers]
        if not missing:
            return True

        new_headers = headers + missing
        gs_call(sheet_u.update, "A1", [new_headers])

        rows = gs_call(sheet_u.get_all_values)
        n_rows = len(rows)
        if n_rows >= 2:
            defaults = {
                "TEMP_SENHA": "",
                "TEMP_EXPIRA": "",
                "TEMP_USADA": "SIM",
                PRIORIDADE_HEADER: "NAO",
                ADMIN_HEADER: "NAO",
            }

            for header in missing:
                col_idx = new_headers.index(header) + 1
                col_letter = gspread.utils.rowcol_to_a1(1, col_idx).rstrip("1")
                rng_col = f"{col_letter}2:{col_letter}{n_rows}"
                gs_call(sheet_u.update, rng_col, [[defaults[header]]] * (n_rows - 1))

        return True


def obter_emails_prioridade(records_u) -> set:
    """Retorna os e-mails dos usuários marcados com prioridade de embarque."""
    emails = set()
    for u in records_u or []:
        if prioridade_ativa(u.get(PRIORIDADE_HEADER, "")):
            email = str(u.get("Email", "") or u.get("EMAIL", "")).strip().lower()
            if email:
                emails.add(email)
    return emails


def colunas_para_exibir(df: pd.DataFrame) -> pd.DataFrame:
    """Remove colunas internas antes de mostrar a tabela na tela."""
    ocultas = [c for c in ["EMAIL", "_PRIORIDADE_LISTA"] if c in df.columns]
    return df.drop(columns=ocultas)


def find_user_row_by_email_tel(sheet_u, email: str, tel_digits: str):
    email = str(email or "").strip().lower()
    tel_digits = tel_only_digits(tel_digits)

    rows = gs_call(sheet_u.get_all_values)
    if not rows or len(rows) < 2:
        return None, None

    headers = [str(h).strip() for h in rows[0]]
    if "Email" in headers:
        i_email = headers.index("Email")
    elif "EMAIL" in headers:
        i_email = headers.index("EMAIL")
    else:
        return None, None

    if "TELEFONE" not in headers:
        return None, None
    i_tel = headers.index("TELEFONE")

    for idx in range(1, len(rows)):
        r = rows[idx] + [""] * (len(headers) - len(rows[idx]))
        em = str(r[i_email]).strip().lower()
        te = tel_only_digits(r[i_tel])
        if em == email and te == tel_digits:
            d = {headers[j]: (r[j] if j < len(r) else "") for j in range(len(headers))}
            return idx + 1, d
    return None, None


def atualizar_dados_usuario_admin(
    sheet_u,
    sheet_p,
    email: str,
    nova_graduacao: str,
    novo_nome: str,
    nova_lotacao: str,
    nova_origem: str,
):
    """
    Permite que o ADM mestre ou autorizado altere Graduação, Nome, Lotação e
    Origem do usuário na aba Usuarios.

    Se o usuário já estiver inscrito no ciclo atual, os mesmos dados também são
    sincronizados na lista de presença para que a exibição, a ordenação e o PDF
    sejam atualizados imediatamente.
    """
    email_norm = str(email or "").strip().lower()
    graduacao_norm = str(nova_graduacao or "").strip().upper()
    nome_norm = str(novo_nome or "").strip()
    lotacao_norm = str(nova_lotacao or "").strip()
    origem_norm = str(nova_origem or "").strip().upper()

    if not email_norm:
        return False, "EMAIL_INVALIDO", 0
    if graduacao_norm not in GRADUACOES_VALIDAS:
        return False, "GRADUACAO_INVALIDA", 0
    if not nome_norm:
        return False, "NOME_INVALIDO", 0
    if not lotacao_norm:
        return False, "LOTACAO_INVALIDA", 0
    if origem_norm not in ORIGENS_VALIDAS:
        return False, "ORIGEM_INVALIDA", 0

    lock, adquirido = adquirir_lock_mutacao()
    if not adquirido:
        return False, "SISTEMA_OCUPADO", 0

    linha_usuario = None
    colunas_u = {}
    valores_anteriores_u = {}
    presencas_atualizadas = []

    try:
        status_coord, _ = coordenador_troca_ciclo().obter_status()
        if status_coord == "EM_ANDAMENTO":
            return False, "TROCA_EM_ANDAMENTO", 0

        dados_u = gs_call(
            sheet_u.get_all_values,
            _max_tries=3,
            _max_sleep=1.5
        )
        if not dados_u or len(dados_u) < 2:
            return False, "USUARIO_NAO_ENCONTRADO", 0

        headers_u = [str(h).strip() for h in dados_u[0]]
        headers_u_upper = [h.upper() for h in headers_u]

        def localizar_coluna(*candidatos):
            for candidato in candidatos:
                candidato_upper = str(candidato).upper()
                if candidato_upper in headers_u_upper:
                    return headers_u_upper.index(candidato_upper)
            return None

        email_col_zero = localizar_coluna("EMAIL")
        colunas_u = {
            "GRADUACAO": localizar_coluna("GRADUAÇÃO", "GRADUACAO"),
            "NOME": localizar_coluna("NOME"),
            "LOTACAO": localizar_coluna("LOTAÇÃO", "LOTACAO"),
            "ORIGEM": localizar_coluna("QG_RMCF_OUTROS", "ORIGEM"),
        }

        if email_col_zero is None:
            return False, "COLUNA_EMAIL_NAO_ENCONTRADA", 0
        if any(indice is None for indice in colunas_u.values()):
            return False, "COLUNAS_CADASTRO_NAO_ENCONTRADAS", 0

        linha_encontrada = None
        linha_valores = None
        for idx, row in enumerate(dados_u[1:], start=2):
            r = list(row) + [""] * (len(headers_u) - len(row))
            if str(r[email_col_zero]).strip().lower() == email_norm:
                linha_encontrada = idx
                linha_valores = r
                break

        if linha_encontrada is None or linha_valores is None:
            return False, "USUARIO_NAO_ENCONTRADO", 0

        linha_usuario = linha_encontrada
        novos_valores_u = {
            "GRADUACAO": graduacao_norm,
            "NOME": nome_norm,
            "LOTACAO": lotacao_norm,
            "ORIGEM": origem_norm,
        }
        valores_anteriores_u = {
            campo: str(linha_valores[coluna] or "").strip()
            for campo, coluna in colunas_u.items()
        }

        # Atualiza somente os campos efetivamente modificados.
        for campo, novo_valor in novos_valores_u.items():
            valor_anterior = valores_anteriores_u[campo]
            comparacao_anterior = valor_anterior.upper() if campo in {"GRADUACAO", "ORIGEM"} else valor_anterior
            comparacao_nova = novo_valor.upper() if campo in {"GRADUACAO", "ORIGEM"} else novo_valor
            if comparacao_anterior != comparacao_nova:
                gs_call(
                    sheet_u.update_cell,
                    linha_usuario,
                    colunas_u[campo] + 1,
                    novo_valor,
                    _max_tries=3,
                    _max_sleep=1.5
                )

        # Sincroniza B:E da lista atual: Origem, Graduação, Nome e Lotação.
        dados_p = gs_call(
            sheet_p.get_all_values,
            _max_tries=2,
            _max_sleep=1.0
        )

        for numero_linha, row in enumerate((dados_p or [])[1:], start=2):
            r = list(row) + [""] * 6
            if str(r[5]).strip().lower() == email_norm:
                valores_anteriores_presenca = [
                    str(r[1] or "").strip(),
                    str(r[2] or "").strip(),
                    str(r[3] or "").strip(),
                    str(r[4] or "").strip(),
                ]
                novos_valores_presenca = [
                    origem_norm,
                    graduacao_norm,
                    nome_norm,
                    lotacao_norm,
                ]

                if valores_anteriores_presenca != novos_valores_presenca:
                    gs_call(
                        sheet_p.update,
                        f"B{numero_linha}:E{numero_linha}",
                        [novos_valores_presenca],
                        value_input_option="USER_ENTERED",
                        _max_tries=2,
                        _max_sleep=1.0
                    )
                    presencas_atualizadas.append((numero_linha, valores_anteriores_presenca))

        # Confirma todos os campos no cadastro do usuário.
        dados_confirmacao_u = gs_call(
            sheet_u.row_values,
            linha_usuario,
            _max_tries=2,
            _max_sleep=1.0
        )
        dados_confirmacao_u = list(dados_confirmacao_u) + [""] * (
            len(headers_u) - len(dados_confirmacao_u)
        )

        for campo, novo_valor in novos_valores_u.items():
            valor_confirmado = str(dados_confirmacao_u[colunas_u[campo]] or "").strip()
            if campo in {"GRADUACAO", "ORIGEM"}:
                confirmado_ok = valor_confirmado.upper() == novo_valor.upper()
            else:
                confirmado_ok = valor_confirmado == novo_valor
            if not confirmado_ok:
                raise RuntimeError(f"A atualização do campo {campo} não foi confirmada na aba Usuarios.")

        # Confirma também as linhas eventualmente alteradas na presença atual.
        if presencas_atualizadas:
            dados_p_confirmacao = gs_call(
                sheet_p.get_all_values,
                _max_tries=2,
                _max_sleep=1.0
            )
            esperados_presenca = [origem_norm, graduacao_norm, nome_norm, lotacao_norm]
            for row in (dados_p_confirmacao or [])[1:]:
                r = list(row) + [""] * 6
                if str(r[5]).strip().lower() == email_norm:
                    confirmados_presenca = [str(r[pos] or "").strip() for pos in range(1, 5)]
                    if confirmados_presenca != esperados_presenca:
                        raise RuntimeError("Os novos dados não foram confirmados na lista de presença atual.")

        buscar_usuarios_admin.clear()
        buscar_usuarios_cadastrados.clear()
        buscar_presenca_atualizada.clear()

        return True, "ATUALIZADOS", len(presencas_atualizadas)

    except Exception:
        LOGGER.exception("Falha ao alterar os dados do usuário pelo painel ADM.")

        # Tenta restaurar o cadastro caso uma etapa posterior tenha falhado.
        if linha_usuario is not None and colunas_u and valores_anteriores_u:
            for campo, valor_anterior in valores_anteriores_u.items():
                try:
                    gs_call(
                        sheet_u.update_cell,
                        linha_usuario,
                        colunas_u[campo] + 1,
                        valor_anterior,
                        _max_tries=1
                    )
                except Exception:
                    LOGGER.exception("Não foi possível reverter o campo %s na aba Usuarios.", campo)

        # Restaura as linhas da lista atual que já haviam sido alteradas.
        for numero_linha, valores_anteriores in presencas_atualizadas:
            try:
                gs_call(
                    sheet_p.update,
                    f"B{numero_linha}:E{numero_linha}",
                    [valores_anteriores],
                    value_input_option="USER_ENTERED",
                    _max_tries=1
                )
            except Exception:
                LOGGER.exception("Não foi possível reverter os dados na lista de presença.")

        buscar_usuarios_admin.clear()
        buscar_usuarios_cadastrados.clear()
        buscar_presenca_atualizada.clear()
        return False, "ERRO_ATUALIZACAO", len(presencas_atualizadas)

    finally:
        lock.release()


# ==========================================================
# LEITURAS (CACHE_DATA)
# ==========================================================
@st.cache_data(ttl=30)
def buscar_usuarios_cadastrados():
    """Uso geral (Login/Cadastro/Recuperar)."""
    # Não transforma falha de leitura em lista vazia, pois isso poderia
    # permitir cadastro duplicado ou negar login de usuário existente.
    sheet_u = ws_usuarios()
    return gs_call(sheet_u.get_all_records)


@st.cache_data(ttl=3)
def buscar_usuarios_admin():
    """Uso específico do ADM: mais fresco."""
    sheet_u = ws_usuarios()
    return gs_call(sheet_u.get_all_records)

def garantir_config_capacidade(sheet_c):
    """
    Garante que a aba Config tenha também o campo CAPACIDADE_ONIBUS.
    Mantém o limite de usuários no padrão atual:
    A1 = LIMITE | A2 = valor
    B1 = CAPACIDADE_ONIBUS | B2 = valor
    """
    try:
        cabecalho = gs_call(sheet_c.row_values, 1)
        b1 = str(cabecalho[1]).strip() if len(cabecalho) > 1 else ""
        if b1 != "CAPACIDADE_ONIBUS":
            gs_call(sheet_c.update, "B1:B2", [["CAPACIDADE_ONIBUS"], [str(CAPACIDADE_PADRAO_ONIBUS)]])
    except Exception:
        try:
            gs_call(sheet_c.update, "B1:B2", [["CAPACIDADE_ONIBUS"], [str(CAPACIDADE_PADRAO_ONIBUS)]])
        except Exception:
            pass


@st.cache_data(ttl=120)
def buscar_limite_dinamico():
    try:
        sheet_c = ws_config()
        val = gs_call(sheet_c.acell, "A2").value
        return int(val)
    except Exception:
        return 100


@st.cache_data(ttl=30)
def buscar_capacidade_onibus_dinamica():
    """
    Capacidade usada para definir quem fica numerado como vaga normal
    e quem passa a aparecer como Exc-xx.
    """
    try:
        sheet_c = ws_config()
        val = gs_call(sheet_c.acell, "B2").value
        capacidade = int(str(val).strip())
        return max(1, capacidade)
    except Exception:
        return CAPACIDADE_PADRAO_ONIBUS

@st.cache_data(ttl=6)
def buscar_presenca_atualizada():
    # Se a leitura falhar, interrompe a operação em vez de fingir que a lista
    # está vazia e permitir gravações ou exclusões incorretas.
    sheet_p = ws_presenca()
    return gs_call(sheet_p.get_all_values)

@st.cache_data(ttl=30)
def buscar_historico_dados():
    try:
        sheet_h = ws_historico()
        return gs_call(sheet_h.get_all_values)
    except Exception:
        return None


# ==========================================================
# FILTRO PARA NÃO EXIBIR LINHAS “LIXO” (evita final estranho)
# ==========================================================
def filtrar_linhas_presenca(dados_p):
    """
    Mantém somente linhas válidas para exibição/ordenação/conferência:
    - pelo menos 6 colunas (DATA, QG_RMCF_OUTROS, GRAD, NOME, LOTAÇÃO, EMAIL)
    - DATA, NOME e EMAIL preenchidos
    """
    if not dados_p or len(dados_p) < 2:
        return dados_p

    header = dados_p[0]
    body = dados_p[1:]

    def norm(x):
        return str(x).strip() if x is not None else ""

    body_ok = []
    for row in body:
        r = list(row) + [""] * (6 - len(row))
        r = r[:6]

        data_hora = norm(r[0])
        nome = norm(r[3])
        email = norm(r[5])

        if data_hora and nome and email:
            body_ok.append(r)

    return [header] + body_ok


# ==========================================================
# HISTÓRICO: arquiva a lista antes da limpeza automática
# ==========================================================
def ensure_historico_headers(sheet_h):
    """Garante o cabeçalho correto na aba Historico."""
    try:
        headers = gs_call(sheet_h.row_values, 1)
        headers = [str(h).strip() for h in headers]
        if headers[:len(HIST_HEADERS)] != HIST_HEADERS:
            gs_call(sheet_h.update, "A1", [HIST_HEADERS])
    except Exception:
        gs_call(sheet_h.update, "A1", [HIST_HEADERS])


def _parse_data_hora_presenca(valor):
    try:
        return FUSO_BR.localize(datetime.strptime(str(valor).strip(), "%d/%m/%Y %H:%M:%S"))
    except Exception:
        return None


def identificar_ciclo_da_lista(dados_p):
    """
    Descobre a qual ciclo a lista pertence a partir dos horários de inscrição.
    Mantém a mesma lógica exibida no topo do app:
    - inscrições após 19:00 => embarque 06:30 do dia seguinte;
    - inscrições antes de 07:00 => embarque 06:30 do mesmo dia;
    - demais horários => embarque 18:30 do mesmo dia.
    """
    datas = []
    for row in (dados_p or [])[1:]:
        if row:
            dt = _parse_data_hora_presenca(row[0])
            if dt:
                datas.append(dt)

    base = min(datas) if datas else datetime.now(FUSO_BR)
    t = base.time()

    if t >= time(19, 0):
        data_ciclo = (base + timedelta(days=1)).date()
        embarque = "06:30"
    elif t < time(7, 0):
        data_ciclo = base.date()
        embarque = "06:30"
    else:
        data_ciclo = base.date()
        embarque = "18:30"

    ciclo_id = f"{data_ciclo.strftime('%Y%m%d')}_{embarque.replace(':', '')}"
    return ciclo_id, data_ciclo.strftime("%d/%m/%Y"), embarque


def arquivar_lista_antes_de_limpar(dados_p):
    """
    Copia a lista atual para a aba Historico antes da limpeza.

    A grade é expandida previamente, a escrita ocorre em faixa explícita e a
    confirmação é feita por assinatura DATA_HORA + EMAIL. Se uma tentativa
    anterior tiver gravado apenas parte do ciclo, somente as linhas faltantes
    serão acrescentadas, sem sobrescrever outros históricos.
    """
    if not dados_p or len(dados_p) < 2:
        return True, "VAZIA"

    try:
        sheet_h = obter_ws_historico_fresco()

        ciclo_id, data_ciclo, embarque = identificar_ciclo_da_lista(dados_p)
        capacidade_onibus = buscar_capacidade_onibus_dinamica()
        prioridade_emails = obter_emails_prioridade(buscar_usuarios_cadastrados())

        arquivado_em = datetime.now(FUSO_BR).strftime("%d/%m/%Y %H:%M:%S")
        linhas = []
        for row in dados_p[1:]:
            r = list(row) + [""] * 6
            r = r[:6]
            email_linha = str(r[5] or "").strip().lower()
            linhas.append([
                ciclo_id,
                data_ciclo,
                embarque,
                arquivado_em,
                r[0],
                r[1],
                r[2],
                r[3],
                r[4],
                r[5],
                str(capacidade_onibus),
                "SIM" if email_linha in prioridade_emails else "NAO",
            ])

        if not linhas:
            return True, "VAZIA"

        def assinatura(linha):
            linha = list(linha) + [""] * 12
            return (
                str(linha[4]).strip(),
                str(linha[9]).strip().lower(),
            )

        # Esta leitura completa ocorre apenas na troca de ciclo (normalmente
        # duas vezes ao dia) e permite detectar/reparar gravação parcial.
        historico_atual = gs_call(
            sheet_h.get_all_values,
            _max_tries=3,
            _max_sleep=2.0
        )
        assinaturas_existentes = {
            assinatura(row)
            for row in (historico_atual or [])[1:]
            if row and str(row[0]).strip() == ciclo_id
        }

        linhas_faltantes = [
            linha for linha in linhas
            if assinatura(linha) not in assinaturas_existentes
        ]

        if not linhas_faltantes:
            LOGGER.info("Ciclo %s já estava integralmente arquivado.", ciclo_id)
            return True, "JA_ARQUIVADA"

        if assinaturas_existentes:
            LOGGER.warning(
                "Histórico parcial detectado no ciclo %s: %s de %s linha(s) já existiam.",
                ciclo_id, len(assinaturas_existentes), len(linhas)
            )

        linha_inicial = max(2, len(historico_atual or []) + 1)
        garantir_espaco_historico(sheet_h, linha_inicial, len(linhas_faltantes))
        linha_final = linha_inicial + len(linhas_faltantes) - 1
        faixa = f"A{linha_inicial}:L{linha_final}"

        gs_call(
            sheet_h.update,
            faixa,
            linhas_faltantes,
            value_input_option="USER_ENTERED",
            _max_tries=4,
            _max_sleep=2.0
        )

        if not validar_bloco_historico(
            sheet_h,
            linha_inicial,
            len(linhas_faltantes),
            ciclo_id
        ):
            raise RuntimeError(
                f"A nova gravação do ciclo {ciclo_id} não foi confirmada integralmente."
            )

        # Confirma que o conjunto final contém todos os passageiros esperados.
        historico_confirmacao = gs_call(
            sheet_h.get_all_values,
            _max_tries=3,
            _max_sleep=2.0
        )
        assinaturas_confirmadas = {
            assinatura(row)
            for row in (historico_confirmacao or [])[1:]
            if row and str(row[0]).strip() == ciclo_id
        }
        assinaturas_esperadas = {assinatura(row) for row in linhas}

        if not assinaturas_esperadas.issubset(assinaturas_confirmadas):
            faltam = len(assinaturas_esperadas - assinaturas_confirmadas)
            raise RuntimeError(
                f"O ciclo {ciclo_id} ainda possui {faltam} registro(s) não confirmados no histórico."
            )

        buscar_historico_dados.clear()
        LOGGER.info(
            "Ciclo %s arquivado com sucesso; %s linha(s) nova(s) gravada(s).",
            ciclo_id, len(linhas_faltantes)
        )
        return True, "ARQUIVADA"

    except Exception as exc:
        LOGGER.exception("Falha ao arquivar a lista no Historico.")
        return False, f"{type(exc).__name__}: {exc}"


def obter_estado_operacional(agora=None):
    """
    Fonte única das regras de abertura e do ciclo exibido no cabeçalho.
    Isso impede que a lista diga uma coisa e a regra de acesso faça outra.
    """
    agora = agora or datetime.now(FUSO_BR)
    t = agora.time()
    wd = agora.weekday()  # seg=0 ... sex=4, sáb=5, dom=6

    # Abertura da lista
    if wd == 5:  # sábado
        aberto = False
    elif wd == 6:  # domingo
        aberto = t >= time(19, 0)
    elif wd == 4:  # sexta
        aberto = not (time(5, 0) <= t < time(7, 0)) and t < time(17, 0)
    else:  # segunda a quinta
        aberto = not (
            (time(5, 0) <= t < time(7, 0))
            or (time(17, 0) <= t < time(19, 0))
        )

    janela_conferencia = (
        time(5, 0) < t < time(7, 0)
        or time(17, 0) < t < time(19, 0)
    )

    # Ciclo mostrado abaixo do título
    if wd == 4:  # sexta
        if t < time(7, 0):
            data_ciclo = agora.date()
            embarque = "06:30"
        elif t < time(19, 0):
            # Entre 17h e 19h a lista está fechada, mas continua pertencendo
            # ao embarque das 18:30 da própria sexta-feira.
            data_ciclo = agora.date()
            embarque = "18:30"
        else:
            data_ciclo = (agora + timedelta(days=3)).date()
            embarque = "06:30"
    elif wd == 5:  # sábado
        data_ciclo = (agora + timedelta(days=2)).date()
        embarque = "06:30"
    elif wd == 6:  # domingo
        data_ciclo = (agora + timedelta(days=1)).date()
        embarque = "06:30"
    else:
        if t >= time(19, 0):
            data_ciclo = (agora + timedelta(days=1)).date()
            embarque = "06:30"
        elif t < time(7, 0):
            data_ciclo = agora.date()
            embarque = "06:30"
        else:
            data_ciclo = agora.date()
            embarque = "18:30"

    return {
        "aberto": aberto,
        "janela_conferencia": janela_conferencia,
        "embarque": embarque,
        "data_ciclo": data_ciclo,
    }


def obter_marco_limpeza(agora=None):
    agora = agora or datetime.now(FUSO_BR)
    t = agora.time()

    if t >= time(18, 50):
        return agora.replace(hour=18, minute=50, second=0, microsecond=0)
    if t >= time(6, 50):
        return agora.replace(hour=6, minute=50, second=0, microsecond=0)
    return (agora - timedelta(days=1)).replace(hour=18, minute=50, second=0, microsecond=0)


def lista_precisa_limpeza(dados_p, agora=None):
    """
    Só considera a lista antiga quando a inscrição mais recente também é
    anterior ao marco. Isso impede apagar inscrições novas misturadas por
    alguma sessão atrasada.
    """
    if not dados_p or len(dados_p) < 2:
        return False

    datas = []
    for row in dados_p[1:]:
        if row:
            dt = _parse_data_hora_presenca(row[0])
            if dt:
                datas.append(dt)

    if not datas:
        return False

    return max(datas) < obter_marco_limpeza(agora)


def sincronizar_troca_de_ciclo(sheet_p, agora=None):
    """
    Arquiva e limpa uma lista vencida sem formar fila de sessões.

    A ordem é proposital:
    1) verificar se já há troca em andamento;
    2) tentar adquirir rapidamente o lock de mutação;
    3) marcar a troca como em andamento;
    4) reler, arquivar, confirmar e limpar.

    As demais sessões não aguardam as tentativas da API: recebem imediatamente
    EM_ANDAMENTO/OCUPADO e podem atualizar alguns segundos depois.
    """
    agora = agora or datetime.now(FUSO_BR)
    coord = coordenador_troca_ciclo()

    status_atual, restante = coord.obter_status()
    if status_atual == "EM_ANDAMENTO":
        return False, None, "EM_ANDAMENTO", 0
    if status_atual == "COOLDOWN":
        return False, None, "COOLDOWN", restante

    lock, adquirido = adquirir_lock_mutacao()
    if not adquirido:
        status_atual, restante = coord.obter_status()
        if status_atual == "EM_ANDAMENTO":
            return False, None, "EM_ANDAMENTO", 0
        return False, None, "OCUPADO", 1

    iniciou = False
    try:
        pode_iniciar, status_inicio, restante = coord.tentar_iniciar()
        if not pode_iniciar:
            return False, None, status_inicio, restante
        iniciou = True

        dados_frescos = gs_call(
            sheet_p.get_all_values,
            _max_tries=3,
            _max_sleep=2.0
        )
        dados_validos = filtrar_linhas_presenca(dados_frescos)

        if not lista_precisa_limpeza(dados_validos, agora):
            coord.finalizar(True)
            iniciou = False
            return False, dados_validos, "NAO_NECESSARIA", 0

        ok, status_arquivo = arquivar_lista_antes_de_limpar(dados_validos)
        if not ok:
            raise RuntimeError(status_arquivo)

        # Preserva o cabeçalho. A faixa inteira abaixo dele é limpa em uma única
        # operação, independentemente da quantidade de linhas da aba principal.
        gs_call(
            sheet_p.batch_clear,
            ["A2:F"],
            _max_tries=3,
            _max_sleep=2.0
        )

        # Confirma que não restaram passageiros antes de liberar o novo ciclo.
        restante_planilha = gs_call(
            sheet_p.get,
            "A2:F",
            _max_tries=3,
            _max_sleep=2.0
        )
        ainda_tem_dados = any(
            any(str(celula).strip() for celula in linha)
            for linha in (restante_planilha or [])
        )
        if ainda_tem_dados:
            raise RuntimeError("A limpeza da lista principal não foi confirmada.")

        header = dados_frescos[0] if dados_frescos else [
            "DATA_HORA", "QG_RMCF_OUTROS", "GRADUAÇÃO", "NOME", "LOTAÇÃO", "EMAIL"
        ]
        coord.finalizar(True)
        iniciou = False
        LOGGER.info("Troca de ciclo concluída com segurança (%s).", status_arquivo)
        return True, [header], "LIMPA", 0

    except Exception as exc:
        LOGGER.exception("Falha durante a troca de ciclo.")
        if iniciou:
            coord.finalizar(False, f"{type(exc).__name__}: {exc}")
            iniciou = False
        return False, None, "FALHA", int(TROCA_CICLO_COOLDOWN_ERRO)
    finally:
        if iniciou:
            coord.finalizar(False, "Troca interrompida antes da finalização.")
        lock.release()


def verificar_status_e_limpar(sheet_p, dados_p):
    agora = datetime.now(FUSO_BR)
    estado = obter_estado_operacional(agora)

    # A leitura cacheada funciona somente como gatilho. A decisão final e a
    # gravação usam uma nova leitura dentro da região de mutação protegida.
    if lista_precisa_limpeza(dados_p, agora):
        limpou, _dados_frescos, status, espera = sincronizar_troca_de_ciclo(sheet_p, agora)

        if status in {"EM_ANDAMENTO", "OCUPADO"}:
            st.warning(
                "A troca de ciclo está sendo concluída por outro acesso. "
                "Aguarde alguns segundos e toque em ATUALIZAR."
            )
            return False, estado["janela_conferencia"]

        if status == "COOLDOWN":
            st.error(
                "A última tentativa de troca de ciclo encontrou uma falha temporária. "
                f"Uma nova tentativa será liberada em aproximadamente {espera} segundo(s)."
            )
            return False, estado["janela_conferencia"]

        if status == "FALHA":
            st.error(
                "Não foi possível concluir com segurança a troca de ciclo. "
                "A lista permanecerá bloqueada para não misturar nem apagar inscrições. "
                "Aguarde cerca de 20 segundos e toque em ATUALIZAR."
            )
            return False, estado["janela_conferencia"]

        if limpou:
            st.session_state["_force_refresh_presenca"] = True
            st.rerun()

    return estado["aberto"], estado["janela_conferencia"]


def registrar_presenca_se_ausente(sheet_p, usuario):
    """Grava uma presença sem duplicidade e sem esperar em fila longa."""
    email = str(usuario.get("Email", "")).strip().lower()

    lock, adquirido = adquirir_lock_mutacao()
    if not adquirido:
        status, _ = coordenador_troca_ciclo().obter_status()
        if status == "EM_ANDAMENTO":
            return False, "TROCA_EM_ANDAMENTO"
        return False, "SISTEMA_OCUPADO"

    try:
        # A validação de horário e a eventual troca de ciclo são executadas
        # enquanto nenhuma outra sessão pode gravar ou apagar linhas. Como o
        # lock é RLock, sincronizar_troca_de_ciclo pode reutilizá-lo nesta thread.
        if not obter_estado_operacional()["aberto"]:
            return False, "LISTA_FECHADA"

        limpou, dados_frescos, status, _espera = sincronizar_troca_de_ciclo(sheet_p)
        if status in {"EM_ANDAMENTO", "OCUPADO", "COOLDOWN", "FALHA"}:
            return False, "TROCA_PENDENTE"

        if dados_frescos is None:
            dados_frescos = gs_call(
                sheet_p.get_all_values,
                _max_tries=2,
                _max_sleep=1.0
            )

        for row in (dados_frescos or [])[1:]:
            if len(row) >= 6 and str(row[5]).strip().lower() == email:
                return False, "JA_REGISTRADA"

        agora_str = datetime.now(FUSO_BR).strftime("%d/%m/%Y %H:%M:%S")
        gs_call(
            sheet_p.append_row,
            [
                agora_str,
                usuario.get("QG_RMCF_OUTROS") or "QG",
                usuario.get("Graduação"),
                usuario.get("Nome"),
                usuario.get("Lotação"),
                usuario.get("Email")
            ],
            value_input_option="USER_ENTERED",
            _max_tries=2,
            _max_sleep=1.0
        )
        return True, "REGISTRADA"

    except Exception:
        LOGGER.exception("Falha ao registrar presença.")
        return False, "ERRO_GRAVACAO"
    finally:
        lock.release()


def excluir_presenca_por_email(sheet_p, email):
    """
    Exclui somente a própria presença sem formar fila de sessões.

    A exclusão é confirmada por uma nova leitura direta do Google Sheets.
    Caso existam duplicidades antigas do mesmo e-mail, todas são removidas de
    baixo para cima para não deslocar os números das linhas ainda pendentes.
    """
    email = str(email or "").strip().lower()

    lock, adquirido = adquirir_lock_mutacao()
    if not adquirido:
        return False, "SISTEMA_OCUPADO"

    try:
        status_coord, _ = coordenador_troca_ciclo().obter_status()
        if status_coord == "EM_ANDAMENTO":
            return False, "TROCA_EM_ANDAMENTO"

        dados_frescos = gs_call(
            sheet_p.get_all_values,
            _max_tries=2,
            _max_sleep=1.0
        )

        linhas_encontradas = [
            row_number
            for row_number, row in enumerate(dados_frescos[1:], start=2)
            if len(row) >= 6 and str(row[5]).strip().lower() == email
        ]

        if not linhas_encontradas:
            return False, "NAO_ENCONTRADA"

        for row_number in reversed(linhas_encontradas):
            gs_call(
                sheet_p.delete_rows,
                row_number,
                _max_tries=2,
                _max_sleep=1.0
            )

        # Confirma que a exclusão realmente chegou ao Sheets antes de informar
        # sucesso para a interface.
        dados_confirmacao = gs_call(
            sheet_p.get_all_values,
            _max_tries=2,
            _max_sleep=1.0
        )
        ainda_existe = any(
            len(row) >= 6 and str(row[5]).strip().lower() == email
            for row in (dados_confirmacao or [])[1:]
        )

        if ainda_existe:
            LOGGER.error("Exclusão não confirmada para o e-mail %s.", email)
            return False, "EXCLUSAO_NAO_CONFIRMADA"

        return True, "EXCLUIDA"

    except Exception:
        LOGGER.exception("Falha ao excluir presença.")
        return False, "ERRO_EXCLUSAO"
    finally:
        lock.release()


# ==========================================================
# CICLO (exibição abaixo do título)
# ==========================================================
def obter_ciclo_atual():
    estado = obter_estado_operacional()
    return estado["embarque"], estado["data_ciclo"].strftime("%d/%m/%Y")



def aplicar_ordenacao(df, capacidade_onibus: int = CAPACIDADE_PADRAO_ONIBUS, prioridade_emails=None):
    """
    Ordena a lista e aplica a regra de prioridade:
    - primeiro calcula a ordem normal;
    - se usuário marcado como prioridade estiver como excedente, ele é deslocado para o final das vagas disponíveis;
    - quando houver 2 ou mais nessa condição, mantém a ordem entre eles;
    - usuários prioritários são destacados em azul/negrito na tabela.
    """
    try:
        capacidade_onibus = max(1, int(capacidade_onibus))
    except Exception:
        capacidade_onibus = CAPACIDADE_PADRAO_ONIBUS

    prioridade_emails = {str(e or "").strip().lower() for e in (prioridade_emails or set()) if str(e or "").strip()}

    if "EMAIL" not in df.columns:
        df["EMAIL"] = "N/A"

    # Garantia: coluna QG_RMCF_OUTROS deve existir na planilha de presença
    if "QG_RMCF_OUTROS" not in df.columns and "ORIGEM" in df.columns:
        df["QG_RMCF_OUTROS"] = df["ORIGEM"]
    if "QG_RMCF_OUTROS" not in df.columns:
        df["QG_RMCF_OUTROS"] = ""

    # Ordem hierárquica dos militares.
    p_grad_normal = {
        "TCEL": 1, "MAJ": 2, "CAP": 3, "1º TEN": 4, "2º TEN": 5, "SUBTEN": 6,
        "1º SGT": 7, "2º SGT": 8, "3º SGT": 9, "CB": 10, "SD": 11
    }

    def normalizar_grad(grad):
        return str(grad or "").strip().upper()

    def normalizar_origem(origem):
        return str(origem or "").strip().upper()

    p_orig = {"QG": 1, "RMCF": 2, "OUTROS": 3}

    def bloco_embarque(row):
        """
        Prioridade de embarque ajustada:
        1) Militares do QG, por antiguidade;
        2) FC COM do QG, por ordem de chegada;
        3) Militares do RMCF, por antiguidade;
        4) Militares de OUTROS, por antiguidade;
        5) FC COM de RMCF/OUTROS, mantendo a ordem antiga por origem e chegada;
        6) FC TER, mantendo a ordem antiga por origem e chegada;
        7) Demais casos não previstos.
        """
        grad = normalizar_grad(row.get("GRADUAÇÃO", ""))
        origem = normalizar_origem(row.get("QG_RMCF_OUTROS", ""))

        if grad == "FC COM" and origem == "QG":
            return 2
        if grad == "FC COM":
            return 5
        if grad == "FC TER":
            return 6
        if origem == "QG":
            return 1
        if origem == "RMCF":
            return 3
        if origem == "OUTROS":
            return 4
        return 7

    def p_grad(row):
        grad = normalizar_grad(row.get("GRADUAÇÃO", ""))
        if grad in {"FC COM", "FC TER"}:
            return 0
        return p_grad_normal.get(grad, 999)

    df["p_bloco"] = df.apply(bloco_embarque, axis=1)
    df["p_o"] = df["QG_RMCF_OUTROS"].apply(lambda x: p_orig.get(normalizar_origem(x), 99))
    df["p_g"] = df.apply(p_grad, axis=1)

    # Desempate por quem entrou primeiro. Para FC COM e FC TER, essa é a ordem principal dentro do bloco.
    df["dt"] = pd.to_datetime(df["DATA_HORA"], dayfirst=True, errors="coerce")

    # Ordenação final: QG militares -> FC COM/QG -> RMCF militares -> OUTROS militares -> FC COM demais -> FC TER.
    df = df.sort_values(by=["p_bloco", "p_o", "p_g", "dt"]).reset_index(drop=True)
    df["_PRIORIDADE_LISTA"] = df["EMAIL"].astype(str).str.strip().str.lower().isin(prioridade_emails)

    # ==========================================================
    # PRIORIDADE DE EMBARQUE
    # ==========================================================
    # Somente mexe se a pessoa prioritária estiver fora das vagas.
    if len(df) > capacidade_onibus and prioridade_emails:
        primeira_faixa = df.iloc[:capacidade_onibus].copy()
        excedentes = df.iloc[capacidade_onibus:].copy()
        prioridade_excedente = excedentes[excedentes["_PRIORIDADE_LISTA"]].copy()

        if not prioridade_excedente.empty:
            qtd_prioridade_excedente = len(prioridade_excedente)

            # Posições finais disponíveis dentro das vagas, sem derrubar quem já é prioritário.
            posicoes_substituir = []
            for pos in range(len(primeira_faixa) - 1, -1, -1):
                if not bool(primeira_faixa.iloc[pos].get("_PRIORIDADE_LISTA", False)):
                    posicoes_substituir.append(pos)
                    if len(posicoes_substituir) == qtd_prioridade_excedente:
                        break

            # Caso extremo: se a faixa de vagas estiver toda preenchida por prioritários,
            # não há usuário comum a deslocar. Nesse caso, mantém a ordem calculada.
            if len(posicoes_substituir) == qtd_prioridade_excedente:
                posicoes_substituir = sorted(posicoes_substituir)

                # Remove os prioritários excedentes da parte excedente para evitar duplicidade.
                emails_mover = set(prioridade_excedente["EMAIL"].astype(str).str.strip().str.lower().tolist())
                excedentes_sem_mover = excedentes[~excedentes["EMAIL"].astype(str).str.strip().str.lower().isin(emails_mover)].copy()

                deslocados_para_excedente = []
                prioridade_excedente = prioridade_excedente.reset_index(drop=True)

                for ordem, pos in enumerate(posicoes_substituir):
                    deslocados_para_excedente.append(primeira_faixa.iloc[pos].copy())
                    primeira_faixa.iloc[pos] = prioridade_excedente.iloc[ordem]

                df = pd.concat([
                    primeira_faixa,
                    pd.DataFrame(deslocados_para_excedente),
                    excedentes_sem_mover
                ], ignore_index=True)

    df.insert(0, "Nº", [str(i + 1) if i < capacidade_onibus else f"Exc-{i - capacidade_onibus + 1:02d}" for i in range(len(df))])

    # Remove primeiro as colunas auxiliares para evitar erro de dtype ao inserir HTML
    df_final = df.drop(columns=["p_bloco", "p_o", "p_g", "dt", PRIORIDADE_HEADER], errors="ignore").copy()
    df_v = df_final.copy()

    for i, r in df_v.iterrows():
        is_prioridade = bool(r.get("_PRIORIDADE_LISTA", False))
        is_exc = "Exc-" in str(r.get("Nº", ""))

        if is_prioridade:
            for c in df_v.columns:
                if c == "_PRIORIDADE_LISTA":
                    continue
                df_v.at[i, c] = f"<span style='color:#1565c0; font-weight:bold;'>{r[c]}</span>"
        elif is_exc:
            for c in df_v.columns:
                if c == "_PRIORIDADE_LISTA":
                    continue
                df_v.at[i, c] = f"<span style='color:#d32f2f; font-weight:bold;'>{r[c]}</span>"

    return df_final, df_v

# ==========================================================
# PDF “mais apresentado” (AGORA COM ORIGEM À DIREITA)
# ==========================================================
class PDFRelatorio(FPDF):
    def __init__(self, titulo="LISTA DE PRESENÇA", sub=None):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.titulo = titulo
        self.sub = sub or ""
        self.set_auto_page_break(auto=True, margin=12)
        self.alias_nb_pages()

    def header(self):
        self.set_font("Arial", "B", 14)
        self.cell(0, 8, self.titulo, ln=True, align="C")

        self.set_font("Arial", "", 9)
        if self.sub:
            self.cell(0, 5, self.sub, ln=True, align="C")
        self.ln(2)

        self.set_draw_color(180, 180, 180)
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-12)
        self.set_font("Arial", "", 8)
        self.set_text_color(90, 90, 90)
        self.cell(0, 6, f"Página {self.page_no()}/{{nb}} - Rota Nova Iguaçu", align="C")


def gerar_pdf_apresentado(df_o: pd.DataFrame, resumo: dict, subtitulo_extra: str = "") -> bytes:
    agora = datetime.now(FUSO_BR).strftime("%d/%m/%Y %H:%M:%S")
    sub = f"Emitido em: {agora}"
    if subtitulo_extra:
        sub = f"{sub} | {subtitulo_extra}"

    pdf = PDFRelatorio(titulo="ROTA NOVA IGUAÇU - LISTA DE PRESENÇA", sub=sub)
    pdf.add_page()

    # Bloco resumo
    pdf.set_font("Arial", "B", 10)
    pdf.set_fill_color(240, 240, 240)
    pdf.cell(0, 8, "RESUMO", ln=True, fill=True)

    pdf.set_font("Arial", "", 9)
    insc = resumo.get("inscritos", 0)
    vagas = resumo.get("vagas", CAPACIDADE_PADRAO_ONIBUS)
    exc = max(0, insc - vagas)
    sobra = max(0, vagas - insc)

    pdf.cell(0, 6, f"Inscritos: {insc} | Vagas: {vagas} | Sobra: {sobra} | Excedentes: {exc}", ln=True)
    pdf.ln(2)

    # Tabela com ORIGEM no final (direita)
    headers = ["Nº", "GRADUAÇÃO", "NOME", "LOTAÇÃO", "ORIGEM"]
    col_w = [12, 26, 78, 55, 19]

    pdf.set_font("Arial", "B", 9)
    pdf.set_fill_color(30, 30, 30)
    pdf.set_text_color(255, 255, 255)

    for i, h in enumerate(headers):
        pdf.cell(col_w[i], 7, h, border=0, align="C", fill=True)
    pdf.ln()

    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Arial", "", 8)

    for idx, (_, r) in enumerate(df_o.iterrows()):
        is_exc = "Exc-" in str(r.get("Nº", ""))
        is_prioridade = bool(r.get("_PRIORIDADE_LISTA", False))

        if is_exc:
            pdf.set_fill_color(255, 235, 238)
        else:
            if idx % 2 == 0:
                pdf.set_fill_color(245, 245, 245)
            else:
                pdf.set_fill_color(255, 255, 255)

        if is_prioridade:
            pdf.set_font("Arial", "B", 8)
            pdf.set_text_color(21, 101, 192)
        else:
            pdf.set_font("Arial", "", 8)
            pdf.set_text_color(0, 0, 0)

        origem = str(r.get("QG_RMCF_OUTROS", "") or r.get("ORIGEM", "") or "").strip()

        pdf.cell(col_w[0], 6, str(r.get("Nº", "")), border=0, fill=True)
        pdf.cell(col_w[1], 6, str(r.get("GRADUAÇÃO", "")), border=0, fill=True)
        pdf.cell(col_w[2], 6, str(r.get("NOME", ""))[:42], border=0, fill=True)
        pdf.cell(col_w[3], 6, str(r.get("LOTAÇÃO", ""))[:34], border=0, fill=True)
        pdf.cell(col_w[4], 6, origem[:10], border=0, align="C", fill=True)
        pdf.ln()

    pdf.set_font("Arial", "", 8)
    pdf.set_text_color(0, 0, 0)

    pdf.ln(4)
    pdf.set_font("Arial", "I", 8)
    pdf.set_text_color(80, 80, 80)
    pdf.multi_cell(0, 5, "Observação: os itens marcados como 'Exc-xx' representam excedentes além da capacidade configurada para o ônibus.")
    pdf.set_text_color(0, 0, 0)

    return pdf.output(dest="S").encode("latin-1")


# ==========================================================
# PDF ADM: RELAÇÃO DE USUÁRIOS CADASTRADOS
# ==========================================================
def ordenar_usuarios_para_relatorio(records_u):
    """
    Monta e ordena a relação de usuários cadastrados usando a mesma base
    de prioridade da lista de passageiros:
    - Militares do QG por antiguidade;
    - FC COM do QG por ordem de cadastro/chegada;
    - Militares do RMCF por antiguidade;
    - Militares de OUTROS por antiguidade;
    - FC COM de RMCF/OUTROS pela ordem antiga;
    - FUNC TER por ordem de cadastro/chegada;
    - Empate: ordem de cadastro na planilha.
    """
    linhas = []
    for ordem, user in enumerate(records_u or []):
        grad = str(user.get("Graduação", "") or user.get("GRADUAÇÃO", "")).strip()
        nome = str(user.get("Nome", "") or user.get("NOME", "")).strip()
        lotacao = str(user.get("Lotação", "") or user.get("LOTAÇÃO", "")).strip()
        origem = str(
            user.get("QG_RMCF_OUTROS", "")
            or user.get("ORIGEM", "")
            or user.get("Origem", "")
        ).strip().upper()
        telefone = tel_format_br(str(user.get("TELEFONE", "") or user.get("Telefone", "") or ""))

        linhas.append({
            "GRADUAÇÃO": grad,
            "NOME": nome,
            "LOTAÇÃO": lotacao,
            "ORIGEM": origem,
            "TELEFONE": telefone,
            "_ORDEM_CADASTRO": ordem,
            "_PRIORIDADE_LISTA": prioridade_ativa(user.get(PRIORIDADE_HEADER, "")),
        })

    df = pd.DataFrame(linhas)
    if df.empty:
        return df

    p_grad_normal = {
        "TCEL": 1, "MAJ": 2, "CAP": 3, "1º TEN": 4, "2º TEN": 5, "SUBTEN": 6,
        "1º SGT": 7, "2º SGT": 8, "3º SGT": 9, "CB": 10, "SD": 11
    }

    def normalizar_grad(grad):
        return str(grad or "").strip().upper()

    def normalizar_origem(origem):
        return str(origem or "").strip().upper()

    p_orig = {"QG": 1, "RMCF": 2, "OUTROS": 3}

    def bloco_embarque(row):
        grad = normalizar_grad(row.get("GRADUAÇÃO", ""))
        origem = normalizar_origem(row.get("ORIGEM", ""))

        if grad == "FC COM" and origem == "QG":
            return 2
        if grad == "FC COM":
            return 5
        if grad == "FC TER":
            return 6
        if origem == "QG":
            return 1
        if origem == "RMCF":
            return 3
        if origem == "OUTROS":
            return 4
        return 7

    def p_grad(row):
        grad = normalizar_grad(row.get("GRADUAÇÃO", ""))
        if grad in {"FC COM", "FC TER"}:
            return 0
        return p_grad_normal.get(grad, 999)

    df["p_bloco"] = df.apply(bloco_embarque, axis=1)
    df["p_o"] = df["ORIGEM"].apply(lambda x: p_orig.get(normalizar_origem(x), 99))
    df["p_g"] = df.apply(p_grad, axis=1)
    df = df.sort_values(by=["p_bloco", "p_o", "p_g", "_ORDEM_CADASTRO"]).reset_index(drop=True)
    df.insert(0, "Nº", [str(i + 1) for i in range(len(df))])
    return df.drop(columns=["p_bloco", "p_o", "p_g"], errors="ignore")


def gerar_pdf_usuarios_admin(records_u) -> bytes:
    """Gera PDF com todos os usuários cadastrados para download no painel ADM."""
    agora = datetime.now(FUSO_BR).strftime("%d/%m/%Y %H:%M:%S")
    pdf = PDFRelatorio(
        titulo="ROTA NOVA IGUAÇU - USUÁRIOS CADASTRADOS",
        sub=f"Emitido em: {agora} | Ordem: QG -> FC COM/QG -> RMCF -> OUTROS -> FC COM demais -> FC TER"
    )
    pdf.add_page()

    df_u = ordenar_usuarios_para_relatorio(records_u)

    pdf.set_font("Arial", "B", 10)
    pdf.set_fill_color(240, 240, 240)
    pdf.cell(0, 8, "RESUMO", ln=True, fill=True)

    total = len(df_u) if df_u is not None else 0
    total_prioridade = 0
    if df_u is not None and not df_u.empty and "_PRIORIDADE_LISTA" in df_u.columns:
        total_prioridade = int(df_u["_PRIORIDADE_LISTA"].fillna(False).astype(bool).sum())

    pdf.set_font("Arial", "", 9)
    pdf.cell(0, 6, f"Total de usuários cadastrados: {total} | Com prioridade: {total_prioridade}", ln=True)
    pdf.ln(2)

    headers = ["Nº", "GRADUAÇÃO", "NOME", "LOTAÇÃO", "ORIGEM", "TELEFONE"]
    col_w = [10, 24, 62, 45, 19, 30]

    pdf.set_font("Arial", "B", 8)
    pdf.set_fill_color(30, 30, 30)
    pdf.set_text_color(255, 255, 255)
    for i, h in enumerate(headers):
        pdf.cell(col_w[i], 7, h, border=0, align="C", fill=True)
    pdf.ln()

    if df_u is not None and not df_u.empty:
        for idx, (_, r) in enumerate(df_u.iterrows()):
            if idx % 2 == 0:
                pdf.set_fill_color(245, 245, 245)
            else:
                pdf.set_fill_color(255, 255, 255)

            is_prioridade = bool(r.get("_PRIORIDADE_LISTA", False))
            if is_prioridade:
                pdf.set_font("Arial", "B", 8)
                pdf.set_text_color(21, 101, 192)
            else:
                pdf.set_font("Arial", "", 8)
                pdf.set_text_color(0, 0, 0)

            pdf.cell(col_w[0], 6, str(r.get("Nº", "")), border=0, align="C", fill=True)
            pdf.cell(col_w[1], 6, str(r.get("GRADUAÇÃO", ""))[:12], border=0, fill=True)
            pdf.cell(col_w[2], 6, str(r.get("NOME", ""))[:36], border=0, fill=True)
            pdf.cell(col_w[3], 6, str(r.get("LOTAÇÃO", ""))[:25], border=0, fill=True)
            pdf.cell(col_w[4], 6, str(r.get("ORIGEM", ""))[:10], border=0, align="C", fill=True)
            pdf.cell(col_w[5], 6, str(r.get("TELEFONE", ""))[:18], border=0, align="C", fill=True)
            pdf.ln()
    else:
        pdf.set_font("Arial", "I", 9)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(0, 8, "Nenhum usuário cadastrado encontrado.", ln=True)

    pdf.set_font("Arial", "", 8)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)
    pdf.set_font("Arial", "I", 8)
    pdf.set_text_color(80, 80, 80)
    pdf.multi_cell(0, 5, "Observação: usuários em azul/negrito possuem prioridade de embarque na lista de passageiros.")
    pdf.set_text_color(0, 0, 0)

    return pdf.output(dest="S").encode("latin-1")


# ==========================================================
# TELA DO HISTÓRICO
# ==========================================================
def render_historico_page():
    st.header("📚 Histórico")
    st.caption("As listas encerradas são salvas automaticamente quando um novo ciclo começa e a lista antiga é limpa.")

    dados_h = buscar_historico_dados()
    if not dados_h or len(dados_h) < 2:
        st.info("Ainda não há listas arquivadas. O primeiro histórico será criado automaticamente quando uma lista antiga for zerada no início de um novo ciclo.")
        return

    headers = list(dados_h[0])
    rows = dados_h[1:]
    rows_norm = []
    for r in rows:
        rr = list(r)
        if len(rr) < len(headers):
            rr = rr + [""] * (len(headers) - len(rr))
        elif len(rr) > len(headers):
            rr = rr[:len(headers)]
        rows_norm.append(rr)
    df_h = pd.DataFrame(rows_norm, columns=headers)

    # Garante as colunas esperadas mesmo se a aba tiver sido criada manualmente com algo faltando.
    for col in HIST_HEADERS:
        if col not in df_h.columns:
            df_h[col] = ""

    df_h["DATA_CICLO_DT"] = pd.to_datetime(df_h["DATA_CICLO"], dayfirst=True, errors="coerce").dt.date
    datas_disponiveis = sorted([d for d in df_h["DATA_CICLO_DT"].dropna().unique()])

    if not datas_disponiveis:
        st.warning("Existe aba de histórico, mas não encontrei nenhuma data válida salva nela.")
        return

    data_padrao = datas_disponiveis[-1]
    data_escolhida = st.date_input(
        "Selecione a data da lista arquivada:",
        value=data_padrao,
        min_value=datas_disponiveis[0],
        max_value=datas_disponiveis[-1],
        format="DD/MM/YYYY"
    )

    if data_escolhida not in datas_disponiveis:
        ultimas = ", ".join([d.strftime("%d/%m/%Y") for d in datas_disponiveis[-10:]])
        st.warning(f"Não há lista salva para essa data. Datas disponíveis mais recentes: {ultimas}.")
        return

    df_dia = df_h[df_h["DATA_CICLO_DT"] == data_escolhida].copy()
    ciclos = []
    for ciclo_id, grupo in df_dia.groupby("CICLO_ID", sort=False):
        embarque = str(grupo["EMBARQUE"].iloc[0]).strip() or "--:--"
        data_ciclo = str(grupo["DATA_CICLO"].iloc[0]).strip()
        ciclos.append((ciclo_id, f"EMBARQUE {embarque}h - {data_ciclo}"))

    if not ciclos:
        st.warning("Não encontrei ciclo salvo para essa data.")
        return

    if len(ciclos) == 1:
        ciclo_escolhido = ciclos[0][0]
        st.success(f"Lista encontrada: {ciclos[0][1]}")
    else:
        opcoes = {label: cid for cid, label in ciclos}
        label_escolhida = st.selectbox("Selecione o ciclo:", list(opcoes.keys()))
        ciclo_escolhido = opcoes[label_escolhida]

    df_ciclo = df_dia[df_dia["CICLO_ID"] == ciclo_escolhido].copy()
    if df_ciclo.empty:
        st.warning("O ciclo selecionado está vazio.")
        return

    df_base = df_ciclo[["DATA_HORA", "QG_RMCF_OUTROS", "GRADUAÇÃO", "NOME", "LOTAÇÃO", "EMAIL"]].copy()

    prioridade_hist_emails = set()
    if PRIORIDADE_HEADER in df_ciclo.columns:
        df_base[PRIORIDADE_HEADER] = df_ciclo[PRIORIDADE_HEADER].fillna("")
        prioridade_hist_emails = set(
            df_base.loc[df_base[PRIORIDADE_HEADER].apply(prioridade_ativa), "EMAIL"]
            .astype(str).str.strip().str.lower().tolist()
        )

    capacidade_hist = CAPACIDADE_PADRAO_ONIBUS
    if "CAPACIDADE_ONIBUS" in df_ciclo.columns:
        cap_vals = pd.to_numeric(df_ciclo["CAPACIDADE_ONIBUS"], errors="coerce").dropna()
        if not cap_vals.empty:
            capacidade_hist = max(1, int(cap_vals.iloc[0]))

    df_o_hist, df_v_hist = aplicar_ordenacao(df_base, capacidade_hist, prioridade_hist_emails)

    insc = len(df_o_hist)
    rest_hist = capacidade_hist - insc
    st.subheader(f"Inscritos: {insc} | Vagas: {capacidade_hist} | {'Sobra' if rest_hist >= 0 else 'Exc'}: {abs(rest_hist)}")

    df_v_show = df_v_hist.copy()
    if "NOME" in df_v_show.columns:
        df_v_show["NOME"] = df_v_show["NOME"].apply(lambda x: f"<b>{x}</b>")

    st.write(
        f"<div class='tabela-responsiva'>"
        f"{colunas_para_exibir(df_v_show).to_html(index=False, justify='center', border=0, escape=False, classes='presenca-zebra')}"
        f"</div>",
        unsafe_allow_html=True
    )

    data_ciclo_pdf = str(df_ciclo["DATA_CICLO"].iloc[0]).strip()
    embarque_pdf = str(df_ciclo["EMBARQUE"].iloc[0]).strip()
    subtitulo = f"Histórico: EMBARQUE {embarque_pdf}h do dia {data_ciclo_pdf}"
    resumo = {"inscritos": insc, "vagas": capacidade_hist}
    pdf_bytes = gerar_pdf_apresentado(df_o_hist, resumo, subtitulo_extra=subtitulo)

    nome_pdf = f"historico_rota_nova_iguacu_{ciclo_escolhido}.pdf"
    st.download_button(
        "📄 GERAR PDF DO HISTÓRICO",
        pdf_bytes,
        nome_pdf,
        mime="application/pdf",
        use_container_width=True
    )


# ==========================================================
# INTERFACE
# ==========================================================
st.set_page_config(page_title="Rota Nova Iguaçu", layout="centered")
st.markdown('<script src="https://telegram.org/js/telegram-web-app.js"></script>', unsafe_allow_html=True)

st.markdown("""
<style>
    .titulo-container { text-align: center; width: 100%; }
    .titulo-responsivo { font-size: clamp(1.2rem, 5vw, 2.2rem); font-weight: bold; margin-bottom: 6px; }
    .subtitulo-ciclo { text-align:center; font-size: 0.95rem; color: #444; margin-bottom: 16px; }
    .stCheckbox { background-color: #f8f9fa; padding: 5px; border-radius: 4px; border: 1px solid #eee; }
    .tabela-responsiva { width: 100%; overflow-x: auto; }
    table { width: 100% !important; font-size: 10px; table-layout: fixed; border-collapse: collapse; }
    th, td { text-align: center; padding: 2px !important; white-space: normal !important; word-wrap: break-word; }
    .footer { text-align: center; font-size: 11px; color: #888; margin-top: 40px; padding: 10px; border-top: 1px solid #eee; }

    /* ======================================================
       ALTERAÇÃO SOLICITADA (TELA): LINHAS ALTERNADAS (ZEBRA)
       - aplica somente na tabela de presença (classe abaixo)
       ====================================================== */
    table.presenca-zebra tbody tr:nth-child(odd)  { background: #f5f5f5; }
    table.presenca-zebra tbody tr:nth-child(even) { background: #ffffff; }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="titulo-container"><div class="titulo-responsivo">🚌 ROTA NOVA IGUAÇU 🚌</div></div>', unsafe_allow_html=True)

# Exibe o ciclo logo abaixo do título
ciclo_h, ciclo_d = obter_ciclo_atual()
st.markdown(f"<div class='subtitulo-ciclo'>Ciclo atual: <b>EMBARQUE {ciclo_h}h</b> do dia <b>{ciclo_d}</b></div>", unsafe_allow_html=True)

if "usuario_logado" not in st.session_state:
    st.session_state.usuario_logado = None
if "is_admin" not in st.session_state:
    st.session_state.is_admin = False
if "_admin_master" not in st.session_state:
    st.session_state._admin_master = False

# (antes era _force_password_change; agora existe o novo fluxo de atualização completa)
if "_force_profile_update" not in st.session_state:
    st.session_state._force_profile_update = False
if "_profile_update_row" not in st.session_state:
    st.session_state._profile_update_row = None

if "_login_kind" not in st.session_state:
    st.session_state._login_kind = ""
if "conf_ativa" not in st.session_state:
    st.session_state.conf_ativa = False
if "_force_refresh_presenca" not in st.session_state:
    st.session_state._force_refresh_presenca = False
if "_adm_first_load" not in st.session_state:
    st.session_state._adm_first_load = False
if "_tel_login_fmt" not in st.session_state:
    st.session_state._tel_login_fmt = ""
if "_tel_cad_fmt" not in st.session_state:
    st.session_state._tel_cad_fmt = ""

# ==========================================================
# NOVO (somente para confirmar exclusão da presença)
# ==========================================================
if "_confirmar_exclusao_presenca" not in st.session_state:
    st.session_state._confirmar_exclusao_presenca = False

# Depois de uma gravação, a exclusão somente é liberada quando uma leitura
# direta do Google Sheets confirmar que a presença realmente foi persistida.
if "_presenca_aguardando_confirmacao" not in st.session_state:
    st.session_state._presenca_aguardando_confirmacao = False

# Durante alguns segundos após gravar/excluir ou enquanto a confirmação de
# exclusão estiver aberta, esta sessão ignora o cache da lista. Isso evita que
# um rerun mostre uma versão antiga e apenas "simule" a exclusão na tela.
if "_leitura_direta_presenca_ate" not in st.session_state:
    st.session_state._leitura_direta_presenca_ate = 0.0

if "_flash_operacao" not in st.session_state:
    st.session_state._flash_operacao = None
if "_adm_flash" not in st.session_state:
    st.session_state._adm_flash = None

try:
    # Verificação estrutural executada apenas uma vez por inicialização do app.
    inicializar_estrutura_usuarios()
    sheet_u_escrita = ws_usuarios()

    # Leitura leve pro público
    records_u_public = buscar_usuarios_cadastrados()
    emails_prioridade_lista = obter_emails_prioridade(records_u_public)
    limite_max = buscar_limite_dinamico()
    capacidade_onibus = buscar_capacidade_onibus_dinamica()

    # =========================================
    # LOGIN / CADASTRO / INSTRUÇÕES / RECUPERAR / ADM
    # =========================================
    if st.session_state.usuario_logado is None and not st.session_state.is_admin:
        t1, t2, t3, t4, t5 = st.tabs(["Login", "Cadastro", "Instruções", "Recuperar", "ADM"])

        with t1:
            with st.form("form_login"):
                l_e = st.text_input("E-mail:")

                raw_tel_login = st.text_input("Telefone:", value=st.session_state._tel_login_fmt)
                fmt_tel_login = tel_format_br(raw_tel_login)
                st.session_state._tel_login_fmt = fmt_tel_login

                l_s = st.text_input("Senha:", type="password")

                entrou = st.form_submit_button("▶️ ENTRAR ◀️", use_container_width=True)
                if entrou:
                    if not tel_is_valid_11(fmt_tel_login):
                        st.error("Telefone inválido. Use DDD + 9 dígitos (ex: 21987654321).")
                    else:
                        tel_login_digits = tel_only_digits(fmt_tel_login)

                        def _senha_temp_valida(u_dict):
                            try:
                                temp = str(u_dict.get("TEMP_SENHA", "") or "").strip()
                                usada = str(u_dict.get("TEMP_USADA", "") or "").strip().upper()
                                exp = str(u_dict.get("TEMP_EXPIRA", "") or "").strip()
                                if not temp or usada != "NAO":
                                    return False
                                exp_dt = _parse_dt(exp)
                                if exp_dt is None:
                                    return False
                                return _br_now() <= exp_dt
                            except Exception:
                                return False

                        def _senha_confere(u_dict, senha_digitada: str):
                            senha_digitada = str(senha_digitada or "")
                            if str(u_dict.get("Senha", "")) == senha_digitada:
                                return ("REAL", True)
                            if _senha_temp_valida(u_dict) and str(u_dict.get("TEMP_SENHA", "")).strip() == senha_digitada:
                                return ("TEMP", True)
                            return ("", False)

                        u_a = next(
                            (u for u in records_u_public
                             if str(u.get("Email", "")).strip().lower() == l_e.strip().lower()
                             and tel_only_digits(u.get("TELEFONE", "")) == tel_login_digits
                             and _senha_confere(u, l_s)[1]),
                            None
                        )

                        if u_a:
                            status_user = str(u_a.get("STATUS", "")).strip().upper()
                            if status_user == "ATIVO":
                                kind, _ok = _senha_confere(u_a, l_s)
                                st.session_state.usuario_logado = u_a
                                st.session_state._login_kind = kind

                                # ==========================================================
                                # NOVO: Se entrou com TEMP -> força ATUALIZAÇÃO COMPLETA DO CADASTRO
                                # (tudo pode mudar, EXCETO email)
                                # ==========================================================
                                if kind == "TEMP":
                                    try:
                                        row_idx, _d = find_user_row_by_email_tel(sheet_u_escrita, l_e, tel_login_digits)
                                        st.session_state._force_profile_update = True
                                        st.session_state._profile_update_row = row_idx
                                    except Exception:
                                        st.session_state._force_profile_update = True
                                        st.session_state._profile_update_row = None

                                st.rerun()
                            else:
                                st.error("Acesso negado. Aguardando aprovação do Administrador.")
                        else:
                            st.error("Dados incorretos.")

        with t2:
            if len(records_u_public) >= limite_max:
                st.warning(f"⚠️ Limite de {limite_max} usuários atingido.")
            else:
                with st.form("form_novo_cadastro"):
                    n_n = st.text_input("Nome de Escala:")
                    n_e = st.text_input("E-mail:")

                    raw_tel_cad = st.text_input("Telefone:", value=st.session_state._tel_cad_fmt)
                    fmt_tel_cad = tel_format_br(raw_tel_cad)
                    st.session_state._tel_cad_fmt = fmt_tel_cad

                    n_g = st.selectbox("Graduação:", GRADUACOES_VALIDAS)
                    n_l = st.text_input("Lotação:")
                    n_o = st.selectbox("Origem:", ["QG", "RMCF", "OUTROS"])
                    n_p = st.text_input("Senha:", type="password")

                    cadastrou = st.form_submit_button("✍️ SALVAR CADASTRO 👈", use_container_width=True)
                    if cadastrou:
                        # ==========================================================
                        # OBRIGATÓRIO: todos os campos do CADASTRO
                        # ==========================================================
                        def norm_str(x):
                            return str(x or "").strip()

                        n_n_ok = bool(norm_str(n_n))
                        n_e_ok = bool(norm_str(n_e))
                        n_l_ok = bool(norm_str(n_l))
                        n_p_ok = bool(norm_str(n_p))
                        n_g_ok = bool(norm_str(n_g))
                        n_o_ok = bool(norm_str(n_o))

                        # e-mail básico
                        email_ok = bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", norm_str(n_e)))

                        missing = []
                        if not n_n_ok: missing.append("Nome de Escala")
                        if not n_e_ok: missing.append("E-mail")
                        if not email_ok and n_e_ok: missing.append("E-mail (formato inválido)")
                        if not tel_is_valid_11(fmt_tel_cad): missing.append("Telefone (inválido)")
                        if not n_g_ok: missing.append("Graduação")
                        if not n_l_ok: missing.append("Lotação")
                        if not n_o_ok: missing.append("Origem")
                        if not n_p_ok: missing.append("Senha")

                        if missing:
                            st.error("Preencha corretamente todos os campos: " + ", ".join(missing) + ".")
                        else:
                            # ==========================================================
                            # BLOQUEAR CADASTRO SE EMAIL OU TELEFONE JÁ EXISTIREM
                            # ==========================================================
                            novo_email = norm_str(n_e).lower()
                            novo_tel_digits = tel_only_digits(fmt_tel_cad)

                            email_existe = any(str(u.get("Email", "")).strip().lower() == novo_email for u in records_u_public)
                            tel_existe = any(tel_only_digits(u.get("TELEFONE", "")) == novo_tel_digits for u in records_u_public)

                            if email_existe and tel_existe:
                                st.error("E-mail e Telefone já cadastrados.")
                            elif email_existe:
                                st.error("E-mail já cadastrado.")
                            elif tel_existe:
                                st.error("Telefone já cadastrado.")
                            else:
                                gs_call(sheet_u_escrita.append_row, [
                                    norm_str(n_n),
                                    norm_str(n_g),
                                    norm_str(n_l),
                                    norm_str(n_p),
                                    norm_str(n_o),
                                    norm_str(n_e),
                                    fmt_tel_cad,
                                    "PENDENTE"
                                ])
                                buscar_usuarios_cadastrados.clear()
                                buscar_usuarios_admin.clear()
                                st.success("Cadastro realizado! Aguardando aprovação do Administrador.")
                                st.rerun()

        with t3:
            st.markdown("### 📖 Guia de Uso")
            st.success("📲 **COMO INSTALAR (TELA INICIAL)**")
            st.markdown("**No Chrome (Android):** Toque nos 3 pontos (⋮) e em 'Instalar Aplicativo'.")
            st.markdown("**No Safari (iPhone):** Toque em Compartilhar (⬆️) e em 'Adicionar à Tela de Início'.")
            st.markdown("**No Telegram:** Procure o bot @RotaNovaIguacuBot e toque no botão 'Abrir App Rota' no menu.")
            st.markdown("**QR CODE:** https://drive.google.com/file/d/1ALXgvt44vGWiGaW7HAfwYHfx-I_Dbgjq/view?usp=sharing")
            st.markdown("**LINK PARA NAVEGADOR:** https://rota-presenca-5hcorx5wezfaezztkehwol.streamlit.app/")
            st.divider()
            st.info("**CADASTRO E LOGIN:** Use seu e-mail como identificador único.")
            st.markdown("""
            **1. Regras de Horário:**
            * **Manhã:** Inscrições abertas até às 05:00h. Reabre às 07:00h.
            * **Tarde:** Inscrições abertas até às 17:00h. Reabre às 19:00h.
            * **Finais de Semana:** Abrem domingo às 19:00h.

            **2. Observação (1):**
            * Nos períodos em que a lista ficar suspensa para conferência (05:00h às 07:00h / 17:00h às 19:00h), os três PPMM que estiverem no topo da lista terão acesso à lista de check up (botão no topo da lista) para tirar a falta de quem estará entrando no ônibus. O mais antigo assume e na ausência dele o seu sucessor assume.
            * Após o horário de 06:50h e de 18:50h, a lista será automaticamente zerada para que o novo ciclo da lista possa ocorrer. Antes de ser zerada, a lista anterior será arquivada automaticamente na aba **Histórico**, onde poderá ser consultada por data e baixada em PDF.
            * A quantidade de vagas consideradas como "normais" é definida pelo Administrador no painel ADM, no campo **Capacidade do ônibus**. Quem ultrapassar essa capacidade aparecerá como **Exc-xx**.

            **3. Observação (2):**
            * **Ativação de Cadastro e Prioridade:** Na aba **Adm**, os Majores podem entrar com seu login e senha para ativar o Cadastro de novos Usuários, bem como Atribuir Prioridade a quem obter esse direito.
            * **Atualização de Dados:** Na aba **Recuperar**, gerar senha temporária. Copie ela e a use para fazer login normal.  Após o login, será aberta a ficha de cadastro contendo todos os dados do usuário, bastando alterar o que for necessário, inclusive a **Senha**, ressaltando que ela **não poderá iniciar com zero seguido apenas por números**.  Após isso, basta **Salvar** e o cadastro estará atualizado.
            * **Prioridade no Embarque:** A quem for atribuída a prioridade, terá embarque garantido no ônibus, desde que assinale no App a sua presença em tempo hábil; e o nome na lista aparecerá formatada em Azul..
            """)

        with t4:
            st.markdown("### 🔐 Recuperar acesso")
            st.caption("Confirme **E-mail + Telefone**. Será gerada uma **senha temporária** válida para **apenas 1 acesso** (expira em 10 minutos).")

            e_r = st.text_input("E-mail cadastrado:")
            raw_tel_rec = st.text_input("Telefone cadastrado:", value=st.session_state.get("_tel_rec_fmt", ""))
            fmt_tel_rec = tel_format_br(raw_tel_rec)
            st.session_state["_tel_rec_fmt"] = fmt_tel_rec

            rec_btn = st.button("👾 GERAR SENHA TEMPORÁRIA 👾", use_container_width=True)
            if rec_btn:
                if not e_r.strip():
                    st.error("Informe o e-mail cadastrado.")
                elif not tel_is_valid_11(fmt_tel_rec):
                    st.error("Telefone inválido. Use DDD + 9 dígitos (ex: 21987654321).")
                else:
                    tel_rec_digits = tel_only_digits(fmt_tel_rec)

                    row_idx, _ = find_user_row_by_email_tel(sheet_u_escrita, e_r, tel_rec_digits)

                    if row_idx:
                        senha_temp = gerar_senha_temp(10)
                        expira_dt = _br_now() + timedelta(minutes=10)
                        expira_str = _fmt_dt(expira_dt)

                        temp_cols = ensure_temp_cols(sheet_u_escrita)
                        gs_call(sheet_u_escrita.update_cell, row_idx, temp_cols["TEMP_SENHA"], senha_temp)
                        gs_call(sheet_u_escrita.update_cell, row_idx, temp_cols["TEMP_EXPIRA"], expira_str)
                        gs_call(sheet_u_escrita.update_cell, row_idx, temp_cols["TEMP_USADA"], "NAO")

                        buscar_usuarios_cadastrados.clear()
                        buscar_usuarios_admin.clear()

                        st.success("✅ Senha temporária gerada com sucesso.")
                        st.info(f"🔑 **Senha temporária:** {senha_temp}\n\n⏳ Expira em: {expira_str}\n\n⚠️ Válida para **apenas 1 acesso**.")
                        st.caption("Após entrar com a senha temporária, você será obrigado a atualizar seu cadastro (exceto e-mail).")
                    else:
                        st.error("Dados não encontrados (verifique e-mail e telefone).")

        with t5:
            with st.form("form_admin"):
                ad_u = st.text_input("Usuário ADM ou E-mail autorizado:")
                ad_s = st.text_input("Senha ADM:", type="password")
                entrou_adm = st.form_submit_button("☠️ ACESSAR PAINEL ☠️")
                if entrou_adm:
                    usuario_digitado = str(ad_u or "").strip()
                    senha_digitada = str(ad_s or "")

                    # ADM mestre: mantém o acesso antigo, com todas as permissões.
                    if usuario_digitado == "123" and senha_digitada == "123":
                        st.session_state.is_admin = True
                        st.session_state._admin_master = True
                        st.session_state._adm_first_load = True
                        st.rerun()
                    else:
                        # ADM autorizado: entra com o próprio e-mail e senha,
                        # desde que esteja ATIVO e marcado em ACESSO_ADM.
                        email_adm = usuario_digitado.lower()
                        adm_user = next(
                            (u for u in records_u_public
                             if str(u.get("Email", "")).strip().lower() == email_adm
                             and str(u.get("Senha", "")) == senha_digitada
                             and str(u.get("STATUS", "")).strip().upper() == "ATIVO"
                             and admin_acesso_ativo(u.get(ADMIN_HEADER, ""))),
                            None
                        )

                        if adm_user:
                            st.session_state.is_admin = True
                            st.session_state._admin_master = False
                            st.session_state._adm_first_load = True
                            st.session_state._admin_user_email = str(adm_user.get("Email", "")).strip().lower()
                            st.rerun()
                        else:
                            st.error("ADM inválido ou sem permissão de acesso ao painel.")

    # =========================================
    # PAINEL ADM
    # =========================================
    elif st.session_state.is_admin:
        st.header("🛡️ PAINEL ADMINISTRATIVO 🛡️")

        adm_flash = st.session_state.get("_adm_flash")
        if adm_flash:
            tipo_flash, texto_flash = adm_flash
            st.session_state._adm_flash = None
            if tipo_flash == "success":
                st.success(texto_flash)
            elif tipo_flash == "warning":
                st.warning(texto_flash)
            else:
                st.error(texto_flash)

        sair_btn = st.button("⬅️ SAIR DO PAINEL")
        if sair_btn:
            st.session_state.is_admin = False
            st.session_state._admin_master = False
            st.session_state._admin_user_email = ""
            st.session_state._adm_first_load = False
            st.rerun()

        if st.session_state._adm_first_load:
            buscar_usuarios_admin.clear()
            st.session_state._adm_first_load = False

        is_admin_master = bool(st.session_state.get("_admin_master", False))
        try:
            prioridade_col_idx = ensure_prioridade_col(sheet_u_escrita)
        except Exception:
            prioridade_col_idx = None

        try:
            admin_col_idx = ensure_admin_col(sheet_u_escrita)
        except Exception:
            admin_col_idx = None

        records_u = buscar_usuarios_admin()

        cA, cB = st.columns([1, 1])
        with cA:
            att_btn = st.button("🔄 Atualizar Usuários", use_container_width=True)
            if att_btn:
                buscar_usuarios_admin.clear()
                st.rerun()
        with cB:
            st.caption("ADM lê mais fresco (TTL=3s).")

        if is_admin_master:
            st.success("Acesso ADM mestre: todas as permissões liberadas, inclusive conceder/remover acesso ADM.")
        else:
            st.info("Acesso ADM autorizado: painel liberado, exceto conceder/remover acesso ADM de usuários.")

        st.subheader("⚙️ Configurações Globais")
        c_cfg1, c_cfg2 = st.columns([1, 1])
        with c_cfg1:
            novo_limite = st.number_input("Limite máximo de usuários cadastrados:", min_value=1, value=int(limite_max), step=1)
        with c_cfg2:
            nova_capacidade_onibus = st.number_input(
                "Capacidade do ônibus (vagas antes de excedente):",
                min_value=1,
                max_value=200,
                value=int(capacidade_onibus),
                step=1
            )

        salvar_lim = st.button("💾 SALVAR CONFIGURAÇÕES")
        if salvar_lim:
            sheet_c = ws_config()
            gs_call(sheet_c.update, "A1:B2", [["LIMITE", "CAPACIDADE_ONIBUS"], [str(int(novo_limite)), str(int(nova_capacidade_onibus))]])
            buscar_limite_dinamico.clear()
            buscar_capacidade_onibus_dinamica.clear()
            st.success("Configurações atualizadas!")
            st.rerun()

        st.divider()
        st.subheader("👥 Gestão de Usuários")

        c_pdf_users, c_pdf_info = st.columns([1, 1])
        with c_pdf_users:
            pdf_usuarios = gerar_pdf_usuarios_admin(records_u)
            st.download_button(
                "📄 PDF DOS USUÁRIOS CADASTRADOS",
                pdf_usuarios,
                "usuarios_cadastrados_rota_nova_iguacu.pdf",
                mime="application/pdf",
                use_container_width=True
            )
        with c_pdf_info:
            st.caption("Gera Graduação, Nome, Lotação, Origem e Telefone na ordem usada pela lista.")

        busca = st.text_input("🔍 Pesquisar por Nome ou E-mail:").strip().lower()

        ativar_all = st.button("✅ ATIVAR TODOS E DESLOGAR", use_container_width=True)
        if ativar_all:
            if records_u:
                start = 2
                end = len(records_u) + 1
                rng = f"H{start}:H{end}"
                gs_call(sheet_u_escrita.update, rng, [["ATIVO"]] * len(records_u))
                buscar_usuarios_admin.clear()
                buscar_usuarios_cadastrados.clear()
                st.session_state.clear()
                st.rerun()

        for i, user in enumerate(records_u):
            nome_user = str(user.get("Nome", "") or "").strip()
            email_user = str(user.get("Email", "") or "").strip()
            grad_user = str(user.get("Graduação", "") or "").strip()
            lot_user = str(user.get("Lotação", "") or "").strip()
            orig_user = str(user.get("QG_RMCF_OUTROS", "") or user.get("ORIGEM", "") or "").strip()

            # A pesquisa continua buscando por nome/e-mail e também passa a encontrar por lotação/origem.
            texto_busca_user = f"{grad_user} {nome_user} {email_user} {lot_user} {orig_user}".lower()

            if busca == "" or busca in texto_busca_user:
                status = str(user.get("STATUS", "")).upper()
                pri_txt = " | PRIORIDADE" if prioridade_ativa(user.get(PRIORIDADE_HEADER, "")) else ""
                adm_txt = " | ADM" if (is_admin_master and admin_acesso_ativo(user.get(ADMIN_HEADER, ""))) else ""

                lot_exibir = lot_user if lot_user else "Sem lotação"
                orig_exibir = orig_user if orig_user else "Sem origem"
                titulo_usuario = f"{grad_user} {nome_user} | {lot_exibir} | {orig_exibir} - {status}{pri_txt}{adm_txt}"

                with st.expander(titulo_usuario):
                    if is_admin_master:
                        c1, c2, c3, c4, c5 = st.columns([2, 1, 1, 1, 1])
                    else:
                        c1, c2, c3, c5 = st.columns([2, 1, 1, 1])

                    c1.write(f"📧 {user.get('Email')} | 📱 {user.get('TELEFONE')}")
                    is_ativo = (status == "ATIVO")
                    is_prioridade = prioridade_ativa(user.get(PRIORIDADE_HEADER, ""))
                    is_acesso_adm = admin_acesso_ativo(user.get(ADMIN_HEADER, ""))

                    new_val = c2.checkbox("Liberar", value=is_ativo, key=f"adm_chk_{i}")
                    if new_val != is_ativo:
                        gs_call(sheet_u_escrita.update_cell, i + 2, 8, "ATIVO" if new_val else "INATIVO")
                        buscar_usuarios_admin.clear()
                        buscar_usuarios_cadastrados.clear()
                        st.rerun()

                    pri_val = c3.checkbox("Prioridade", value=is_prioridade, key=f"adm_pri_{i}")
                    if pri_val != is_prioridade:
                        if prioridade_col_idx is None:
                            prioridade_col_idx = ensure_prioridade_col(sheet_u_escrita)
                        gs_call(sheet_u_escrita.update_cell, i + 2, prioridade_col_idx, "SIM" if pri_val else "NAO")
                        buscar_usuarios_admin.clear()
                        buscar_usuarios_cadastrados.clear()
                        st.rerun()

                    if is_admin_master:
                        adm_val = c4.checkbox("Acesso ADM", value=is_acesso_adm, key=f"adm_access_{i}")
                        if adm_val != is_acesso_adm:
                            if admin_col_idx is None:
                                admin_col_idx = ensure_admin_col(sheet_u_escrita)
                            gs_call(sheet_u_escrita.update_cell, i + 2, admin_col_idx, "SIM" if adm_val else "NAO")
                            buscar_usuarios_admin.clear()
                            buscar_usuarios_cadastrados.clear()
                            st.rerun()

                    del_btn = c5.button("🗑️", key=f"del_{i}")
                    if del_btn:
                        gs_call(sheet_u_escrita.delete_rows, i + 2)
                        buscar_usuarios_admin.clear()
                        buscar_usuarios_cadastrados.clear()
                        st.rerun()

                    # Tanto o ADM mestre quanto o ADM autorizado podem alterar
                    # Graduação, Nome, Lotação e Origem do usuário.
                    graduacao_planilha = str(grad_user or "").strip().upper()
                    graduacao_para_select = (
                        graduacao_planilha
                        if graduacao_planilha in GRADUACOES_VALIDAS
                        else "SD"
                    )
                    graduacao_index = GRADUACOES_VALIDAS.index(graduacao_para_select)

                    origem_planilha = str(orig_user or "").strip().upper()
                    origem_para_select = (
                        origem_planilha
                        if origem_planilha in ORIGENS_VALIDAS
                        else "QG"
                    )
                    origem_index = ORIGENS_VALIDAS.index(origem_para_select)

                    st.markdown("**✏️ Alterar dados cadastrais**")
                    with st.form(f"adm_editar_dados_{i}"):
                        c_grad, c_origem = st.columns([1, 1])
                        nova_graduacao_adm = c_grad.selectbox(
                            "Graduação:",
                            GRADUACOES_VALIDAS,
                            index=graduacao_index
                        )
                        nova_origem_adm = c_origem.selectbox(
                            "Origem:",
                            ORIGENS_VALIDAS,
                            index=origem_index
                        )

                        novo_nome_adm = st.text_input(
                            "Nome de Escala:",
                            value=nome_user
                        )
                        nova_lotacao_adm = st.text_input(
                            "Lotação:",
                            value=lot_user
                        )

                        salvar_dados_btn = st.form_submit_button(
                            "💾 SALVAR DADOS DO USUÁRIO",
                            use_container_width=True
                        )

                    if salvar_dados_btn:
                        graduacao_nova_norm = str(nova_graduacao_adm or "").strip().upper()
                        nome_novo_norm = str(novo_nome_adm or "").strip()
                        lotacao_nova_norm = str(nova_lotacao_adm or "").strip()
                        origem_nova_norm = str(nova_origem_adm or "").strip().upper()

                        campos_invalidos = []
                        if graduacao_nova_norm not in GRADUACOES_VALIDAS:
                            campos_invalidos.append("Graduação")
                        if not nome_novo_norm:
                            campos_invalidos.append("Nome de Escala")
                        if not lotacao_nova_norm:
                            campos_invalidos.append("Lotação")
                        if origem_nova_norm not in ORIGENS_VALIDAS:
                            campos_invalidos.append("Origem")

                        if campos_invalidos:
                            st.error(
                                "Preencha corretamente: "
                                + ", ".join(campos_invalidos)
                                + "."
                            )
                        else:
                            dados_anteriores = (
                                graduacao_planilha,
                                str(nome_user or "").strip(),
                                str(lot_user or "").strip(),
                                origem_planilha,
                            )
                            dados_novos = (
                                graduacao_nova_norm,
                                nome_novo_norm,
                                lotacao_nova_norm,
                                origem_nova_norm,
                            )

                            if dados_novos == dados_anteriores:
                                st.info("Nenhuma alteração foi identificada nos dados deste usuário.")
                            else:
                                alterou, status_dados, qtd_presencas = atualizar_dados_usuario_admin(
                                    sheet_u_escrita,
                                    ws_presenca(),
                                    email_user,
                                    graduacao_nova_norm,
                                    nome_novo_norm,
                                    lotacao_nova_norm,
                                    origem_nova_norm,
                                )

                                if alterou:
                                    complemento = (
                                        " A lista de presença atual também foi sincronizada."
                                        if qtd_presencas > 0
                                        else ""
                                    )
                                    st.session_state._adm_flash = (
                                        "success",
                                        f"✅ Dados de {graduacao_nova_norm} {nome_novo_norm} atualizados com sucesso.{complemento}"
                                    )
                                    st.rerun()
                                elif status_dados in {"SISTEMA_OCUPADO", "TROCA_EM_ANDAMENTO"}:
                                    st.warning(
                                        "O sistema está concluindo outra operação ou uma troca de ciclo. "
                                        "Aguarde alguns segundos e tente salvar novamente."
                                    )
                                elif status_dados == "USUARIO_NAO_ENCONTRADO":
                                    st.error(
                                        "O usuário não foi localizado na planilha. "
                                        "Atualize a relação e tente novamente."
                                    )
                                elif status_dados in {
                                    "COLUNA_EMAIL_NAO_ENCONTRADA",
                                    "COLUNAS_CADASTRO_NAO_ENCONTRADAS",
                                }:
                                    st.error(
                                        "Uma ou mais colunas do cadastro não foram localizadas na aba Usuarios. "
                                        "Verifique os cabeçalhos Nome, Graduação, Lotação, QG_RMCF_OUTROS/ORIGEM e Email."
                                    )
                                elif status_dados == "GRADUACAO_INVALIDA":
                                    st.error("A graduação selecionada não é válida.")
                                elif status_dados == "NOME_INVALIDO":
                                    st.error("O Nome de Escala não pode ficar vazio.")
                                elif status_dados == "LOTACAO_INVALIDA":
                                    st.error("A Lotação não pode ficar vazia.")
                                elif status_dados == "ORIGEM_INVALIDA":
                                    st.error("A origem selecionada não é válida.")
                                else:
                                    st.error(
                                        "Não foi possível atualizar os dados do usuário agora. "
                                        "Tente novamente."
                                    )

    # =========================================
    # USUÁRIO LOGADO
    # =========================================
    else:
        u = st.session_state.usuario_logado

        # ==========================================================
        # NOVO: FORÇAR ATUALIZAÇÃO COMPLETA DO CADASTRO APÓS LOGIN COM SENHA TEMP
        # (e-mail NÃO pode ser alterado)
        # ==========================================================
        if st.session_state.get("_force_profile_update", False):
            st.warning("🔐 Você entrou com uma **senha temporária**. Atualize agora seu **cadastro completo** (o e-mail não pode ser alterado).")

            # tenta localizar linha
            row_idx = st.session_state.get("_profile_update_row")
            if row_idx is None:
                try:
                    row_idx, _ = find_user_row_by_email_tel(sheet_u_escrita, u.get("Email", ""), u.get("TELEFONE", ""))
                except Exception:
                    row_idx = None

            # Pré-preenche com dados atuais
            nome_atual = str(u.get("Nome", "") or "")
            grad_atual = str(u.get("Graduação", "") or "SD")
            lot_atual = str(u.get("Lotação", "") or "")
            orig_atual = str(u.get("QG_RMCF_OUTROS", "") or u.get("ORIGEM", "") or "QG")
            tel_atual_fmt = tel_format_br(str(u.get("TELEFONE", "") or ""))

            grads = GRADUACOES_VALIDAS
            origs = ORIGENS_VALIDAS

            try:
                grad_idx = grads.index(str(grad_atual).strip()) if str(grad_atual).strip() in grads else grads.index("SD")
            except Exception:
                grad_idx = grads.index("SD")

            try:
                orig_idx = origs.index(str(orig_atual).strip().upper()) if str(orig_atual).strip().upper() in origs else origs.index("QG")
            except Exception:
                orig_idx = origs.index("QG")

            with st.form("form_atualizar_cadastro_temp"):
                st.text_input("E-mail (não pode alterar):", value=str(u.get("Email", "") or ""), disabled=True)

                novo_nome = st.text_input("Nome de Escala:", value=nome_atual)
                novo_grad = st.selectbox("Graduação:", grads, index=grad_idx)
                novo_lot = st.text_input("Lotação:", value=lot_atual)

                raw_tel_up = st.text_input("Telefone:", value=st.session_state.get("_tel_up_fmt", tel_atual_fmt))
                fmt_tel_up = tel_format_br(raw_tel_up)
                st.session_state["_tel_up_fmt"] = fmt_tel_up

                novo_orig = st.selectbox("Origem:", origs, index=orig_idx)

                st.markdown("#### 🔑 Nova senha")
                nova1 = st.text_input("Nova senha:", type="password")
                nova2 = st.text_input("Confirmar nova senha:", type="password")

                ok_btn = st.form_submit_button("💾 SALVAR ATUALIZAÇÃO", use_container_width=True)

            if ok_btn:
                def norm_str(x):
                    return str(x or "").strip()

                n_ok = bool(norm_str(novo_nome))
                l_ok = bool(norm_str(novo_lot))
                p_ok = bool(norm_str(nova1))

                missing = []
                if not n_ok: missing.append("Nome de Escala")
                if not tel_is_valid_11(fmt_tel_up): missing.append("Telefone (inválido)")
                if not norm_str(novo_grad): missing.append("Graduação")
                if not l_ok: missing.append("Lotação")
                if not norm_str(novo_orig): missing.append("Origem")
                if not p_ok: missing.append("Nova senha")

                if missing:
                    st.error("Preencha corretamente: " + ", ".join(missing) + ".")
                elif nova1 != nova2:
                    st.error("As senhas não conferem.")
                else:
                    try:
                        if not row_idx:
                            st.error("Não foi possível localizar seu usuário na planilha para atualizar o cadastro.")
                        else:
                            # Regra: telefone não pode colidir com outro usuário (exceto ele mesmo)
                            tel_new_digits = tel_only_digits(fmt_tel_up)
                            email_log = str(u.get("Email", "")).strip().lower()

                            # busca registros mais recentes para validar duplicidade
                            records_check = buscar_usuarios_cadastrados()
                            tel_colide = False
                            for uu in records_check:
                                em2 = str(uu.get("Email", "")).strip().lower()
                                if em2 == email_log:
                                    continue
                                if tel_only_digits(uu.get("TELEFONE", "")) == tel_new_digits:
                                    tel_colide = True
                                    break

                            if tel_colide:
                                st.error("Este telefone já está cadastrado para outro usuário.")
                            else:
                                # Atualiza colunas no layout do seu append_row:
                                # 1 Nome | 2 Graduação | 3 Lotação | 4 Senha | 5 Origem | 6 Email | 7 Telefone | 8 STATUS
                                gs_call(sheet_u_escrita.update_cell, row_idx, 1, norm_str(novo_nome))
                                gs_call(sheet_u_escrita.update_cell, row_idx, 2, norm_str(novo_grad))
                                gs_call(sheet_u_escrita.update_cell, row_idx, 3, norm_str(novo_lot))
                                gs_call(sheet_u_escrita.update_cell, row_idx, 4, norm_str(nova1))
                                gs_call(sheet_u_escrita.update_cell, row_idx, 5, norm_str(novo_orig))
                                gs_call(sheet_u_escrita.update_cell, row_idx, 7, fmt_tel_up)

                                # Finaliza token TEMP: marca como usado e limpa
                                temp_cols = ensure_temp_cols(sheet_u_escrita)
                                gs_call(sheet_u_escrita.update_cell, row_idx, temp_cols["TEMP_SENHA"], "")
                                gs_call(sheet_u_escrita.update_cell, row_idx, temp_cols["TEMP_EXPIRA"], "")
                                gs_call(sheet_u_escrita.update_cell, row_idx, temp_cols["TEMP_USADA"], "SIM")

                                buscar_usuarios_cadastrados.clear()
                                buscar_usuarios_admin.clear()

                                # Atualiza sessão local
                                st.session_state.usuario_logado["Nome"] = norm_str(novo_nome)
                                st.session_state.usuario_logado["Graduação"] = norm_str(novo_grad)
                                st.session_state.usuario_logado["Lotação"] = norm_str(novo_lot)
                                st.session_state.usuario_logado["Senha"] = norm_str(nova1)
                                st.session_state.usuario_logado["QG_RMCF_OUTROS"] = norm_str(novo_orig)
                                st.session_state.usuario_logado["TELEFONE"] = fmt_tel_up

                                st.session_state._force_profile_update = False
                                st.session_state._profile_update_row = None
                                st.session_state._login_kind = "REAL"

                                st.success("✅ Cadastro atualizado. Você já pode usar o sistema normalmente.")
                                st.rerun()
                    except Exception as ex:
                        st.error(f"Falha ao atualizar cadastro: {ex}")

            st.stop()


        st.sidebar.markdown("### 👤 Usuário Conectado 🙍‍♂️")
        st.sidebar.info(f"**{u.get('Graduação')} {u.get('Nome')}**")

        flash_operacao = st.session_state.get("_flash_operacao")
        if flash_operacao:
            tipo_flash, texto_flash = flash_operacao
            st.session_state._flash_operacao = None
            if tipo_flash == "success":
                st.success(texto_flash)
            elif tipo_flash == "warning":
                st.warning(texto_flash)
            else:
                st.error(texto_flash)

        sair_user = st.sidebar.button("⬅️ Sair", use_container_width=True)
        if sair_user:
            for key in list(st.session_state.keys()):
                del st.session_state[key]
            st.rerun()

        st.sidebar.markdown("---")
        menu_usuario = st.sidebar.radio("Menu", ["Lista Atual", "Histórico"], index=0)
        st.sidebar.markdown("---")
        st.sidebar.caption("Desenvolvido e atualizado em 2026 por...:      MAJ ANDRÉ AGUIAR - 3ª DPJM®️")

        sheet_p_escrita = ws_presenca()

        agora_monotonic = time_module.monotonic()
        usar_leitura_direta = (
            bool(st.session_state._force_refresh_presenca)
            or bool(st.session_state._confirmar_exclusao_presenca)
            or bool(st.session_state._presenca_aguardando_confirmacao)
            or agora_monotonic < float(st.session_state._leitura_direta_presenca_ate or 0.0)
        )

        if usar_leitura_direta:
            # Atualização direta apenas para esta sessão. Não limpa o cache
            # global e, portanto, não provoca avalanche de leituras nas demais.
            # É obrigatória durante a confirmação/exclusão para que nenhum
            # rerun utilize uma lista anterior à gravação recém-realizada.
            dados_p = gs_call(
                sheet_p_escrita.get_all_values,
                _max_tries=2,
                _max_sleep=1.0
            )
            st.session_state._force_refresh_presenca = False
        else:
            dados_p = buscar_presenca_atualizada()
        dados_p_show = filtrar_linhas_presenca(dados_p)

        aberto, janela_conf = verificar_status_e_limpar(sheet_p_escrita, dados_p_show)

        if menu_usuario == "Histórico":
            render_historico_page()
            st.stop()

        df_o, df_v = pd.DataFrame(), pd.DataFrame()
        ja, pos = False, 999

        if dados_p_show and len(dados_p_show) > 1:
            df_o, df_v = aplicar_ordenacao(pd.DataFrame(dados_p_show[1:], columns=dados_p_show[0]), capacidade_onibus, emails_prioridade_lista)
            email_logado = str(u.get("Email")).strip().lower()
            ja = any(email_logado == str(row.get("EMAIL", "")).strip().lower() for _, row in df_o.iterrows())
            if ja:
                pos_idx = df_o.index[df_o["EMAIL"].str.lower() == email_logado].tolist()[0]
                pos = str(df_o.loc[pos_idx, "Nº"])

        # A presença recém-gravada só é considerada pronta para exclusão depois
        # que esta leitura direta comprovar que o e-mail já existe no Sheets.
        presenca_confirmada_nesta_execucao = bool(
            ja and st.session_state._presenca_aguardando_confirmacao
        )
        if presenca_confirmada_nesta_execucao:
            st.session_state._presenca_aguardando_confirmacao = False

        if ja or st.session_state._confirmar_exclusao_presenca:
            if ja:
                st.success(f"✅ Presença registrada: {pos}")
            else:
                # Mantém o fluxo de confirmação visível mesmo diante de uma
                # leitura transitória. O botão SIM fará nova leitura direta.
                st.warning("A presença está sendo validada diretamente no Google Sheets.")

            # ==========================================================
            # EXCLUSÃO SEGURA:
            # - botão só é liberado após confirmação direta da gravação;
            # - abrir a confirmação força novas leituras diretas;
            # - a caixa não desaparece por causa de cache antigo.
            # ==========================================================
            if not st.session_state._confirmar_exclusao_presenca:
                exc_btn = st.button(
                    "❌ EXCLUIR MINHA PRESENÇA ⚠️",
                    use_container_width=True,
                    key="btn_excluir_presenca",
                    disabled=bool(st.session_state._presenca_aguardando_confirmacao)
                )

                if st.session_state._presenca_aguardando_confirmacao:
                    st.caption("Aguardando o Google Sheets confirmar a gravação da presença.")

                if exc_btn:
                    st.session_state._confirmar_exclusao_presenca = True
                    st.session_state._force_refresh_presenca = True
                    st.session_state._leitura_direta_presenca_ate = time_module.monotonic() + 15.0
                    st.rerun()

            if st.session_state._confirmar_exclusao_presenca:
                st.warning("⚠️ Você realmente deseja **excluir sua presença**?")

                c_sim, c_nao, c_cancelar = st.columns(3)

                with c_sim:
                    sim_btn = st.button("✅ SIM", use_container_width=True, key="btn_confirmar_exclusao_sim")
                with c_nao:
                    nao_btn = st.button("❌ NÃO", use_container_width=True, key="btn_confirmar_exclusao_nao")
                with c_cancelar:
                    cancel_btn = st.button("🚫 CANCELAR", use_container_width=True, key="btn_confirmar_exclusao_cancelar")

                if nao_btn or cancel_btn:
                    st.session_state._confirmar_exclusao_presenca = False
                    st.session_state._force_refresh_presenca = True
                    st.session_state._leitura_direta_presenca_ate = time_module.monotonic() + 5.0
                    st.rerun()

                if sim_btn:
                    email_logado = str(u.get("Email")).strip().lower()
                    excluiu, status_exclusao = excluir_presenca_por_email(sheet_p_escrita, email_logado)

                    if excluiu:
                        st.session_state._confirmar_exclusao_presenca = False
                        st.session_state._presenca_aguardando_confirmacao = False
                        st.session_state._force_refresh_presenca = True
                        st.session_state._leitura_direta_presenca_ate = time_module.monotonic() + 10.0
                        st.session_state._flash_operacao = ("success", "✅ Presença excluída com sucesso.")
                        st.rerun()
                    elif status_exclusao in {"SISTEMA_OCUPADO", "TROCA_EM_ANDAMENTO"}:
                        st.warning("O sistema está concluindo outra operação. Aguarde alguns segundos e tente novamente.")
                    elif status_exclusao == "NAO_ENCONTRADA":
                        st.session_state._confirmar_exclusao_presenca = False
                        st.session_state._presenca_aguardando_confirmacao = False
                        st.session_state._force_refresh_presenca = True
                        st.session_state._leitura_direta_presenca_ate = time_module.monotonic() + 10.0
                        st.session_state._flash_operacao = ("warning", "Sua presença já não estava mais na lista.")
                        st.rerun()
                    elif status_exclusao == "EXCLUSAO_NAO_CONFIRMADA":
                        st.error(
                            "A exclusão foi solicitada, mas ainda não pôde ser confirmada no Google Sheets. "
                            "A confirmação permanecerá aberta para uma nova tentativa segura."
                        )
                    else:
                        st.error("Não foi possível excluir sua presença agora. Atualize e tente novamente.")

        elif st.session_state._presenca_aguardando_confirmacao:
            st.info("⏳ Confirmando sua presença diretamente no Google Sheets...")
            _ = st.button(
                "❌ EXCLUIR MINHA PRESENÇA ⚠️",
                use_container_width=True,
                key="btn_excluir_presenca_aguardando",
                disabled=True
            )
            confirmar_btn = st.button(
                "🔄 ATUALIZAR E CONFIRMAR PRESENÇA",
                use_container_width=True,
                key="btn_atualizar_confirmacao_presenca"
            )
            if confirmar_btn:
                st.session_state._force_refresh_presenca = True
                st.session_state._leitura_direta_presenca_ate = time_module.monotonic() + 10.0
                st.rerun()

        elif aberto:
            salvar_btn = st.button("🚀 CONFIRMAR MINHA PRESENÇA ✅", use_container_width=True)
            if salvar_btn:
                gravou, status_gravacao = registrar_presenca_se_ausente(sheet_p_escrita, u)
                if gravou:
                    # Não libera a exclusão com base apenas no retorno do append.
                    # O próximo rerun fará leitura direta e só então confirmará
                    # que a presença realmente existe no Google Sheets.
                    st.session_state._confirmar_exclusao_presenca = False
                    st.session_state._presenca_aguardando_confirmacao = True
                    st.session_state._force_refresh_presenca = True
                    st.session_state._leitura_direta_presenca_ate = time_module.monotonic() + 12.0
                    st.rerun()
                elif status_gravacao == "JA_REGISTRADA":
                    st.session_state._force_refresh_presenca = True
                    st.session_state._flash_operacao = ("success", "✅ Sua presença já está registrada.")
                    st.rerun()
                elif status_gravacao == "LISTA_FECHADA":
                    st.warning("A lista foi fechada antes da conclusão da operação. Atualize a página.")
                elif status_gravacao in {"TROCA_EM_ANDAMENTO", "TROCA_PENDENTE", "SISTEMA_OCUPADO"}:
                    st.warning(
                        "O sistema está concluindo a troca de ciclo ou outra inscrição. "
                        "Aguarde alguns segundos e toque novamente em CONFIRMAR."
                    )
                else:
                    st.error("Não foi possível registrar sua presença agora. Atualize e tente novamente.")
        else:
            st.info("⌛ Lista fechada para novas inscrições.")

            # ==========================================================
            # ATUALIZAR DISPONÍVEL MESMO COM LISTA FECHADA
            # ==========================================================
            up_btn_fechado = st.button("🔄 ATUALIZAR", use_container_width=True, key="up_btn_fechado")
            if up_btn_fechado:
                st.session_state._force_refresh_presenca = True
                st.rerun()

        # CONFERÊNCIA
        if ja and janela_conf:
            st.divider()
            st.subheader("📋 LISTA DE EMBARQUE 📋")
            painel_btn = st.button("✍️ CONFERÊNCIA ✍️", use_container_width=True)
            if painel_btn:
                st.session_state.conf_ativa = not st.session_state.conf_ativa

            if st.session_state.conf_ativa and (dados_p_show and len(dados_p_show) > 1):
                for i, row in df_o.iterrows():
                    label = f"{row.get('Nº','')} - {row.get('GRADUAÇÃO','')} {row.get('NOME','')} - {row.get('LOTAÇÃO','')}".strip()
                    _ = st.checkbox(label if label else " ", key=f"chk_p_{i}")

        if dados_p_show and len(dados_p_show) > 1:
            insc = len(df_o)
            rest = capacidade_onibus - insc
            st.subheader(f"Inscritos: {insc} | Vagas: {capacidade_onibus} | {'Sobra' if rest >= 0 else 'Exc'}: {abs(rest)}")

            c_up1, c_up2 = st.columns([1, 1])
            with c_up1:
                up_btn = st.button("🔄 ATUALIZAR", use_container_width=True, key="up_btn_tabela")
                if up_btn:
                    st.session_state._force_refresh_presenca = True
                    st.rerun()
            with c_up2:
                st.caption("Atualiza sob demanda.")

            # ==========================================================
            # ALTERAÇÃO SOLICITADA (TELA):
            # 1) Zebra (linhas alternadas) via CSS na classe 'presenca-zebra'
            # 2) Nome em negrito (coluna NOME) sem quebrar excedentes (span vermelho)
            # ==========================================================
            df_v_show = df_v.copy()
            if "NOME" in df_v_show.columns:
                df_v_show["NOME"] = df_v_show["NOME"].apply(lambda x: f"<b>{x}</b>")

            st.write(
                f"<div class='tabela-responsiva'>"
                f"{colunas_para_exibir(df_v_show).to_html(index=False, justify='center', border=0, escape=False, classes='presenca-zebra')}"
                f"</div>",
                unsafe_allow_html=True
            )

            c1, c2 = st.columns(2)
            with c1:
                resumo = {"inscritos": insc, "vagas": capacidade_onibus}
                pdf_bytes = gerar_pdf_apresentado(df_o, resumo)
                _ = st.download_button(
                    "📄 PDF (Relatório)",
                    pdf_bytes,
                    "lista_rota_nova_iguacu.pdf",
                    use_container_width=True
                )

            with c2:
                txt_w = "*🚌 LISTA DE PRESENÇA*\n\n"
                for _, r in df_o.iterrows():
                    txt_w += f"{r['Nº']}. {r['GRADUAÇÃO']} {r['NOME']} - {r['LOTAÇÃO']}\n"
                st.markdown(
                    f'<a href="https://wa.me/?text={urllib.parse.quote(txt_w)}" target="_blank">'
                    f"<button style='width:100%; height:38px; background-color:#25D366; color:white; border:none; "
                    f"border-radius:4px; font-weight:bold;'>🟢 WHATSAPP</button></a>",
                    unsafe_allow_html=True
                )

    st.markdown('<div class="footer">Desenvolvido e atualizado em 2026 por...:      <b>MAJ ANDRÉ AGUIAR - 3ª DPJM®️</b></div>', unsafe_allow_html=True)

    # ==========================================================
    # GIF NO FINAL DA PÁGINA
    #  - 20% menor => width:80%
    # ==========================================================
    st.markdown(
        f"""
        <div style="width:100%; text-align:center; margin-top:12px;">
            <img src="{GIF_URL}" style="width:80%; max-width:520px; height:auto;" />
        </div>
        """,
        unsafe_allow_html=True
    )

except Exception as e:
    st.error(f"⚠️ Erro: {e}")
