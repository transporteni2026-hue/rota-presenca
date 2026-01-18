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
    # RECUPERA OS SECRETS
    info = st.secrets["gcp_service_account"].to_dict()
    
    # CORREÇÃO CRÍTICA PARA ERRO DE BASE64:
    # Transforma o texto '\n' em quebras de linha reais que o Google exige.
    if "private_key" in info:
        info["private_key"] = info["private_key"].replace("\\n", "\n")
    
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

    is_aberto = False

    # Regras de abertura/fechamento
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
        dias_para_seg = (7 - wd) % 7
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

    if "QG_RMCF_OUTROS" not in df.columns and "ORIGEM" in df.columns:
        df["QG_RMCF_OUTROS"] = df["ORIGEM"]
    if "QG_RMCF_OUTROS" not in df.columns:
        df["QG_RMCF_OUTROS"] = ""

    p_orig = {"QG": 1, "RMCF": 2, "OUTROS": 3}

    p_grad_normal = {
        "TCEL": 1, "MAJ": 2, "CAP": 3, "1º TEN": 4, "2º TEN": 5, "SUBTEN": 6,
        "1º SGT": 7, "2º SGT": 8, "3º SGT": 9, "CB": 10, "SD": 11
    }

    def grupo_fc(grad):
        g = str(grad or "").strip().upper()
        if g == "FC COM":
            return 1
        if g == "FC TER":
            return 2
        return 0

    df["grupo_fc"] = df["GRADUAÇÃO"].apply(grupo_fc)
    df["p_o"] = df["QG_RMCF_OUTROS"].map(p_orig).fillna(99)

    def p_grad(row):
        if int(row.get("grupo_fc", 0)) == 0:
            return p_grad_normal.get(str(row.get("GRADUAÇÃO", "")).strip().upper(), 999)
        return 0

    df["p_g"] = df.apply(p_grad, axis=1)
    df["dt"] = pd.to_datetime(df["DATA_HORA"], dayfirst=True, errors="coerce")
    df = df.sort_values(by=["grupo_fc", "p_o", "p_g", "dt"]).reset_index(drop=True)

    df.insert(0, "Nº", [str(i + 1) if i < 38 else f"Exc-{i - 37:02d}" for i in range(len(df))])

    df_v = df.copy()
    for i, r in df_v.iterrows():
        if "Exc-" in str(r["Nº"]):
            for c in df_v.columns:
                df_v.at[i, c] = f"<span style='color:#d32f2f; font-weight:bold;'>{r[c]}</span>"

    return df.drop(columns=["grupo_fc", "p_o", "p_g", "dt"]), df_v.drop(columns=["grupo_fc", "p_o", "p_g", "dt"])


# ==========================================================
# PDF “mais apresentado”
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
            pdf.set_fill_color(245, 245, 245) if idx % 2 == 0 else pdf.set_fill_color(255, 255, 255)
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
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="titulo-container"><div class="titulo-responsivo">🚌 ROTA NOVA IGUAÇU 🚌</div></div>', unsafe_allow_html=True)

ciclo_h, ciclo_d = obter_ciclo_atual()
st.markdown(f"<div class='subtitulo-ciclo'>Ciclo atual: <b>EMBARQUE {ciclo_h}h</b> do dia <b>{ciclo_d}</b></div>", unsafe_allow_html=True)

if "usuario_logado" not in st.session_state:
    st.session_state.usuario_logado = None
if "is_admin" not in st.session_state:
    st.session_state.is_admin = False
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


