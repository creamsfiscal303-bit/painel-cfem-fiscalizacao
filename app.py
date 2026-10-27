import io
import re
import tempfile
from pathlib import Path

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

st.set_page_config(
    page_title="Inteligência Fiscalizatória | CFEM",
    page_icon="⛏️",
    layout="wide",
)

SOURCE_URL = "https://dadosabertos.anm.gov.br/CFEM/CFEM_Arrecadacao_2022_2026.csv"
SOURCE_NAME = "ANM — CFEM Arrecadação 2022–2026"
CHUNK_SIZE = 100_000

st.title("⛏️ Painel de Inteligência Fiscalizatória")
st.caption("Arrecadação da CFEM • Agência Nacional de Mineração (ANM) • Dados públicos de 2022 a 2026")
st.info(
    "A arrecadação da CFEM é um indício de atividade mineral declarada, mas não comprova, isoladamente, "
    "operação atual nem obrigação de registro no CREA. Use os resultados como apoio à triagem e confirme "
    "as informações antes de qualquer ação fiscalizatória."
)

@st.cache_data(show_spinner=False, ttl=6 * 60 * 60)
def download_source() -> str:
    """Baixa o CSV oficial para um arquivo temporário persistente durante a sessão/cache."""
    path = Path(tempfile.gettempdir()) / "CFEM_Arrecadacao_2022_2026.csv"
    if path.exists() and path.stat().st_size > 1_000_000:
        return str(path)

    response = requests.get(SOURCE_URL, stream=True, timeout=(30, 180))
    response.raise_for_status()
    with open(path, "wb") as f:
        for block in response.iter_content(chunk_size=1024 * 1024):
            if block:
                f.write(block)
    if path.stat().st_size < 1_000_000:
        raise RuntimeError("O arquivo baixado parece incompleto. Verifique a fonte oficial da ANM.")
    return str(path)

def normalize_columns(columns):
    return [str(c).replace("\ufeff", "").strip().strip('"') for c in columns]

def parse_br_number(series: pd.Series) -> pd.Series:
    s = series.astype(str).str.strip()
    s = s.str.replace("\u00a0", "", regex=False).str.replace(" ", "", regex=False)
    # Os campos da ANM usam vírgula como separador decimal.
    s = s.str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
    return pd.to_numeric(s, errors="coerce")

def normalize_document(value) -> str:
    """Mantém apenas dígitos para comparar CNPJs sem pontuação."""
    if pd.isna(value):
        return ""
    digits = re.sub(r"\D", "", str(value))
    # CSVs podem importar CNPJ como número e perder zeros à esquerda.
    if digits and len(digits) < 14:
        digits = digits.zfill(14)
    return digits

def read_crea_csv(uploaded_file) -> pd.DataFrame:
    """Lê a lista de empresas CREA-MS com separador/encoding comuns."""
    raw = uploaded_file.getvalue()
    last_error = None
    for encoding in ("utf-8-sig", "latin1", "cp1252"):
        try:
            data = pd.read_csv(io.BytesIO(raw), sep=None, engine="python", dtype=str, encoding=encoding)
            data.columns = [str(c).replace("\ufeff", "").strip() for c in data.columns]
            # Normaliza nomes de colunas sem exigir cabeçalhos exatos.
            lookup = {re.sub(r"[^a-z0-9]", "", c.lower().translate(str.maketrans("áàãâéêíóôõúç", "aaaaeeiooouc"))): c for c in data.columns}
            cnpj_col = next((v for k, v in lookup.items() if k in ("cnpj", "cpfcnpj", "documento")), None)
            name_col = next((v for k, v in lookup.items() if k in ("empresa", "razaosocial", "nome", "nomeempresarial")), None)
            status_col = next((v for k, v in lookup.items() if k in ("situacaoempresa", "situacao", "status")), None)
            if not cnpj_col or not name_col:
                raise ValueError("Não encontrei as colunas de CNPJ e nome da empresa no CSV.")
            out = pd.DataFrame({
                "CNPJ_normalizado": data[cnpj_col].map(normalize_document),
                "Empresa_CREA": data[name_col].fillna("").astype(str).str.strip(),
                "Situacao_CREA": data[status_col].fillna("").astype(str).str.strip() if status_col else "Não informada",
            })
            out = out[out["CNPJ_normalizado"].str.len().eq(14) & out["CNPJ_normalizado"].ne("")].copy()
            # Mantém uma linha por CNPJ, priorizando ATIVO quando houver duplicidade.
            out["_ativo"] = out["Situacao_CREA"].str.upper().eq("ATIVO")
            out = out.sort_values("_ativo", ascending=False).drop_duplicates("CNPJ_normalizado").drop(columns="_ativo")
            return out
        except Exception as exc:
            last_error = exc
    raise ValueError(f"Não foi possível ler o CSV: {last_error}")

