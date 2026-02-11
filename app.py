import streamlit as st
import gspread
from gspread.exceptions import APIError
from google.oauth2.service_account import Credentials
import pandas as pd
from datetime import datetime, time, timedelta
import pytz
from fpdf import FPDF
import urllib.parse
import time as time_module
import random
import re

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

FUSO_BR = pytz.timezone("America/Sao_Paulo")

# ==========================================================
# GIF NO FINAL DA PÁGINA (alteração solicitada)
# ==========================================================
GIF_URL = "https://www.imagensanimadas.com/data/media/425/onibus-imagem-animada-0024.gif"


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
# WRAPPER COM RETRY / BACKOFF PARA 429
# ==========================================================
def gs_call(func, *args, **kwargs):
    max_tries = 6
    base = 0.6
    for attempt in range(max_tries):
        try:
            return func(*args, **kwargs)
        except APIError as e:
            msg = str(e)
            is_429 = ("429" in msg) or ("Quota exceeded" in msg) or ("RESOURCE_EXHAUSTED" in msg)
            is_5xx = any(code in msg for code in ["500", "502", "503", "504"])
            if is_429 or is_5xx:
                sleep_s = (base * (2 ** attempt)) + random.uniform(0.0, 0.35)
                time_module.sleep(min(sleep_s, 6.0))
                continue
            raise
    raise APIError("Google Sheets: muitas requisições (429). Tente novamente em instantes.")


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
    try:
        return gs_call(doc.worksheet, WS_CONFIG)
    except Exception:
        sheet_c = gs_call(doc.add_worksheet, title=WS_CONFIG, rows="10", cols="5")
        gs_call(sheet_c.update, "A1:A2", [["LIMITE"], ["100"]])
        return sheet_c


# ==========================================================
# SENHA TEMPORÁRIA (1 acesso) - RECUPERAÇÃO SEGURA
# ==========================================================
TEMP_HEADERS = ["TEMP_SENHA", "TEMP_EXPIRA", "TEMP_USADA"]

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


# ==========================================================
# LEITURAS (CACHE_DATA)
# ==========================================================
@st.cache_data(ttl=30)
def buscar_usuarios_cadastrados():
    """Uso geral (Login/Cadastro/Recuperar)."""
    try:
        sheet_u = ws_usuarios()
        return gs_call(sheet_u.get_all_records)
    except Exception:
        return []

@st.cache_data(ttl=3)
def buscar_usuarios_admin():
    """Uso específico do ADM: mais fresco."""
    try:
        sheet_u = ws_usuarios()
        return gs_call(sheet_u.get_all_records)
    except Exception:
        return []

@st.cache_data(ttl=120)
def buscar_limite_dinamico():
    try:
        sheet_c = ws_config()
        val = gs_call(sheet_c.acell, "A2").value
        return int(val)
    except Exception:
        return 100

@st.cache_data(ttl=6)
def buscar_presenca_atualizada():
    try:
        sheet_p = ws_presenca()
        return gs_call(sheet_p.get_all_values)
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