try:
    records_u_public = buscar_usuarios_cadastrados()
    limite_max = buscar_limite_dinamico()
    sheet_u_escrita = ws_usuarios()

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
                        st.error("Telefone inválido. Use DDD + 9 dígitos.")
                    else:
                        tel_login_digits = tel_only_digits(fmt_tel_login)
                        u_a = next((u for u in records_u_public if str(u.get("Email", "")).strip().lower() == l_e.strip().lower() and str(u.get("Senha", "")) == str(l_s) and tel_only_digits(u.get("TELEFONE", "")) == tel_login_digits), None)
                        if u_a:
                            if str(u_a.get("STATUS", "")).strip().upper() == "ATIVO":
                                st.session_state.usuario_logado = u_a
                                st.rerun()
                            else:
                                st.error("Acesso negado. Aguardando aprovação.")
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
                    n_g = st.selectbox("Graduação:", ["TCEL", "MAJ", "CAP", "1º TEN", "2º TEN", "SUBTEN", "1º SGT", "2º SGT", "3º SGT", "CB", "SD", "FC COM", "FC TER"])
                    n_l = st.text_input("Lotação:")
                    n_o = st.selectbox("Origem:", ["QG", "RMCF", "OUTROS"])
                    n_p = st.text_input("Senha:", type="password")
                    cadastrou = st.form_submit_button("✍️ SALVAR CADASTRO 👈", use_container_width=True)
                    if cadastrou:
                        def norm_str(x): return str(x or "").strip()
                        missing = []
                        if not norm_str(n_n): missing.append("Nome de Escala")
                        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", norm_str(n_e)): missing.append("E-mail")
                        if not tel_is_valid_11(fmt_tel_cad): missing.append("Telefone")
                        if not norm_str(n_p): missing.append("Senha")
                        if missing:
                            st.error("Preencha corretamente: " + ", ".join(missing))
                        else:
                            novo_email = norm_str(n_e).lower()
                            novo_tel_digits = tel_only_digits(fmt_tel_cad)
                            if any(str(u.get("Email", "")).strip().lower() == novo_email for u in records_u_public): st.error("E-mail já cadastrado.")
                            elif any(tel_only_digits(u.get("TELEFONE", "")) == novo_tel_digits for u in records_u_public): st.error("Telefone já cadastrado.")
                            else:
                                gs_call(sheet_u_escrita.append_row, [norm_str(n_n), norm_str(n_g), norm_str(n_l), norm_str(n_p), norm_str(n_o), norm_str(n_e), fmt_tel_cad, "PENDENTE"])
                                buscar_usuarios_cadastrados.clear()
                                st.success("Cadastro realizado! Aguarde aprovação.")
                                st.rerun()

        with t3:
            st.markdown("### 📖 Guia de Uso")
            st.success("📲 **COMO INSTALAR (TELA INICIAL)**")
            st.markdown("**No Chrome (Android):** Toque nos 3 pontos (⋮) e em 'Instalar Aplicativo'.")
            st.markdown("**No Safari (iPhone):** Toque em Compartilhar (⬆️) e em 'Adicionar à Tela de Início'.")
            st.divider()
            st.info("**CADASTRO E LOGIN:** Use seu e-mail como identificador único.")

        with t4:
            e_r = st.text_input("E-mail cadastrado:")
            if st.button("Recuperar Dados"):
                u_r = next((u for u in records_u_public if str(u.get("Email", "")).strip().lower() == e_r.strip().lower()), None)
                if u_r: st.info(f"Senha: {u_r.get('Senha')}")
                else: st.error("E-mail não encontrado.")

        with t5:
            with st.form("form_admin"):
                ad_u = st.text_input("ADM:")
                ad_s = st.text_input("Senha:", type="password")
                if st.form_submit_button("Acessar"):
                    if ad_u == "Administrador" and ad_s == "Administrador@123":
                        st.session_state.is_admin = True
                        st.rerun()

    elif st.session_state.is_admin:
        st.header("🛡️ PAINEL ADMINISTRATIVO")
        if st.button("Sair"):
            st.session_state.is_admin = False
            st.rerun()
        # Gestão de usuários simplificada aqui...

    else:
        u = st.session_state.usuario_logado
        st.sidebar.info(f"**{u.get('Graduação')} {u.get('Nome')}**")
        if st.sidebar.button("Sair"):
            for key in list(st.session_state.keys()):
                if key in st.session_state:
                    del st.session_state[key]
            st.rerun()

        sheet_p_escrita = ws_presenca()
        dados_p_show = filtrar_linhas_presenca(buscar_presenca_atualizada())
        aberto, janela_conf = verificar_status_e_limpar(sheet_p_escrita, dados_p_show)

        df_o, df_v = pd.DataFrame(), pd.DataFrame()
        ja, pos = False, 999
        if dados_p_show and len(dados_p_show) > 1:
            df_o, df_v = aplicar_ordenacao(pd.DataFrame(dados_p_show[1:], columns=dados_p_show[0]))
            email_logado = str(u.get("Email")).strip().lower()
            ja = any(email_logado == str(row.get("EMAIL", "")).strip().lower() for _, row in df_o.iterrows())
            if ja: pos = df_o.index[df_o["EMAIL"].str.lower() == email_logado].tolist()[0] + 1

        if ja:
            st.success(f"✅ Presença registrada: {pos}º")
            if st.button("❌ EXCLUIR MINHA PRESENÇA"):
                for idx, r in enumerate(buscar_presenca_atualizada()):
                    if len(r) >= 6 and str(r[5]).strip().lower() == email_logado:
                        gs_call(sheet_p_escrita.delete_rows, idx + 1)
                        buscar_presenca_atualizada.clear()
                        st.rerun()
        elif aberto:
            if st.button("🚀 CONFIRMAR MINHA PRESENÇA"):
                gs_call(sheet_p_escrita.append_row, [datetime.now(FUSO_BR).strftime("%d/%m/%Y %H:%M:%S"), u.get("QG_RMCF_OUTROS") or "QG", u.get("Graduação"), u.get("Nome"), u.get("Lotação"), u.get("Email")])
                buscar_presenca_atualizada.clear()
                st.rerun()
        else:
            st.info("⌛ Lista fechada.")
            if st.button("🔄 ATUALIZAR"):
                buscar_presenca_atualizada.clear()
                st.rerun()

        if dados_p_show and len(dados_p_show) > 1:
            st.write(f"<div class='tabela-responsiva'>{df_v.drop(columns=['EMAIL']).to_html(index=False, justify='center', border=0, escape=False)}</div>", unsafe_allow_html=True)

    st.markdown('<div class="footer">Desenvolvido por: <b>MAJ ANDRÉ AGUIAR - CAES®️</b></div>', unsafe_allow_html=True)
    st.markdown(f'<div style="text-align:center;"><img src="{GIF_URL}" style="width:80%; max-width:520px;"/></div>', unsafe_allow_html=True)

except Exception as e:
    st.error(f"⚠️ Erro: {e}")