@st.cache_data(show_spinner=False, ttl=24 * 60 * 60)
def load_municipality_geojson(state: str):
    """Obtém GeoJSON municipal pré-compilado via CDN, evitando a API dinâmica do IBGE."""
    url = (
        "https://cdn.jsdelivr.net/gh/henriquemalvar/br-geojson@main/"
        f"dist/municipios/{state}.geojson"
    )
    try:
        response = requests.get(url, timeout=(8, 30))
        response.raise_for_status()
        geo = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(
            "Não foi possível acessar o arquivo geográfico municipal no CDN. "
            "Os dados da CFEM continuam disponíveis; tente novamente mais tarde."
        ) from exc
    except ValueError as exc:
        raise RuntimeError("O arquivo geográfico retornou um JSON inválido.") from exc

    features = geo.get("features", [])
    if not features:
        raise ValueError("O GeoJSON não contém municípios para a UF selecionada.")

    # O conjunto pré-compilado contém códigos IBGE na propriedade cd_geocmu.
    sample_props = features[0].get("properties", {}) or {}
    code_key = next(
        (key for key in ["cd_geocmu", "CD_MUN", "CD_GEOCMU", "codarea", "codigo"] if key in sample_props),
        None,
    )
    if code_key is None:
        raise ValueError(f"Não foi possível identificar o código IBGE. Campos: {list(sample_props.keys())}")
    return geo, code_key