def verificar_status_e_limpar(sheet_p, dados_p):
    agora = datetime.now(FUSO_BR)
    hora_atual, dia_semana = agora.time(), agora.weekday()

    if hora_atual >= time(18, 50):
        marco = agora.replace(hour=18, minute=50, second=0, microsecond=0)
    elif hora_atual >= time(6, 50):
        marco = agora.replace(hour=6, minute=50, second=0, microsecond=0)
    else:
        marco = (agora - timedelta(days=1)).replace(hour=18, minute=50, second=0, microsecond=0)

    if dados_p and len(dados_p) > 1:
        try:
            ultima_str = dados_p[-1][0]
            ultima_dt = FUSO_BR.localize(datetime.strptime(ultima_str, "%d/%m/%Y %H:%M:%S"))
            if ultima_dt < marco:
                gs_call(sheet_p.resize, rows=1)
                gs_call(sheet_p.resize, rows=100)
                st.session_state["_force_refresh_presenca"] = True
                st.rerun()
        except Exception:
            pass

    # Regras de abertura/fechamento:
    # - SEG a QUI: fecha apenas nas janelas 05:00-07:00 e 17:00-19:00
    # - SEX: fecha às 17:00 e só reabre DOM às 19:00 (portanto SEX após 17:00 fica fechado)
    # - SÁB: fechado o dia todo
    # - DOM: abre a partir de 19:00
    if dia_semana == 5:  # Sábado
        is_aberto = False
    elif dia_semana == 6:  # Domingo
        is_aberto = (hora_atual >= time(19, 0))
    elif dia_semana == 4:  # Sexta
        if hora_atual >= time(17, 0):
            is_aberto = False
        elif time(5, 0) <= hora_atual < time(7, 0):
            is_aberto = False
        else:
            is_aberto = True
    else:  # Segunda a Quinta
        if (time(5, 0) <= hora_atual < time(7, 0)) or (time(17, 0) <= hora_atual < time(19, 0)):
            is_aberto = False
        else:
            is_aberto = True

    janela_conferencia = (time(5, 0) < hora_atual < time(7, 0)) or (time(17, 0) < hora_atual < time(19, 0))
    return is_aberto, janela_conferencia


# ==========================================================
# CICLO (exibição abaixo do título)
# ==========================================================
def obter_ciclo_atual():
    agora = datetime.now(FUSO_BR)
    t = agora.time()
    wd = agora.weekday()

    em_fechamento_fds = (wd == 4 and t >= time(17, 0)) or (wd == 5) or (wd == 6 and t < time(19, 0))
    if em_fechamento_fds:
        # Próximo ciclo: 06:30 da próxima segunda-feira
        dias_para_seg = (7 - wd) % 7  # sex->3, sáb->2, dom->1
        alvo_dt = (agora + timedelta(days=dias_para_seg)).date()
        alvo_h = "06:30"
    else:
        if t >= time(19, 0):
            alvo_dt = (agora + timedelta(days=1)).date()
            alvo_h = "06:30"
        elif t < time(7, 0):
            alvo_dt = agora.date()
            alvo_h = "06:30"
        else:
            alvo_dt = agora.date()
            alvo_h = "18:30"

    alvo_dt_str = alvo_dt.strftime("%d/%m/%Y")
    return alvo_h, alvo_dt_str


def aplicar_ordenacao(df):
    if "EMAIL" not in df.columns:
        df["EMAIL"] = "N/A"

    # Garantia: coluna QG_RMCF_OUTROS deve existir na planilha de presença
    if "QG_RMCF_OUTROS" not in df.columns and "ORIGEM" in df.columns:
        df["QG_RMCF_OUTROS"] = df["ORIGEM"]
    if "QG_RMCF_OUTROS" not in df.columns:
        df["QG_RMCF_OUTROS"] = ""

    # Prioridades
    p_orig = {"QG": 1, "RMCF": 2, "OUTROS": 3}

    # Ordem solicitada (grupo "normal")
    p_grad_normal = {
        "TCEL": 1, "MAJ": 2, "CAP": 3, "1º TEN": 4, "2º TEN": 5, "SUBTEN": 6,
        "1º SGT": 7, "2º SGT": 8, "3º SGT": 9, "CB": 10, "SD": 11
    }

    # Grupo FC: primeiro FC COM (grupo 1), depois FC TER (grupo 2)
    def grupo_fc(grad):
        g = str(grad or "").strip().upper()
        if g == "FC COM":
            return 1
        if g == "FC TER":
            return 2
        return 0

    df["grupo_fc"] = df["GRADUAÇÃO"].apply(grupo_fc)

    # Origem sempre: QG -> RMCF -> OUTROS (dentro de cada grupo)
    df["p_o"] = df["QG_RMCF_OUTROS"].map(p_orig).fillna(99)

    # Para o grupo normal, aplica ordem de graduação; para FC, deixa 0 (desempate por dt)
    def p_grad(row):
        if int(row.get("grupo_fc", 0)) == 0:
            return p_grad_normal.get(str(row.get("GRADUAÇÃO", "")).strip().upper(), 999)
        return 0

    df["p_g"] = df.apply(p_grad, axis=1)

    # Desempate por quem entrou primeiro
    df["dt"] = pd.to_datetime(df["DATA_HORA"], dayfirst=True, errors="coerce")

    # Ordenação final conforme regra
    df = df.sort_values(by=["grupo_fc", "p_o", "p_g", "dt"]).reset_index(drop=True)

    df.insert(0, "Nº", [str(i + 1) if i < 38 else f"Exc-{i - 37:02d}" for i in range(len(df))])

    df_v = df.copy()
    for i, r in df_v.iterrows():
        if "Exc-" in str(r["Nº"]):
            for c in df_v.columns:
                df_v.at[i, c] = f"<span style='color:#d32f2f; font-weight:bold;'>{r[c]}</span>"

    return df.drop(columns=["grupo_fc", "p_o", "p_g", "dt"]), df_v.drop(columns=["grupo_fc", "p_o", "p_g", "dt"])


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


