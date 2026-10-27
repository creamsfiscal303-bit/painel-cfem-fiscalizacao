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

@st.cache_data(show_spinner=False, ttl=24 * 60 * 60)
def load_municipality_geojson(state: str):
    """Obtém limites municipais simplificados da API de malhas do IBGE."""
    url = (
        f"https://servicodados.ibge.gov.br/api/v3/malhas/estados/{state}"
        "?formato=application/vnd.geo+json&qualidade=minima&intrarregiao=municipio"
    )
    response = requests.get(url, timeout=(15, 60))
    response.raise_for_status()
    geo = response.json()
    features = geo.get("features", [])
    if not features:
        raise ValueError("A API do IBGE não retornou limites municipais para esta UF.")

    # Detecta a propriedade usada pelo IBGE para identificar o município.
    sample_props = features[0].get("properties", {}) or {}
    code_key = next(
        (key for key in ["codarea", "CD_MUN", "CD_GEOCMU", "id", "codigo", "Código"] if key in sample_props),
        None,
    )
    if code_key is None:
        # Algumas respostas usam o id da feição em vez de uma propriedade.
        if features[0].get("id") is not None:
            code_key = "__feature_id__"
        else:
            raise ValueError(f"Não foi possível identificar o código municipal. Campos: {list(sample_props.keys())}")

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
tab_overview, tab_map, tab_companies, tab_records, tab_export, tab_method = st.tabs(
    ["Visão geral", "Mapa municipal", "Empresas e processos", "Registros detalhados", "Exportar dados", "Metodologia"]
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
                st.error("Não foi possível carregar o mapa municipal do IBGE.")
                st.caption("Os gráficos e as exportações continuam disponíveis. Tente novamente mais tarde.")
                st.exception(exc)

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