@st.cache_data(show_spinner=False, ttl=6 * 60 * 60)
def load_state_data(file_path: str, state: str) -> pd.DataFrame:
    """Lê o CSV em blocos e mantém somente a UF selecionada para reduzir uso de memória."""
    chunks = []
    encodings = ["utf-8-sig", "latin-1"]
    last_error = None

    for encoding in encodings:
        chunks = []
        try:
            for chunk in pd.read_csv(
                file_path,
                sep=",",
                dtype=str,
                chunksize=CHUNK_SIZE,
                encoding=encoding,
                keep_default_na=False,
                on_bad_lines="skip",
                low_memory=True,
            ):
                chunk.columns = normalize_columns(chunk.columns)
                if "UF" not in chunk.columns:
                    raise ValueError(
                        "A coluna 'UF' não foi encontrada. Confira o delimitador e a estrutura do CSV da ANM."
                    )
                chunk["UF"] = chunk["UF"].astype(str).str.strip().str.upper()
                chunk = chunk[chunk["UF"] == state]
                if not chunk.empty:
                    chunks.append(chunk)
            break
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    else:
        raise RuntimeError(f"Não foi possível decodificar o CSV: {last_error}")

    if not chunks:
        return pd.DataFrame()

    df = pd.concat(chunks, ignore_index=True)
    df.columns = normalize_columns(df.columns)

    for col in ["Ano", "Mês", "Processo", "AnoDoProcesso", "Tipo_PF_PJ", "CPF_CNPJ",
                "Substância", "UF", "CodigoMunicipio", "Município", "UnidadeDeMedida", "DataCriacao"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    if "Ano" in df.columns:
        df["Ano_num"] = pd.to_numeric(df["Ano"].str.extract(r"(\d{4})", expand=False), errors="coerce")
    if "QuantidadeComercializada" in df.columns:
        df["Quantidade_num"] = parse_br_number(df["QuantidadeComercializada"])
    if "ValorRecolhido" in df.columns:
        df["Valor_num"] = parse_br_number(df["ValorRecolhido"])

    if "Município" in df.columns:
        df["Município"] = df["Município"].str.replace(r"\s+", " ", regex=True).str.strip()
    if "Substância" in df.columns:
        df["Substância"] = df["Substância"].str.replace(r"\s+", " ", regex=True).str.strip().str.upper()
    if "UnidadeDeMedida" in df.columns:
        df["UnidadeDeMedida"] = df["UnidadeDeMedida"].str.replace(r"\s+", " ", regex=True).str.strip()

    return df

with st.sidebar:
    st.header("Filtros")
    state_options = [
        "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO",
        "MA", "MT", "MS", "MG", "PA", "PB", "PR", "PE", "PI",
        "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO"
    ]
    selected_state = st.selectbox("Unidade da Federação", state_options, index=5)
    st.caption("Cobertura nacional: selecione a UF desejada. O painel processa uma UF por vez para reduzir o consumo de memória.")
    st.divider()
    st.markdown("**Fonte dos dados**")
    st.markdown(f"[Arquivo oficial da ANM]({SOURCE_URL})")
    st.caption("A base é baixada sob demanda e lida em blocos.")

try:
    with st.spinner("Baixando/verificando a base oficial da ANM e filtrando os registros..."):
        source_path = download_source()
        df = load_state_data(source_path, selected_state)
except Exception as exc:
    st.error("Não foi possível carregar a base da ANM.")
    st.exception(exc)
    st.markdown(f"Fonte alternativa para conferência: [{SOURCE_URL}]({SOURCE_URL})")
    st.stop()

if df.empty:
    st.warning("Não foram encontrados registros para a seleção atual.")
    st.stop()

required_cols = ["Ano", "Mês", "Processo", "Tipo_PF_PJ", "CPF_CNPJ", "Substância", "UF",
                 "CodigoMunicipio", "Município", "QuantidadeComercializada", "UnidadeDeMedida",
                 "ValorRecolhido", "DataCriacao"]
missing = [c for c in required_cols if c not in df.columns]
if missing:
    st.warning("Algumas colunas esperadas não foram encontradas: " + ", ".join(missing))

with st.sidebar:
    years = sorted([int(y) for y in df["Ano_num"].dropna().unique()]) if "Ano_num" in df.columns else []
    if years:
        min_year, max_year = min(years), max(years)
        year_range = st.slider("Ano de arrecadação", min_value=min_year, max_value=max_year,
                               value=(min_year, max_year), step=1)
    else:
        year_range = None

    substances = sorted(df["Substância"].dropna().astype(str).unique()) if "Substância" in df.columns else []
    selected_substances = st.multiselect("Substância mineral", substances, default=[])

    municipalities = sorted(df["Município"].dropna().astype(str).unique()) if "Município" in df.columns else []
    selected_municipalities = st.multiselect("Município", municipalities, default=[])

    person_types = sorted(
        [v for v in df["Tipo_PF_PJ"].dropna().astype(str).str.strip().unique() if v]
    ) if "Tipo_PF_PJ" in df.columns else []
    selected_person_types = st.multiselect(
        "Tipo de pessoa", person_types, default=[],
        help="Ex.: PJ para pessoa jurídica e PF para pessoa física, conforme os códigos da base da ANM."
    )

    search_text = st.text_input("Busca por CNPJ, processo ou município").strip()

filtered = df.copy()
if year_range and "Ano_num" in filtered.columns:
    filtered = filtered[filtered["Ano_num"].between(year_range[0], year_range[1])]
if selected_substances:
    filtered = filtered[filtered["Substância"].isin(selected_substances)]
if selected_municipalities:
    filtered = filtered[filtered["Município"].isin(selected_municipalities)]
if selected_person_types and "Tipo_PF_PJ" in filtered.columns:
    filtered = filtered[filtered["Tipo_PF_PJ"].astype(str).str.strip().isin(selected_person_types)]
if search_text:
    search_cols = [c for c in ["CPF_CNPJ", "Processo", "Município", "CodigoMunicipio"] if c in filtered.columns]
    mask = pd.Series(False, index=filtered.index)
    for col in search_cols:
        mask |= filtered[col].astype(str).str.contains(re.escape(search_text), case=False, na=False)
    filtered = filtered[mask]

if filtered.empty:
    st.warning("Nenhum registro corresponde aos filtros selecionados.")
    st.stop()

def unique_count(col):
    if col not in filtered.columns:
        return 0
    values = filtered[col].astype(str).str.strip()
    values = values[values.ne("") & values.ne("nan")]
    return int(values.nunique())

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Registros", f"{len(filtered):,}".replace(",", "."))
k2.metric("Processos distintos", f"{unique_count('Processo'):,}".replace(",", "."))
k3.metric("Municípios", f"{unique_count('Município'):,}".replace(",", "."))
k4.metric("Substâncias", f"{unique_count('Substância'):,}".replace(",", "."))
k5.metric("Valor recolhido", f"R$ {filtered['Valor_num'].sum():,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if "Valor_num" in filtered.columns else "N/D")

st.divider()
tab_overview, tab_map, tab_companies, tab_records, tab_export, tab_match, tab_method = st.tabs(
    ["Visão geral", "Mapa municipal", "Empresas e processos", "Registros detalhados", "Exportar dados", "Conferência CREA-MS", "Metodologia"]
)

with tab_overview:
    left, right = st.columns(2)
    if "Município" in filtered.columns and "Valor_num" in filtered.columns:
        by_municipality = (filtered.groupby("Município", dropna=False)
                           .agg(ValorRecolhido=("Valor_num", "sum"),
                                Registros=("Município", "size"),
                                Processos=("Processo", "nunique"))
                           .reset_index()
                           .sort_values("ValorRecolhido", ascending=False)
                           .head(15))
        left.subheader("Municípios por valor recolhido")
        left.plotly_chart(px.bar(by_municipality.sort_values("ValorRecolhido"),
                                 x="ValorRecolhido", y="Município", orientation="h",
                                 hover_data=["Registros", "Processos"],
                                 labels={"ValorRecolhido": "Valor recolhido (R$)", "Município": "Município"}),
                          use_container_width=True)
    if "Substância" in filtered.columns:
        by_substance = (filtered.groupby("Substância", dropna=False)
                        .agg(Registros=("Substância", "size"))
                        .reset_index().sort_values("Registros", ascending=False).head(15))
        right.subheader("Substâncias mais frequentes")
        right.plotly_chart(px.bar(by_substance.sort_values("Registros"),
                                  x="Registros", y="Substância", orientation="h",
                                  labels={"Registros": "Registros", "Substância": "Substância"}),
                           use_container_width=True)

    if "Ano_num" in filtered.columns and "Valor_num" in filtered.columns:
        annual = (filtered.groupby("Ano_num", dropna=False)
                  .agg(ValorRecolhido=("Valor_num", "sum"), Registros=("Ano_num", "size"))
                  .reset_index().sort_values("Ano_num"))
        st.subheader("Evolução anual")
        st.plotly_chart(px.line(annual, x="Ano_num", y="ValorRecolhido", markers=True,
                                labels={"Ano_num": "Ano", "ValorRecolhido": "Valor recolhido (R$)"}),
                        use_container_width=True)

with tab_map:
    st.subheader("Mapa municipal da arrecadação da CFEM")
    st.caption(
        "Cada município aparece uma única vez no mapa. Os valores e as contagens são agregados "
        "a partir de todos os registros que atendem aos filtros selecionados."
    )
    if "CodigoMunicipio" not in filtered.columns:
        st.warning("A base filtrada não contém a coluna CodigoMunicipio.")
    else:
        map_metric = st.radio(
            "Indicador do mapa",
            ["Valor recolhido (R$)", "Quantidade de registros", "Processos distintos"],
            horizontal=True,
        )
        group_cols = ["CodigoMunicipio"]
        if "Município" in filtered.columns:
            group_cols.append("Município")
        if "UF" in filtered.columns:
            group_cols.append("UF")

        agg_spec = {"Registros": ("CodigoMunicipio", "size")}
        if "Valor_num" in filtered.columns:
            agg_spec["ValorRecolhido"] = ("Valor_num", "sum")
        if "Processo" in filtered.columns:
            agg_spec["Processos"] = ("Processo", "nunique")
        city_data = filtered.groupby(group_cols, dropna=False).agg(**agg_spec).reset_index()
        city_data["CodigoMunicipio"] = (
            city_data["CodigoMunicipio"].astype(str).str.extract(r"(\d{7})", expand=False)
        )
        city_data = city_data.dropna(subset=["CodigoMunicipio"])
        city_data = city_data.drop_duplicates(subset=["CodigoMunicipio"])

        metric_map = {
            "Valor recolhido (R$)": "ValorRecolhido",
            "Quantidade de registros": "Registros",
            "Processos distintos": "Processos",
        }
        metric_col = metric_map[map_metric]
        if metric_col not in city_data.columns:
            st.warning("O indicador escolhido não está disponível na base.")
        elif city_data.empty:
            st.warning("Não há municípios para exibir com os filtros atuais.")
        else:
            try:
                geojson, code_key = load_municipality_geojson(selected_state)
                if code_key == "__feature_id__":
                    for feature in geojson["features"]:
                        feature["properties"]["__map_code"] = str(feature.get("id", ""))
                    feature_key = "properties.__map_code"
                else:
                    for feature in geojson["features"]:
                        feature["properties"]["__map_code"] = str(feature.get("properties", {}).get(code_key, ""))
                    feature_key = "properties.__map_code"

                city_data["CodigoMunicipio"] = city_data["CodigoMunicipio"].astype(str)
                city_data["ValorMapa"] = pd.to_numeric(city_data[metric_col], errors="coerce").fillna(0)
                fig_map = px.choropleth(
                    city_data,
                    geojson=geojson,
                    locations="CodigoMunicipio",
                    featureidkey=feature_key,
                    color="ValorMapa",
                    hover_name="Município" if "Município" in city_data.columns else "CodigoMunicipio",
                    hover_data={
                        "CodigoMunicipio": True,
                        "Registros": True,
                        "Processos": True if "Processos" in city_data.columns else False,
                        "ValorRecolhido": ":,.2f" if "ValorRecolhido" in city_data.columns else False,
                        "ValorMapa": False,
                    },
                    labels={
                        "CodigoMunicipio": "Código IBGE",
                        "Registros": "Registros",
                        "Processos": "Processos distintos",
                        "ValorRecolhido": "Valor recolhido (R$)",
                        "ValorMapa": map_metric,
                    },
                )
                fig_map.update_geos(fitbounds="locations", visible=False)
                fig_map.update_layout(height=650, margin={"r": 0, "t": 20, "l": 0, "b": 0})
                st.plotly_chart(fig_map, use_container_width=True)
                st.caption(f"Municípios exibidos sem duplicidade: {len(city_data):,}".replace(",", "."))
                st.dataframe(
                    city_data.sort_values(metric_col, ascending=False),
                    use_container_width=True,
                    hide_index=True,
                )
                st.download_button(
                    "Baixar resumo municipal (CSV)",
                    city_data.to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"cfem_{selected_state.lower()}_resumo_municipal.csv",
                    mime="text/csv",
                )
            except Exception as exc:
                st.warning("Não foi possível carregar o mapa municipal neste momento.")
                st.caption(
                    "A API externa do IBGE excedeu o tempo de resposta. Os dados da CFEM, os gráficos "
                    "e as exportações continuam disponíveis. Atualize a página ou tente novamente mais tarde."
                )
                with st.expander("Detalhes técnicos"):
                    st.code(str(exc))

with tab_companies:
    st.subheader("Consolidação por identificador e processo")
    st.caption("A base pode conter CPF mascarado. O painel não tenta reconstruir dados ocultos nem presume que cada processo represente uma empresa distinta.")
    if "CPF_CNPJ" in filtered.columns:
        grouped = (filtered.groupby(["CPF_CNPJ", "Tipo_PF_PJ"], dropna=False)
                   .agg(Registros=("CPF_CNPJ", "size"),
                        Processos=("Processo", "nunique"),
                        Municipios=("Município", "nunique"),
                        Substancias=("Substância", "nunique"),
                        ValorRecolhido=("Valor_num", "sum"))
                   .reset_index()
                   .sort_values(["Registros", "ValorRecolhido"], ascending=False))
        st.dataframe(grouped, use_container_width=True, hide_index=True)
        st.download_button("Baixar consolidação por identificador (CSV)",
                           grouped.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"cfem_{selected_state.lower()}_consolidado.csv",
                           mime="text/csv")
    if "Processo" in filtered.columns:
        process_cols = [c for c in ["Processo", "AnoDoProcesso", "CPF_CNPJ", "Tipo_PF_PJ",
                                     "Substância", "UF", "CodigoMunicipio", "Município"] if c in filtered.columns]
        proc = filtered[process_cols].drop_duplicates().sort_values("Processo")
        st.subheader("Processos minerários identificados")
        st.dataframe(proc, use_container_width=True, hide_index=True)
        st.download_button("Baixar processos (CSV)", proc.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"cfem_{selected_state.lower()}_processos.csv", mime="text/csv")

with tab_records:
    st.subheader("Dados filtrados")
    display_cols = [c for c in required_cols if c in filtered.columns]
    st.dataframe(filtered[display_cols], use_container_width=True, hide_index=True, height=520)
    st.download_button("Baixar registros filtrados (CSV)",
                       filtered[display_cols].to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"cfem_{selected_state.lower()}_registros.csv",
                       mime="text/csv")

with tab_export:
    st.subheader("Exportação dos dados")
    st.write(
        "Exporte todos os campos originais disponíveis na base da ANM, incluindo "
        "**CPF_CNPJ**, tipo de pessoa, processo, substância, município, quantidade, valor e data. "
        "A exportação respeita todos os filtros aplicados: UF, ano, substância, município, tipo de pessoa e busca por CNPJ/processo/município."
    )
    st.warning(
        "O identificador é exportado exatamente como aparece na fonte. Se um CPF estiver mascarado "
        "na base da ANM, os dígitos ocultos não podem ser recuperados por este painel."
    )

    helper_cols = {"Ano_num", "Quantidade_num", "Valor_num"}
    export_cols = [c for c in filtered.columns if c not in helper_cols]
    export_df = filtered[export_cols].copy()

    col_csv, col_xlsx = st.columns(2)
    col_csv.download_button(
        "⬇️ Baixar todos os campos em CSV",
        data=export_df.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"cfem_{selected_state.lower()}_dados_completos.csv",
        mime="text/csv",
        help="CSV abre no Excel e preserva CPF/CNPJ como texto na origem do arquivo."
    )

    @st.cache_data(show_spinner=False)
    def make_excel_export(data: pd.DataFrame, state_code: str) -> bytes:
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            data.to_excel(writer, sheet_name="Dados completos", index=False)
            if "CPF_CNPJ" in data.columns:
                id_cols = [c for c in ["CPF_CNPJ", "Tipo_PF_PJ"] if c in data.columns]
                summary = (
                    data.groupby(id_cols, dropna=False)
                    .agg(
                        Registros=("CPF_CNPJ", "size"),
                        Processos=("Processo", "nunique") if "Processo" in data.columns else ("CPF_CNPJ", "size"),
                        Municipios=("Município", "nunique") if "Município" in data.columns else ("CPF_CNPJ", "size"),
                        Substancias=("Substância", "nunique") if "Substância" in data.columns else ("CPF_CNPJ", "size"),
                    )
                    .reset_index()
                )
                summary.to_excel(writer, sheet_name="Resumo por titular", index=False)
        return output.getvalue()

    excel_bytes = make_excel_export(export_df, selected_state)
    col_xlsx.download_button(
        "⬇️ Baixar Excel com resumo",
        data=excel_bytes,
        file_name=f"cfem_{selected_state.lower()}_dados_completos.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        help="Inclui os registros completos e uma aba de resumo por identificador."
    )
    st.caption(
        f"Registros incluídos na exportação: {len(export_df):,}. "
        "Os filtros de ano, substância, município e busca são aplicados antes da exportação."
    )

with tab_match:
    st.subheader("Conferência de titulares da ANM com empresas registradas no CREA-MS")
    st.write(
        "Envie a relação de empresas do CREA-MS em CSV. O cruzamento utiliza o CNPJ normalizado, "
        "ignorando pontos, barras e traços. A tabela detalha cada combinação de CNPJ, UF, município e substância. "
        "O resultado é uma triagem e não determina, isoladamente, obrigatoriedade de registro ou ocorrência de infração."
    )
    uploaded_crea = st.file_uploader(
        "Carregar CSV de empresas do CREA-MS", type=["csv"], key="crea_ms_csv_upload",
        help="Pode conter CNPJ, Empresa e Situação Empresa; os cabeçalhos podem variar."
    )
    if uploaded_crea is not None:
        try:
            crea = read_crea_csv(uploaded_crea)
            st.success(f"Lista carregada: {len(crea):,} CNPJs únicos válidos.".replace(",", "."))
            if "CPF_CNPJ" not in filtered.columns:
                st.warning("A base filtrada da ANM não contém a coluna CPF_CNPJ esperada.")
            else:
                cfem_match = filtered.copy()
                # Primeiro restringe a pessoas jurídicas: a função de normalização completa
                # zeros à esquerda para CNPJ, então não devemos converter CPF de 11 dígitos em CNPJ.
                if "Tipo_PF_PJ" in cfem_match.columns:
                    tipo = cfem_match["Tipo_PF_PJ"].fillna("").astype(str).str.upper().str.strip()
                    pj_mask = tipo.str.contains(r"(^PJ$|PESSOA\s*JUR[IÍ]D|JUR[IÍ]DICA|JURIDICA)", regex=True)
                    # Para fontes que usam códigos diferentes, aceita também apenas identificadores
                    # que já tenham 14 dígitos; CPFs de 11 dígitos ficam fora da conferência PJ.
                    raw_digits = cfem_match["CPF_CNPJ"].map(lambda v: re.sub(r"\D", "", str(v)) if pd.notna(v) else "")
                    cfem_match = cfem_match[pj_mask | raw_digits.str.len().eq(14)].copy()
                else:
                    raw_digits = cfem_match["CPF_CNPJ"].map(lambda v: re.sub(r"\D", "", str(v)) if pd.notna(v) else "")
                    cfem_match = cfem_match[raw_digits.str.len().eq(14)].copy()
                cfem_match["CNPJ_normalizado"] = cfem_match["CPF_CNPJ"].map(normalize_document)
                cfem_match = cfem_match[cfem_match["CNPJ_normalizado"].str.len().eq(14)].copy()

                # Garante as colunas geográficas/de substância, mesmo se uma versão da fonte não as trouxer.
                for col in ["UF", "Município", "Substância"]:
                    if col not in cfem_match.columns:
                        cfem_match[col] = "Não informado"
                    cfem_match[col] = cfem_match[col].fillna("").astype(str).str.strip().replace("", "Não informado")

                agg = {
                    "Registros_CFEM": ("CNPJ_normalizado", "size"),
                    "Processos_distintos": ("Processo", "nunique") if "Processo" in cfem_match.columns else ("CNPJ_normalizado", "size"),
                    "Valor_CFEM_R": ("Valor_num", "sum") if "Valor_num" in cfem_match.columns else ("CNPJ_normalizado", "size"),
                }
                if "Ano_num" in cfem_match.columns:
                    cfem_match["Ano_num"] = pd.to_numeric(cfem_match["Ano_num"], errors="coerce")
                    def years_text(values):
                        years = sorted({int(v) for v in values.dropna().tolist()})
                        return ", ".join(map(str, years)) if years else "Não informado"
                    def annual_counts(group):
                        counts = group.dropna(subset=["Ano_num"]).groupby("Ano_num").size().sort_index()
                        return "; ".join(f"{int(year)}: {int(count)}" for year, count in counts.items()) if not counts.empty else "Não informado"
                    agg["Anos_CFEM"] = ("Ano_num", years_text)
                    agg["Ultimo_ano_CFEM"] = ("Ano_num", "max")
                    agg["Registros_por_ano"] = ("Ano_num", lambda values: annual_counts(cfem_match.loc[values.index]))
                else:
                    agg["Anos_CFEM"] = ("CNPJ_normalizado", lambda values: "Não informado")
                    agg["Ultimo_ano_CFEM"] = ("CNPJ_normalizado", lambda values: "Não informado")
                    agg["Registros_por_ano"] = ("CNPJ_normalizado", lambda values: "Não informado")
                dimensions = ["CNPJ_normalizado", "UF", "Município", "Substância"]
                detail = cfem_match.groupby(dimensions, dropna=False).agg(**agg).reset_index()
                detail["Ultimo_ano_CFEM"] = detail["Ultimo_ano_CFEM"].apply(
                    lambda value: str(int(value)) if pd.notna(value) and str(value).strip() not in ("", "nan") and str(value).replace(".", "", 1).isdigit() else "Não informado"
                )
                detail = detail.merge(crea, on="CNPJ_normalizado", how="left", indicator=True)
                detail["Resultado_conferencia"] = detail["_merge"].map({"both": "Encontrado no CREA-MS", "left_only": "Não localizado no CREA-MS"})
                detail = detail.drop(columns=["_merge"]).rename(columns={"CNPJ_normalizado": "CNPJ"})

                name_candidates = [c for c in ["Nome", "Titular", "NomeTitular", "NomePessoa", "NomeContribuinte"] if c in cfem_match.columns]
                if name_candidates:
                    name_col = name_candidates[0]
                    names = cfem_match.groupby("CNPJ_normalizado")[name_col].agg(
                        lambda x: next((str(v).strip() for v in x if pd.notna(v) and str(v).strip()), "")
                    ).rename("Nome_ANM").reset_index().rename(columns={"CNPJ_normalizado": "CNPJ"})
                    detail = detail.merge(names, on="CNPJ", how="left")

                status_upper = detail["Situacao_CREA"].fillna("").astype(str).str.upper()
                detail.loc[detail["Resultado_conferencia"].eq("Encontrado no CREA-MS") & status_upper.str.contains("INATIV|CANCEL|BAIXAD", regex=True), "Resultado_conferencia"] = "Encontrado — cadastro INATIVO"
                detail.loc[detail["Resultado_conferencia"].eq("Encontrado no CREA-MS") & status_upper.str.contains("ATIVO", regex=False), "Resultado_conferencia"] = "Encontrado — cadastro ATIVO"

                # Indicadores consolidados por CNPJ: a empresa não é contada várias vezes por município/substância.
                holder_summary = detail.groupby("CNPJ", dropna=False).agg(
                    Resultado_conferencia=("Resultado_conferencia", "first"),
                    Valor_CFEM_R=("Valor_CFEM_R", "sum"),
                    Processos_distintos=("Processos_distintos", "sum"),
                ).reset_index()
                total = holder_summary["CNPJ"].nunique()
                found = holder_summary[holder_summary["Resultado_conferencia"].str.startswith("Encontrado")]["CNPJ"].nunique()
                missing_count = holder_summary[holder_summary["Resultado_conferencia"].eq("Não localizado no CREA-MS")]["CNPJ"].nunique()
                a, b, c = st.columns(3)
                a.metric("Titulares PJ com CNPJ", f"{total:,}".replace(",", "."))
                b.metric("Localizados no CREA-MS", f"{found:,}".replace(",", "."))
                c.metric("Não localizados", f"{missing_count:,}".replace(",", "."))

                f1, f2, f3, f4 = st.columns(4)
                result_choices = ["Todos"] + sorted(detail["Resultado_conferencia"].dropna().unique().tolist())
                selected_result = f1.selectbox("Resultado da conferência", result_choices, key="crea_match_filter")
                uf_choices = ["Todas"] + sorted(detail["UF"].dropna().astype(str).unique().tolist())
                selected_uf = f2.selectbox("UF", uf_choices, key="crea_match_uf")
                municipality_choices = ["Todos"] + sorted(detail["Município"].dropna().astype(str).unique().tolist())
                selected_municipality = f3.selectbox("Município", municipality_choices, key="crea_match_municipio")
                substance_choices = ["Todas"] + sorted(detail["Substância"].dropna().astype(str).unique().tolist())
                selected_substance = f4.selectbox("Substância mineral", substance_choices, key="crea_match_substancia")

                shown = detail.copy()
                if selected_result != "Todos":
                    shown = shown[shown["Resultado_conferencia"].eq(selected_result)]
                if selected_uf != "Todas":
                    shown = shown[shown["UF"].eq(selected_uf)]
                if selected_municipality != "Todos":
                    shown = shown[shown["Município"].eq(selected_municipality)]
                if selected_substance != "Todas":
                    shown = shown[shown["Substância"].eq(selected_substance)]

                preferred_cols = ["CNPJ", "Nome_ANM", "UF", "Município", "Substância", "Anos_CFEM", "Ultimo_ano_CFEM", "Registros_por_ano", "Registros_CFEM", "Processos_distintos", "Valor_CFEM_R", "Empresa_CREA", "Situacao_CREA", "Resultado_conferencia"]
                display_cols = [c for c in preferred_cols if c in shown.columns]
                st.dataframe(shown[display_cols].sort_values("Valor_CFEM_R", ascending=False), use_container_width=True, hide_index=True, height=500)
                st.caption(f"Exibindo {len(shown):,} combinações de CNPJ, UF, município e substância. Os indicadores acima contam titulares únicos. Os anos e o último ano refletem o período selecionado no filtro global de arrecadação.".replace(",", "."))

                csv_bytes = shown[display_cols].to_csv(index=False).encode("utf-8-sig")
                st.download_button(
                    "Baixar resultado da conferência (CSV)", csv_bytes,
                    file_name=f"conferencia_crea_ms_cfem_{selected_state.lower()}.csv", mime="text/csv"
                )
                xlsx_buffer = io.BytesIO()
                with pd.ExcelWriter(xlsx_buffer, engine="openpyxl") as writer:
                    shown[display_cols].to_excel(writer, sheet_name="Conferencia", index=False)
                    # Força identificadores como texto no Excel para preservar zeros à esquerda.
                    ws = writer.sheets["Conferencia"]
                    if "CNPJ" in display_cols:
                        cnpj_col_idx = display_cols.index("CNPJ") + 1
                        for cell in ws.iter_cols(min_col=cnpj_col_idx, max_col=cnpj_col_idx, min_row=2):
                            for c in cell:
                                c.number_format = "@"
                                c.value = str(c.value) if c.value is not None else ""
                    # Ajusta larguras para facilitar leitura, sem deixar valores numéricos como ####.
                    for column_cells in ws.columns:
                        letter = column_cells[0].column_letter
                        max_len = max((len(str(c.value)) if c.value is not None else 0) for c in column_cells[:250])
                        ws.column_dimensions[letter].width = min(max(max_len + 2, 12), 38)
                    holder_summary.groupby("Resultado_conferencia", dropna=False).agg(
                        Titulares=("CNPJ", "nunique"),
                        Valor_CFEM_R=("Valor_CFEM_R", "sum"),
                        Processos=("Processos_distintos", "sum")
                    ).reset_index().to_excel(writer, sheet_name="Resumo por titular", index=False)
                st.download_button(
                    "Baixar resultado em Excel", xlsx_buffer.getvalue(),
                    file_name=f"conferencia_crea_ms_cfem_{selected_state.lower()}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
                st.caption("A conferência considera os filtros atualmente aplicados ao painel e somente identificadores com formato de CNPJ. A ausência no cadastro é um indício para análise, não uma conclusão de irregularidade.")
        except Exception as exc:
            st.error("Não foi possível processar a lista de empresas. Confira se o CSV possui CNPJ e nome da empresa.")
            st.caption(str(exc))
    else:
        st.info("Carregue o arquivo EMPRESAS_CREA_MS.csv para iniciar a conferência. O arquivo é processado na sessão e não precisa ser publicado no GitHub.")

with tab_method:
    st.subheader("Como interpretar o painel")
    st.markdown("""
- **Fonte:** arquivo público de arrecadação da CFEM disponibilizado pela ANM.
- **Período:** anos de arrecadação presentes na versão publicada do arquivo (2022 a 2026).
- **Filtro principal:** UF; o painel inicia no Ceará para apoiar o treinamento.
- **Consolidação:** registros são agrupados por identificador disponível e/ou processo; processos diferentes podem pertencer ao mesmo titular.
- **Limitações:** CPF pode estar mascarado; o valor recolhido não é faturamento nem medida direta de produção; registros históricos não comprovam atividade atual.
- **Uso fiscalizatório:** os resultados são indícios para planejamento e seleção de alvos de verificação, não conclusões automáticas de infração.
- **Validação recomendada:** consultar o cadastro da ANM, a situação cadastral do CNPJ, o registro empresarial no CREA competente, as ARTs e a atividade efetivamente executada.
""")
    st.markdown(f"**Arquivo de origem:** [{SOURCE_NAME}]({SOURCE_URL})")

st.caption("Painel demonstrativo. Antes de usar em procedimento oficial, valide amostras dos dados, critérios de triagem e atualidade das informações.")