def gerar_pdf_apresentado(df_o: pd.DataFrame, resumo: dict) -> bytes:
    agora = datetime.now(FUSO_BR).strftime("%d/%m/%Y %H:%M:%S")
    sub = f"Emitido em: {agora}"

    pdf = PDFRelatorio(titulo="ROTA NOVA IGUAÇU - LISTA DE PRESENÇA", sub=sub)
    pdf.add_page()

    # Bloco resumo
    pdf.set_font("Arial", "B", 10)
    pdf.set_fill_color(240, 240, 240)
    pdf.cell(0, 8, "RESUMO", ln=True, fill=True)

    pdf.set_font("Arial", "", 9)
    insc = resumo.get("inscritos", 0)
    vagas = resumo.get("vagas", 38)
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
        if is_exc:
            pdf.set_fill_color(255, 235, 238)
        else:
            if idx % 2 == 0:
                pdf.set_fill_color(245, 245, 245)
            else:
                pdf.set_fill_color(255, 255, 255)

        origem = str(r.get("QG_RMCF_OUTROS", "") or r.get("ORIGEM", "") or "").strip()

        pdf.cell(col_w[0], 6, str(r.get("Nº", "")), border=0, fill=True)
        pdf.cell(col_w[1], 6, str(r.get("GRADUAÇÃO", "")), border=0, fill=True)
        pdf.cell(col_w[2], 6, str(r.get("NOME", ""))[:42], border=0, fill=True)
        pdf.cell(col_w[3], 6, str(r.get("LOTAÇÃO", ""))[:34], border=0, fill=True)
        pdf.cell(col_w[4], 6, origem[:10], border=0, align="C", fill=True)
        pdf.ln()

    pdf.ln(4)
    pdf.set_font("Arial", "I", 8)
    pdf.set_text_color(80, 80, 80)
    pdf.multi_cell(0, 5, "Observação: os itens marcados como 'Exc-xx' representam excedentes além do limite de 38 vagas.")
    pdf.set_text_color(0, 0, 0)

    return pdf.output(dest="S").encode("latin-1")


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

try:
    # Leitura leve pro público
    records_u_public = buscar_usuarios_cadastrados()
    limite_max = buscar_limite_dinamico()
    sheet_u_escrita = ws_usuarios()

    # Garante colunas TEMP_* para recuperação segura
    try:
        ensure_temp_cols(sheet_u_escrita)
    except Exception:
        pass

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
                        st.error("Telefone inválido. Use DDD + 9 dígitos (ex: (21) 98765.4321).")
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

                    n_g = st.selectbox("Graduação:", ["TCEL", "MAJ", "CAP", "1º TEN", "2º TEN", "SUBTEN", "1º SGT",
                                                      "2º SGT", "3º SGT", "CB", "SD", "FC COM", "FC TER"])
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

            **2. Observação:**
            * Nos períodos em que a lista ficar suspensa para conferência (05:00h às 07:00h / 17:00h às 19:00h), os três PPMM que estiverem no topo da lista terão acesso à lista de check up (botão no topo da lista) para tirar a falta de quem estará entrando no ônibus. O mais antigo assume e na ausência dele o seu sucessor assume.
            * Após o horário de 06:50h e de 18:50h, a lista será automaticamente zerada para que o novo ciclo da lista possa ocorrer. Sendo assim, caso queira manter um histórico de viagem, antes desses horários, faça o download do pdf e/ou do resumo do W.Zap.
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
                    st.error("Telefone inválido. Use DDD + 9 dígitos (ex: (21) 98765.4321).")
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
                ad_u = st.text_input("Usuário ADM:")
                ad_s = st.text_input("Senha ADM:", type="password")
                entrou_adm = st.form_submit_button("☠️ ACESSAR PAINEL ☠️")
                if entrou_adm:
                    if ad_u == "Administrador" and ad_s == "Administrador@123":
                        st.session_state.is_admin = True
                        st.session_state._adm_first_load = True
                        st.rerun()
                    else:
                        st.error("ADM inválido.")

    # =========================================
    # PAINEL ADM
    # =========================================
    elif st.session_state.is_admin:
        st.header("🛡️ PAINEL ADMINISTRATIVO 🛡️")

        sair_btn = st.button("⬅️ SAIR DO PAINEL")
        if sair_btn:
            st.session_state.is_admin = False
            st.session_state._adm_first_load = False
            st.rerun()

        if st.session_state._adm_first_load:
            buscar_usuarios_admin.clear()
            st.session_state._adm_first_load = False

        records_u = buscar_usuarios_admin()

        cA, cB = st.columns([1, 1])
        with cA:
            att_btn = st.button("🔄 Atualizar Usuários", use_container_width=True)
            if att_btn:
                buscar_usuarios_admin.clear()
                st.rerun()
        with cB:
            st.caption("ADM lê mais fresco (TTL=3s).")

        st.subheader("⚙️ Configurações Globais")
        novo_limite = st.number_input("Limite máximo de usuários:", value=int(limite_max))
        salvar_lim = st.button("💾 SALVAR NOVO LIMITE")
        if salvar_lim:
            sheet_c = ws_config()
            gs_call(sheet_c.update, "A2", [[str(novo_limite)]])
            st.success("Limite atualizado!")
            st.rerun()

        st.divider()
        st.subheader("👥 Gestão de Usuários")
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
            if busca == "" or busca in str(user.get("Nome", "")).lower() or busca in str(user.get("Email", "")).lower():
                status = str(user.get("STATUS", "")).upper()
                with st.expander(f"{user.get('Graduação')} {user.get('Nome')} - {status}"):
                    c1, c2, c3 = st.columns([2, 1, 1])
                    c1.write(f"📧 {user.get('Email')} | 📱 {user.get('TELEFONE')}")
                    is_ativo = (status == "ATIVO")

                    new_val = c2.checkbox("Liberar", value=is_ativo, key=f"adm_chk_{i}")
                    if new_val != is_ativo:
                        gs_call(sheet_u_escrita.update_cell, i + 2, 8, "ATIVO" if new_val else "INATIVO")
                        buscar_usuarios_admin.clear()
                        buscar_usuarios_cadastrados.clear()
                        st.rerun()

                    del_btn = c3.button("🗑️", key=f"del_{i}")
                    if del_btn:
                        gs_call(sheet_u_escrita.delete_rows, i + 2)
                        buscar_usuarios_admin.clear()
                        buscar_usuarios_cadastrados.clear()
                        st.rerun()

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

            grads = ["TCEL", "MAJ", "CAP", "1º TEN", "2º TEN", "SUBTEN", "1º SGT", "2º SGT", "3º SGT", "CB", "SD", "FC COM", "FC TER"]
            origs = ["QG", "RMCF", "OUTROS"]

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

        sair_user = st.sidebar.button("⬅️ Sair", use_container_width=True)
        if sair_user:
            for key in list(st.session_state.keys()):
                del st.session_state[key]
            st.rerun()

        st.sidebar.markdown("---")
        st.sidebar.caption("Desenvolvido por: MAJ ANDRÉ AGUIAR - CAES®️")

        sheet_p_escrita = ws_presenca()

        if st.session_state._force_refresh_presenca:
            buscar_presenca_atualizada.clear()
            st.session_state._force_refresh_presenca = False

        dados_p = buscar_presenca_atualizada()
        dados_p_show = filtrar_linhas_presenca(dados_p)

        aberto, janela_conf = verificar_status_e_limpar(sheet_p_escrita, dados_p_show)

        df_o, df_v = pd.DataFrame(), pd.DataFrame()
        ja, pos = False, 999

        if dados_p_show and len(dados_p_show) > 1:
            df_o, df_v = aplicar_ordenacao(pd.DataFrame(dados_p_show[1:], columns=dados_p_show[0]))
            email_logado = str(u.get("Email")).strip().lower()
            ja = any(email_logado == str(row.get("EMAIL", "")).strip().lower() for _, row in df_o.iterrows())
            if ja:
                pos = df_o.index[df_o["EMAIL"].str.lower() == email_logado].tolist()[0] + 1

        if ja:
            st.success(f"✅ Presença registrada: {pos}º")

            # ==========================================================
            # ALTERAÇÃO SOLICITADA: confirmação antes de excluir
            # ==========================================================
            exc_btn = st.button("❌ EXCLUIR MINHA PRESENÇA ⚠️", use_container_width=True, key="btn_excluir_presenca")
            if exc_btn:
                st.session_state._confirmar_exclusao_presenca = True
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
                    st.rerun()

                if sim_btn:
                    email_logado = str(u.get("Email")).strip().lower()
                    if dados_p and len(dados_p) > 1:
                        for idx, r in enumerate(dados_p):
                            if len(r) >= 6 and str(r[5]).strip().lower() == email_logado:
                                gs_call(sheet_p_escrita.delete_rows, idx + 1)
                                break

                    st.session_state._confirmar_exclusao_presenca = False
                    buscar_presenca_atualizada.clear()
                    st.rerun()

        elif aberto:
            salvar_btn = st.button("🚀 CONFIRMAR MINHA PRESENÇA ✅", use_container_width=True)
            if salvar_btn:
                agora = datetime.now(FUSO_BR).strftime("%d/%m/%Y %H:%M:%S")
                gs_call(sheet_p_escrita.append_row, [
                    agora,
                    u.get("QG_RMCF_OUTROS") or "QG",
                    u.get("Graduação"),
                    u.get("Nome"),
                    u.get("Lotação"),
                    u.get("Email")
                ])
                buscar_presenca_atualizada.clear()
                st.rerun()
        else:
            st.info("⌛ Lista fechada para novas inscrições.")

            # ==========================================================
            # ATUALIZAR DISPONÍVEL MESMO COM LISTA FECHADA
            # ==========================================================
            up_btn_fechado = st.button("🔄 ATUALIZAR", use_container_width=True, key="up_btn_fechado")
            if up_btn_fechado:
                buscar_presenca_atualizada.clear()
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
            rest = 38 - insc
            st.subheader(f"Inscritos: {insc} | Vagas: 38 | {'Sobra' if rest >= 0 else 'Exc'}: {abs(rest)}")

            c_up1, c_up2 = st.columns([1, 1])
            with c_up1:
                up_btn = st.button("🔄 ATUALIZAR", use_container_width=True, key="up_btn_tabela")
                if up_btn:
                    buscar_presenca_atualizada.clear()
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
                f"{df_v_show.drop(columns=['EMAIL']).to_html(index=False, justify='center', border=0, escape=False, classes='presenca-zebra')}"
                f"</div>",
                unsafe_allow_html=True
            )

            c1, c2 = st.columns(2)
            with c1:
                resumo = {"inscritos": insc, "vagas": 38}
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

    st.markdown('<div class="footer">Desenvolvido por: <b>MAJ ANDRÉ AGUIAR - CAES®️</b></div>', unsafe_allow_html=True)

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


