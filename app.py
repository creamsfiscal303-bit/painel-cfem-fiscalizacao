import io
import json
import math
import re
import tempfile
import time
import zipfile
import unicodedata
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

try:
    import shapefile  # pyshp: lê shapefile sem depender de geopandas
except ImportError:  # o app segue funcionando sem a aba de polígonos
    shapefile = None

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
    digits = re.sub(r"\D", "", re.sub(r"\.0+$", "", str(value).strip()))
    # CSVs podem importar CNPJ como número e perder zeros à esquerda.
    if digits and len(digits) < 14:
        digits = digits.zfill(14)
    return digits

def _norm_header(value) -> str:
    """Minúsculas, sem acento e sem símbolos: 'Situação Empresa' -> 'situacaoempresa'."""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", text.lower())


CNPJ_HINTS = ("cnpj", "cpfcnpj", "cnpjcpf", "documento")
NAME_HINTS = ("razaosocial", "nomeempresarial", "nomedaempresa", "nomeempresa", "empresa", "nome")
STATUS_HINTS = ("situacaoempresa", "situacaoregistro", "situacao", "status")


def guess_column(columns, hints):
    """Acha a coluna cujo cabeçalho mais se parece com as dicas (igual primeiro, depois 'contém')."""
    keys = {col: _norm_header(col) for col in columns}
    for hint in hints:
        for col, key in keys.items():
            if key == hint:
                return col
    for hint in hints:
        for col, key in keys.items():
            if hint in key:
                return col
    return None


def read_registry_table(uploaded_file, sheet_name=None) -> pd.DataFrame:
    """Lê a lista de empresas (Excel ou CSV). Detecta sozinho a linha do cabeçalho, mesmo com título acima."""
    raw = uploaded_file.getvalue()
    if uploaded_file.name.lower().endswith((".xlsx", ".xlsm")):
        table = pd.read_excel(io.BytesIO(raw), sheet_name=sheet_name or 0, header=None, dtype=str, engine="openpyxl")
    else:
        table, last_error = None, None
        for encoding in ("utf-8-sig", "latin1"):
            try:
                table = pd.read_csv(io.BytesIO(raw), sep=None, engine="python", header=None, dtype=str, encoding=encoding)
                break
            except Exception as exc:
                last_error = exc
        if table is None:
            raise ValueError(f"Não foi possível ler o CSV: {last_error}")
    table = table.dropna(how="all").reset_index(drop=True)
    if table.empty:
        raise ValueError("O arquivo está vazio.")
    header_row = 0
    for i in range(min(25, len(table))):
        if any("cnpj" in _norm_header(v) for v in table.iloc[i].dropna()):
            header_row = i
            break
    names, seen = [], {}
    for j, value in enumerate(table.iloc[header_row]):
        name = str(value).strip() if pd.notna(value) and str(value).strip() else f"coluna_{j + 1}"
        seen[name] = seen.get(name, 0) + 1
        names.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    body = table.iloc[header_row + 1:].reset_index(drop=True)
    body.columns = names
    return body


def build_registry(table, cnpj_col, name_col=None, status_col=None) -> pd.DataFrame:
    """Padroniza a lista do conselho: uma linha por CNPJ (14 dígitos), priorizando cadastro ATIVO."""
    out = pd.DataFrame({
        "CNPJ_normalizado": table[cnpj_col].map(normalize_document),
        "Empresa_CREA": table[name_col].fillna("").astype(str).str.strip() if name_col else "",
        "Situacao_CREA": table[status_col].fillna("").astype(str).str.strip() if status_col else "Não informada",
    })
    out = out[out["CNPJ_normalizado"].str.len().eq(14)].copy()
    out["_ativo"] = out["Situacao_CREA"].str.upper().eq("ATIVO")
    return out.sort_values("_ativo", ascending=False).drop_duplicates("CNPJ_normalizado").drop(columns="_ativo")

IBGE_MESH_URL = "https://servicodados.ibge.gov.br/api/v3/malhas/estados/{uf}"
IBGE_CITIES_URL = "https://servicodados.ibge.gov.br/api/v1/localidades/estados/{uf}/municipios"


def _iter_coords(geometry):
    """Percorre todos os pares [lon, lat] de uma geometria GeoJSON."""
    def walk(c):
        if c and isinstance(c[0], (int, float)):
            yield c
        else:
            for item in c:
                yield from walk(item)
    yield from walk((geometry or {}).get("coordinates", []))


def geojson_bounds(geojson):
    """Retorna (lon_min, lat_min, lon_max, lat_max) ou None se não houver coordenadas."""
    xs, ys = [], []
    for feature in geojson.get("features", []):
        for c in _iter_coords(feature.get("geometry")):
            xs.append(c[0])
            ys.append(c[1])
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _is_lonlat(geojson) -> bool:
    b = geojson_bounds(geojson)
    return b is not None and -180 <= b[0] <= 180 and -180 <= b[2] <= 180 and -90 <= b[1] <= 90 and -90 <= b[3] <= 90


def feature_centroid(geometry):
    """Centro aproximado (lon, lat) do maior polígono da geometria (fórmula do centroide de polígono)."""
    if not geometry:
        return None
    coords = geometry.get("coordinates", [])
    polys = [coords] if geometry.get("type") == "Polygon" else coords if geometry.get("type") == "MultiPolygon" else []
    best, best_area = None, -1.0
    for poly in polys:
        ring = poly[0] if poly else []
        if len(ring) < 3:
            continue
        a = cx = cy = 0.0
        for (x1, y1, *_), (x2, y2, *_) in zip(ring, ring[1:] + ring[:1]):
            cross = x1 * y2 - x2 * y1
            a += cross
            cx += (x1 + x2) * cross
            cy += (y1 + y2) * cross
        if a == 0:
            continue
        area = abs(a) / 2
        if area > best_area:
            best_area, best = area, (cx / (3 * a), cy / (3 * a))
    if best is None:
        pts = list(_iter_coords(geometry))
        if not pts:
            return None
        best = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    return best


def _geojson_from_local_zip(state: str):
    geo_zip = Path(__file__).resolve().parent / "dados" / "base_municipal_cfem_2022.zip"
    if not geo_zip.exists():
        raise FileNotFoundError("dados/base_municipal_cfem_2022.zip não encontrado.")
    with zipfile.ZipFile(geo_zip, "r") as archive:
        with archive.open(f"municipios/{state.upper()}.geojson") as file:
            return json.load(file)


def _geojson_from_ibge(state: str):
    """Baixa a malha municipal da UF direto da API do IBGE e acrescenta o nome dos municípios."""
    resp = requests.get(
        IBGE_MESH_URL.format(uf=state.upper()),
        params={"formato": "application/vnd.geo+json", "qualidade": "minima", "intrarregiao": "municipio"},
        timeout=60,
    )
    resp.raise_for_status()
    geo = resp.json()
    try:
        names_resp = requests.get(IBGE_CITIES_URL.format(uf=state.upper()), timeout=30)
        names_resp.raise_for_status()
        names = {str(c["id"]): c["nome"] for c in names_resp.json()}
    except Exception:
        names = {}
    for feature in geo.get("features", []):
        props = feature.setdefault("properties", {})
        code = str(props.get("codarea", ""))
        props["NM_MUN"] = names.get(code, code)
    return geo


@st.cache_data(show_spinner=False, ttl=24 * 60 * 60)
def load_municipality_geojson(state: str):
    """Carrega os polígonos municipais: primeiro o ZIP local; se falhar, usa a API do IBGE."""
    geo, errors = None, []
    for loader in (_geojson_from_local_zip, _geojson_from_ibge):
        try:
            candidate = loader(state)
            if candidate.get("features") and not _is_lonlat(candidate):
                errors.append(f"{loader.__name__}: coordenadas fora de lon/lat (projetadas?)")
                continue
            if candidate.get("features"):
                geo = candidate
                break
            errors.append(f"{loader.__name__}: sem municípios")
        except Exception as exc:  # tenta a próxima fonte
            errors.append(f"{loader.__name__}: {exc}")
    if geo is None:
        raise RuntimeError("Nenhuma fonte geográfica funcionou. " + " | ".join(errors))

    sample_props = geo["features"][0].get("properties", {}) or {}
    code_key = next(
        (key for key in ["CD_MUN", "cd_geocmu", "CD_GEOCMU", "codarea", "codigo"] if key in sample_props),
        None,
    )
    if code_key is None:
        raise ValueError(f"Não foi possível identificar o código IBGE. Campos: {list(sample_props.keys())}")
    return geo, code_key


def normalize_place(value) -> str:
    value = "" if pd.isna(value) else str(value).strip().upper()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def build_activity_map(filtered_df, geojson, code_key, metric_label, show_points=True):
    """Cruza os registros da CFEM com os polígonos e monta o mapa.

    Retorna (figura, tabela_por_municipio, municipios_sem_correspondencia).
    Municípios sem CFEM ficam cinza; os com atividade são coloridos (escala logarítmica).
    """
    feats = geojson["features"]
    geo7, geo6, name_to_code, code_to_name, code_to_centroid = set(), {}, {}, {}, {}
    for f in feats:
        props = f.setdefault("properties", {})
        code = re.sub(r"\D", "", str(props.get(code_key, "")))
        props["map_code"] = code
        name = str(props.get("NM_MUN") or props.get("name") or props.get("Name") or props.get("nome") or code)
        code_to_name[code] = name
        code_to_centroid[code] = feature_centroid(f.get("geometry"))
        geo7.add(code)
        geo6[code[:6]] = code
        name_to_code[normalize_place(name)] = code

    df = filtered_df.copy()
    df["__cod_raw"] = df["CodigoMunicipio"].astype(str).str.replace(r"\D", "", regex=True)
    pairs = df[["__cod_raw", "Município"]].drop_duplicates()

    def to_geo_code(row):
        c = row["__cod_raw"]
        if c in geo7:
            return c
        if len(c) >= 6 and c[:6] in geo6:
            return geo6[c[:6]]
        return name_to_code.get(normalize_place(row["Município"]))

    pairs["__geo_code"] = pairs.apply(to_geo_code, axis=1)
    df = df.merge(pairs, on=["__cod_raw", "Município"], how="left")

    unmatched = (
        df[df["__geo_code"].isna()]["Município"].dropna().astype(str).drop_duplicates().sort_values().tolist()
    )
    matched = df.dropna(subset=["__geo_code"])
    agg = {"Registros": ("__geo_code", "size")}
    if "Valor_num" in matched.columns:
        agg["ValorRecolhido"] = ("Valor_num", "sum")
    if "Processo" in matched.columns:
        agg["Processos"] = ("Processo", "nunique")
    table = matched.groupby("__geo_code").agg(**agg).reset_index().rename(columns={"__geo_code": "CodigoIBGE"})
    table.insert(1, "Município", table["CodigoIBGE"].map(code_to_name))

    metric_col = {
        "Valor recolhido (R$)": "ValorRecolhido",
        "Quantidade de registros": "Registros",
        "Processos distintos": "Processos",
    }[metric_label]
    table = table.sort_values(metric_col, ascending=False).reset_index(drop=True)

    all_codes = [f["properties"]["map_code"] for f in feats if f["properties"]["map_code"]]
    # Mapa "plano" (MapLibre): lê lon/lat direto e mostra o mapa de fundo.
    MapTrace = getattr(go, "Choroplethmap", None) or go.Choroplethmapbox
    fig = go.Figure()
    # Camada 1: todos os municípios da UF em cinza (sem atividade registrada).
    fig.add_trace(MapTrace(
        geojson=geojson, featureidkey="properties.map_code", locations=all_codes,
        z=[0] * len(all_codes), colorscale=[[0, "#9ca3af"], [1, "#9ca3af"]], showscale=False,
        marker_opacity=0.35, marker_line_width=0.5, marker_line_color="#6b7280", hoverinfo="skip",
    ))
    # Camada 2: só municípios com CFEM, coloridos pela métrica (log para não "apagar" os menores).
    vals = pd.to_numeric(table[metric_col], errors="coerce").fillna(0).clip(lower=0)
    if not table.empty:
        z = (vals + 1).map(lambda v: math.log10(v))
        vmax = float(vals.max())
        ticks = [10 ** k for k in range(0, int(math.ceil(math.log10(vmax + 1))) + 1)]
        fig.add_trace(MapTrace(
            geojson=geojson, featureidkey="properties.map_code", locations=table["CodigoIBGE"],
            z=z, colorscale="YlOrRd",
            customdata=table[["Município"]].assign(valor=vals).values,
            hovertemplate="<b>%{customdata[0]}</b><br>" + metric_label + ": %{customdata[1]:,.2f}<extra></extra>",
            marker_opacity=0.85, marker_line_width=0.6, marker_line_color="#374151",
            colorbar=dict(
                title=metric_label,
                tickvals=[math.log10(t + 1) for t in ticks],
                ticktext=[f"{t:,.0f}".replace(",", ".") for t in ticks],
            ),
        ))
    # Camada 3: um ponto por município com CFEM (centro do município), tamanho proporcional ao indicador.
    if show_points and not table.empty:
        PointTrace = getattr(go, "Scattermap", None) or go.Scattermapbox
        cents = table["CodigoIBGE"].map(code_to_centroid)
        ok = cents.notna()
        if ok.any():
            pv = vals[ok]
            pmax = float(pv.max()) or 1.0
            fig.add_trace(PointTrace(
                lon=[c[0] for c in cents[ok]], lat=[c[1] for c in cents[ok]], mode="markers",
                marker=dict(size=(7 + 28 * (pv / pmax) ** 0.5).tolist(), color="#dc2626", opacity=0.75),
                customdata=table.loc[ok, ["Município"]].assign(valor=pv).values,
                hovertemplate="<b>%{customdata[0]}</b><br>" + metric_label + ": %{customdata[1]:,.2f}<extra></extra>",
                showlegend=False,
            ))
    bounds = geojson_bounds(geojson)
    lon_c, lat_c, zoom = -52.0, -14.0, 4.0
    if bounds:
        lon_min, lat_min, lon_max, lat_max = bounds
        lon_c, lat_c = (lon_min + lon_max) / 2, (lat_min + lat_max) / 2
        span = max((lat_max - lat_min) * 1.1, (lon_max - lon_min) * 0.55, 0.1)
        zoom = max(2.5, min(9.0, math.log2(600 / span * 360 / 512)))
    layout_args = dict(
        height=650, margin={"r": 0, "t": 20, "l": 0, "b": 0}, paper_bgcolor="rgba(0,0,0,0)",
    )
    if hasattr(go, "Choroplethmap"):
        layout_args["map"] = dict(style="carto-positron", center=dict(lon=lon_c, lat=lat_c), zoom=zoom)
    else:
        layout_args["mapbox"] = dict(style="carto-positron", center=dict(lon=lon_c, lat=lat_c), zoom=zoom)
    fig.update_layout(**layout_args)
    return fig, table, unmatched


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

SIGMINE_ZIP_URLS = [
    "https://dadosabertos.anm.gov.br/SIGMINE/PROCESSOS_MINERARIOS/{uf}.zip",
    "https://app.anm.gov.br/dadosabertos/SIGMINE/PROCESSOS_MINERARIOS/{uf}.zip",
]


def _clean_text(value) -> str:
    return "" if value is None or (not isinstance(value, str) and pd.isna(value)) else str(value).strip()


def process_key(processo, ano=None):
    """Padroniza o número do processo como 'NUMERO/ANO' sem zeros à esquerda (ex.: '800123/2010')."""
    text = _clean_text(processo)
    if "/" in text:
        left, right = text.split("/", 1)
        num, year = re.sub(r"\D", "", left), re.sub(r"\D", "", right)
    else:
        num, year = re.sub(r"\D", "", text), re.sub(r"\D", "", _clean_text(ano))
    if not num or not year:
        return None
    return f"{int(num)}/{int(year)}"


@st.cache_data(show_spinner=False, ttl=24 * 60 * 60)
def download_sigmine_zip(state: str) -> str:
    """Baixa o shapefile (ZIP) dos processos minerários ativos da UF, publicado pela ANM."""
    path = Path(tempfile.gettempdir()) / f"SIGMINE_PROCESSOS_{state.upper()}.zip"
    if path.exists() and path.stat().st_size > 10_000 and zipfile.is_zipfile(path) \
            and time.time() - path.stat().st_mtime < 24 * 3600:
        return str(path)
    errors = []
    for template in SIGMINE_ZIP_URLS:
        url = template.format(uf=state.upper())
        part = path.with_suffix(".part")
        try:
            with requests.get(url, stream=True, timeout=(30, 180)) as resp:
                resp.raise_for_status()
                with open(part, "wb") as f:
                    for block in resp.iter_content(chunk_size=1024 * 1024):
                        if block:
                            f.write(block)
            if not zipfile.is_zipfile(part):
                raise RuntimeError("o arquivo baixado não é um ZIP válido")
            part.replace(path)
            return str(path)
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("Não foi possível baixar o shapefile do SIGMINE. " + " | ".join(errors))


def _round_coords(c, nd=5):
    """Arredonda as coordenadas (~1 m) para reduzir o tamanho do mapa."""
    if c and isinstance(c[0], (int, float)):
        return [round(float(v), nd) for v in c]
    return [_round_coords(item, nd) for item in c]


def _open_shapefile(shp_path: str):
    last_error = None
    for encoding in ("utf-8", "latin-1"):
        reader = shapefile.Reader(shp_path, encoding=encoding)
        try:
            fields = [f[0].upper() for f in reader.fields[1:]]
            records = [dict(zip(fields, list(rec))) for rec in reader.records()]
            return reader, records
        except UnicodeDecodeError as exc:
            last_error = exc
            reader.close()
    raise ValueError(f"Não foi possível decodificar os atributos do shapefile: {last_error}")


@st.cache_data(show_spinner=False, ttl=24 * 60 * 60)
def load_sigmine_polygons(state: str, keys: tuple):
    """Lê o shapefile da UF e guarda só os polígonos dos processos que aparecem na CFEM (keys)."""
    if shapefile is None:
        raise ImportError("Biblioteca 'pyshp' não instalada. Adicione 'pyshp' ao requirements.txt.")
    zip_path = download_sigmine_zip(state)
    wanted = set(keys)
    features, total = [], 0
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(tmp)
        shp_files = sorted(Path(tmp).rglob("*.shp"))
        if not shp_files:
            raise ValueError("O ZIP do SIGMINE não contém arquivo .shp.")
        reader, records = _open_shapefile(str(shp_files[0]))
        try:
            total = len(records)
            for idx, attrs in enumerate(records):
                key = process_key(attrs.get("PROCESSO")) or process_key(attrs.get("NUMERO"), attrs.get("ANO"))
                if key not in wanted:
                    continue
                geom = reader.shape(idx).__geo_interface__
                if geom.get("type") not in ("Polygon", "MultiPolygon"):
                    continue
                features.append({
                    "type": "Feature",
                    "properties": {
                        "map_id": str(idx), "key": key,
                        "processo": _clean_text(attrs.get("PROCESSO")), "titular": _clean_text(attrs.get("NOME")),
                        "fase": _clean_text(attrs.get("FASE")), "substancia": _clean_text(attrs.get("SUBS")),
                        "uso": _clean_text(attrs.get("USO")), "area_ha": attrs.get("AREA_HA"),
                        "ult_evento": _clean_text(attrs.get("ULT_EVENTO")),
                    },
                    "geometry": {"type": geom["type"], "coordinates": _round_coords(geom["coordinates"])},
                })
        finally:
            reader.close()
    return {"type": "FeatureCollection", "features": features}, total


def log_scale(vals):
    """Escala logarítmica para colorir: municípios/processos pequenos não ficam 'apagados'."""
    z = (vals + 1).map(math.log10)
    vmax = float(vals.max()) if len(vals) else 0.0
    ticks = [10 ** k for k in range(0, int(math.ceil(math.log10(vmax + 1))) + 1)]
    return z, [math.log10(t + 1) for t in ticks], [f"{t:,.0f}".replace(",", ".") for t in ticks]


def view_from_bounds(bounds):
    """Centro e zoom aproximados para enquadrar o mapa."""
    if not bounds:
        return -52.0, -14.0, 4.0
    lon_min, lat_min, lon_max, lat_max = bounds
    span = max((lat_max - lat_min) * 1.1, (lon_max - lon_min) * 0.55, 0.05)
    zoom = max(2.5, min(12.0, math.log2(600 / span * 360 / 512)))
    return (lon_min + lon_max) / 2, (lat_min + lat_max) / 2, zoom


def build_process_map(filtered_df, sig_geo, metric_label):
    """Liga os processos da CFEM (filtrados) aos polígonos do SIGMINE e monta o mapa.

    Retorna (figura | None, tabela_por_processo, processos_sem_poligono, estatisticas, geojson_selecionado).
    """
    df = filtered_df.copy()
    anos = df["AnoDoProcesso"] if "AnoDoProcesso" in df.columns else [None] * len(df)
    df["__pkey"] = [process_key(p, a) for p, a in zip(df["Processo"], anos)]
    df = df.dropna(subset=["__pkey"])

    def top_values(series):
        return ", ".join(sorted({str(v).strip() for v in series if str(v).strip()})[:5])

    agg = {"Registros": ("__pkey", "size")}
    if "Valor_num" in df.columns:
        agg["ValorRecolhido"] = ("Valor_num", "sum")
    if "Município" in df.columns:
        agg["Municipios_CFEM"] = ("Município", top_values)
    if "Substância" in df.columns:
        agg["Substancias_CFEM"] = ("Substância", top_values)
    cfem = df.groupby("__pkey").agg(**agg).reset_index().rename(columns={"__pkey": "key"})

    feats = sig_geo.get("features", [])
    rows = []
    for f in feats:
        p = f["properties"]
        cen = feature_centroid(f["geometry"])
        rows.append({
            "map_id": p["map_id"], "key": p["key"], "Processo": p.get("processo"), "Titular": p.get("titular"),
            "Fase": p.get("fase"), "Substancia_SIGMINE": p.get("substancia"), "Uso": p.get("uso"),
            "Area_ha": p.get("area_ha"), "Ultimo_evento": p.get("ult_evento"),
            "lon": cen[0] if cen else None, "lat": cen[1] if cen else None,
        })
    poly_all = pd.DataFrame(rows, columns=["map_id", "key", "Processo", "Titular", "Fase", "Substancia_SIGMINE",
                                           "Uso", "Area_ha", "Ultimo_evento", "lon", "lat"])
    have_polygon = set(poly_all["key"])
    unmatched = cfem[~cfem["key"].isin(have_polygon)].copy()
    sort_col = "ValorRecolhido" if "ValorRecolhido" in unmatched.columns else "Registros"
    unmatched = unmatched.sort_values(sort_col, ascending=False).rename(columns={"key": "Processo_chave"})

    poly = poly_all.merge(cfem, on="key", how="inner")
    stats = {"processos_cfem": len(cfem), "com_poligono": int(cfem["key"].isin(have_polygon).sum()),
             "poligonos": len(poly)}
    if poly.empty:
        return None, pd.DataFrame(), unmatched, stats, {"type": "FeatureCollection", "features": []}

    keys_sel = set(poly["key"])
    geo_sel = {"type": "FeatureCollection", "features": [f for f in feats if f["properties"]["key"] in keys_sel]}

    metric_col = "ValorRecolhido" if metric_label.startswith("Valor") and "ValorRecolhido" in poly.columns else "Registros"
    vals = pd.to_numeric(poly[metric_col], errors="coerce").fillna(0).clip(lower=0)
    z, tickvals, ticktext = log_scale(vals)
    if metric_col == "ValorRecolhido":
        value_text = vals.map(lambda v: "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    else:
        value_text = vals.map(lambda v: f"{int(v)} registros")
    custom = pd.DataFrame({
        "p": poly["Processo"], "t": poly["Titular"], "f": poly["Fase"], "s": poly["Substancia_SIGMINE"],
        "a": pd.to_numeric(poly["Area_ha"], errors="coerce"), "v": value_text,
    }).values
    hover = ("<b>Processo %{customdata[0]}</b><br>Titular: %{customdata[1]}<br>Fase: %{customdata[2]}"
             "<br>Substância: %{customdata[3]}<br>Área: %{customdata[4]:,.1f} ha<br>CFEM: %{customdata[5]}<extra></extra>")

    MapTrace = getattr(go, "Choroplethmap", None) or go.Choroplethmapbox
    PointTrace = getattr(go, "Scattermap", None) or go.Scattermapbox
    fig = go.Figure()
    fig.add_trace(MapTrace(
        geojson=geo_sel, featureidkey="properties.map_id", locations=poly["map_id"], z=z, colorscale="YlOrRd",
        marker_opacity=0.65, marker_line_width=1.2, marker_line_color="#7f1d1d",
        customdata=custom, hovertemplate=hover,
        colorbar=dict(title=metric_label, tickvals=tickvals, ticktext=ticktext),
    ))
    # Os polígonos são pequenos: um ponto no centro de cada um garante que apareçam na visão da UF inteira.
    ok = poly["lon"].notna() & poly["lat"].notna()
    vmax = float(vals.max()) or 1.0
    fig.add_trace(PointTrace(
        lon=poly.loc[ok, "lon"].tolist(), lat=poly.loc[ok, "lat"].tolist(), mode="markers",
        marker=dict(size=(7 + 15 * (vals[ok] / vmax) ** 0.5).tolist(), color="#7f1d1d", opacity=0.85),
        customdata=custom[ok.values], hovertemplate=hover, showlegend=False,
    ))
    lon_c, lat_c, zoom = view_from_bounds(geojson_bounds(geo_sel))
    view = dict(style="carto-positron", center=dict(lon=lon_c, lat=lat_c), zoom=zoom)
    layout = dict(height=650, margin={"r": 0, "t": 20, "l": 0, "b": 0}, paper_bgcolor="rgba(0,0,0,0)")
    layout["map" if hasattr(go, "Choroplethmap") else "mapbox"] = view
    fig.update_layout(**layout)

    table_agg = {"Processo": ("Processo", "first"), "Titular": ("Titular", "first"), "Fase": ("Fase", "first"),
                 "Substancia_SIGMINE": ("Substancia_SIGMINE", "first"), "Area_ha": ("Area_ha", "sum"),
                 "Latitude": ("lat", "first"), "Longitude": ("lon", "first"), "Registros_CFEM": ("Registros", "first")}
    if "ValorRecolhido" in poly.columns:
        table_agg["Valor_CFEM_R"] = ("ValorRecolhido", "first")
    if "Municipios_CFEM" in poly.columns:
        table_agg["Municipios_CFEM"] = ("Municipios_CFEM", "first")
    table = poly.groupby("key").agg(**table_agg).reset_index().rename(columns={"key": "Processo_chave"})
    sort_by = "Valor_CFEM_R" if "Valor_CFEM_R" in table.columns else "Registros_CFEM"
    return fig, table.sort_values(sort_by, ascending=False).reset_index(drop=True), unmatched, stats, geo_sel


def summarize_holders(df, id_col):
    """Uma linha por titular + UF + município + substância, com os anos agrupados e a contagem de processos."""
    work = df.copy()
    for col in ["UF", "Município", "Substância"]:
        if col not in work.columns:
            work[col] = "Não informado"
        work[col] = work[col].fillna("").astype(str).str.strip().replace("", "Não informado")
    if "Processo" not in work.columns:
        work["Processo"] = ""
    work["__ano"] = pd.to_numeric(work["Ano_num"], errors="coerce") if "Ano_num" in work.columns else float("nan")
    work["__valor"] = pd.to_numeric(work["Valor_num"], errors="coerce").fillna(0.0) if "Valor_num" in work.columns else 0.0
    dims = [id_col, "UF", "Município", "Substância"]

    base = work.groupby(dims, dropna=False).agg(
        Registros_CFEM=("__ano", "size"),
        Processos_distintos=("Processo", "nunique"),
        Valor_CFEM_R=("__valor", "sum"),
        Ultimo_ano_CFEM=("__ano", "max"),
    ).reset_index()

    text_cols = ["Anos_CFEM", "Registros_por_ano", "Processos_por_ano"]
    dated = work.dropna(subset=["__ano"])
    if dated.empty:
        for col in text_cols:
            base[col] = "Não informado"
    else:
        per_year = (
            dated.groupby(dims + ["__ano"], dropna=False)
            .agg(reg=("__ano", "size"), proc=("Processo", "nunique"))
            .reset_index().sort_values("__ano")
        )
        year_txt = per_year["__ano"].astype(int).astype(str)
        per_year["year_txt"] = year_txt
        per_year["reg_txt"] = year_txt + ": " + per_year["reg"].astype(str)
        per_year["proc_txt"] = year_txt + ": " + per_year["proc"].astype(str)
        joined = per_year.groupby(dims, dropna=False).agg(
            Anos_CFEM=("year_txt", lambda s: ", ".join(s)),
            Registros_por_ano=("reg_txt", lambda s: "; ".join(s)),
            Processos_por_ano=("proc_txt", lambda s: "; ".join(s)),
        ).reset_index()
        base = base.merge(joined, on=dims, how="left")
        for col in text_cols:
            base[col] = base[col].fillna("Não informado")
    base["Ultimo_ano_CFEM"] = base["Ultimo_ano_CFEM"].map(lambda v: str(int(v)) if pd.notna(v) else "Não informado")
    ordered = dims + ["Anos_CFEM", "Ultimo_ano_CFEM", "Registros_por_ano", "Processos_por_ano",
                      "Registros_CFEM", "Processos_distintos", "Valor_CFEM_R"]
    return base[ordered].sort_values("Valor_CFEM_R", ascending=False).reset_index(drop=True)


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
tab_overview, tab_map, tab_sig, tab_companies, tab_records, tab_export, tab_match, tab_method = st.tabs(
    ["Visão geral", "Mapa municipal", "Localização dos processos", "Empresas e processos", "Registros detalhados", "Exportar dados", "Conferência CREA", "Metodologia"]
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
    st.subheader("Mapa municipal da atividade mineral (CFEM)")
    st.caption(
        "Municípios em cinza não têm arrecadação de CFEM no recorte; os coloridos têm atividade mineral declarada. "
        "A cor usa escala logarítmica para que municípios menores também apareçam."
    )
    if "CodigoMunicipio" not in filtered.columns or "Município" not in filtered.columns:
        st.warning("A base filtrada não contém as colunas CodigoMunicipio/Município.")
    else:
        map_metric = st.radio(
            "Indicador do mapa",
            ["Valor recolhido (R$)", "Quantidade de registros", "Processos distintos"],
            horizontal=True,
        )
        show_points = st.checkbox("Mostrar pontos (centro de cada município com atividade)", value=True)
        try:
            geojson, code_key = load_municipality_geojson(selected_state)
            fig_map, city_table, unmatched = build_activity_map(filtered, geojson, code_key, map_metric, show_points)
            st.plotly_chart(fig_map, use_container_width=True)
            st.caption(
                f"Municípios na malha: {len(geojson['features']):,}. "
                f"Municípios com CFEM no mapa: {len(city_table):,}."
            )
            if city_table.empty:
                st.warning(
                    "Os limites foram carregados, mas nenhum município da ANM foi associado à malha. "
                    "Veja os detalhes abaixo."
                )
            if unmatched:
                with st.expander(f"Municípios da ANM sem correspondência na malha ({len(unmatched)})"):
                    st.write(", ".join(unmatched))
            if not city_table.empty:
                st.dataframe(city_table, use_container_width=True, hide_index=True)
                st.download_button(
                    "Baixar resumo municipal (CSV)",
                    city_table.to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"cfem_{selected_state.lower()}_resumo_municipal.csv",
                    mime="text/csv",
                )
        except Exception as exc:
            st.warning("Não foi possível montar o mapa municipal neste momento.")
            with st.expander("Detalhes técnicos"):
                st.code(repr(exc))

with tab_sig:
    st.subheader("Localização real dos processos minerários com CFEM (SIGMINE/ANM)")
    st.caption(
        "Cada polígono é a área do processo minerário (poligonal) cadastrada na ANM, ligada ao número do processo "
        "que aparece na arrecadação da CFEM. Os filtros da barra lateral valem para este mapa. O SIGMINE traz apenas "
        "processos ativos: processos encerrados podem constar na CFEM e não ter polígono."
    )
    if shapefile is None:
        st.warning("Falta a biblioteca `pyshp`. Adicione a linha `pyshp` ao requirements.txt do repositório e reinicie o app.")
    elif "Processo" not in filtered.columns:
        st.warning("A base filtrada não contém a coluna Processo.")
    else:
        load_sig = st.checkbox(
            f"Carregar polígonos do SIGMINE para {selected_state}", value=False, key="sig_load",
            help="Baixa o shapefile da UF na primeira vez (alguns MB) e guarda em cache.",
        )
        if load_sig:
            try:
                id_cols = [c for c in ["Processo", "AnoDoProcesso"] if c in df.columns]
                pairs = df[id_cols].drop_duplicates()
                anos_all = pairs["AnoDoProcesso"] if "AnoDoProcesso" in pairs.columns else [None] * len(pairs)
                all_keys = tuple(sorted({k for k in (process_key(p, a) for p, a in zip(pairs["Processo"], anos_all)) if k}))
                with st.spinner("Baixando e lendo os polígonos do SIGMINE..."):
                    sig_geo, sig_total = load_sigmine_polygons(selected_state, all_keys)
                if sig_geo["features"] and not _is_lonlat(sig_geo):
                    st.warning("As coordenadas do shapefile não estão em latitude/longitude; o mapa pode não aparecer corretamente.")
                sig_metric = st.radio(
                    "Colorir pelo indicador", ["Valor recolhido (R$)", "Quantidade de registros"],
                    horizontal=True, key="sig_metric",
                )
                fig_sig, sig_table, sig_unmatched, sig_stats, sig_selected = build_process_map(filtered, sig_geo, sig_metric)
                m1, m2, m3 = st.columns(3)
                m1.metric("Processos com CFEM (filtro atual)", f"{sig_stats['processos_cfem']:,}".replace(",", "."))
                m2.metric("Com polígono no SIGMINE", f"{sig_stats['com_poligono']:,}".replace(",", "."))
                m3.metric("Sem polígono (ex.: inativos)", f"{len(sig_unmatched):,}".replace(",", "."))
                if fig_sig is None:
                    st.warning(
                        "Nenhum processo da CFEM foi encontrado entre os processos ativos do SIGMINE neste recorte. "
                        "Veja abaixo os números de processo sem polígono para conferir o formato."
                    )
                else:
                    st.plotly_chart(fig_sig, use_container_width=True)
                    st.caption(
                        f"{sig_stats['poligonos']:,} polígonos exibidos (um processo pode ter mais de um). "
                        f"O SIGMINE da UF tem {sig_total:,} polígonos ativos no total.".replace(",", ".")
                    )
                    st.dataframe(sig_table, use_container_width=True, hide_index=True)
                    d1, d2 = st.columns(2)
                    d1.download_button(
                        "Baixar tabela de processos (CSV)", sig_table.to_csv(index=False).encode("utf-8-sig"),
                        file_name=f"processos_cfem_sigmine_{selected_state.lower()}.csv", mime="text/csv",
                    )
                    d2.download_button(
                        "Baixar polígonos (GeoJSON, para QGIS/Google Earth)",
                        json.dumps(sig_selected).encode("utf-8"),
                        file_name=f"poligonos_cfem_sigmine_{selected_state.lower()}.geojson",
                        mime="application/geo+json",
                    )
                if not sig_unmatched.empty:
                    with st.expander(f"Processos da CFEM sem polígono ativo no SIGMINE ({len(sig_unmatched)})"):
                        st.dataframe(sig_unmatched, use_container_width=True, hide_index=True)
            except Exception as exc:
                st.warning("Não foi possível montar o mapa dos processos neste momento.")
                with st.expander("Detalhes técnicos"):
                    st.code(repr(exc))

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
    records_view = st.radio(
        "Visualização",
        ["Agrupada por titular, município e substância", "Linha a linha (registros originais)"],
        horizontal=True, key="records_view",
    )
    if records_view.startswith("Agrupada") and "CPF_CNPJ" in filtered.columns:
        grouped_records = summarize_holders(filtered, "CPF_CNPJ")
        st.dataframe(grouped_records, use_container_width=True, hide_index=True, height=520)
        st.caption(
            "Cada linha reúne os anos de arrecadação de um mesmo titular em um município e substância. "
            "Processos_distintos conta os processos vinculados; Processos_por_ano mostra quantos processos "
            "tiveram CFEM em cada ano; Registros_por_ano conta os lançamentos (um pagamento mensal é um registro)."
        )
        st.download_button(
            "Baixar visão agrupada (CSV)", grouped_records.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"cfem_{selected_state.lower()}_registros_agrupados.csv", mime="text/csv",
        )
    else:
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
    crea_label = st.text_input(
        "Nome do Conselho (aparece nos rótulos e nos arquivos)", value="CREA", key="crea_label",
        help="Ex.: CREA-CE, CREA-MS. Serve só para identificar os resultados.",
    ).strip() or "CREA"
    FOUND_LABEL = f"Encontrado no {crea_label}"
    NOT_FOUND_LABEL = f"Não localizado no {crea_label}"
    st.subheader(f"Conferência de titulares da ANM com empresas registradas no {crea_label}")
    st.write(
        "Envie a relação de empresas registradas no seu Conselho em Excel (.xlsx) ou CSV, com as colunas "
        "CNPJ, nome da empresa e situação do registro. O cruzamento usa o CNPJ normalizado, ignorando pontos, "
        "barras e traços. A tabela detalha cada combinação de CNPJ, UF, município e substância. "
        "O resultado é uma triagem e não determina, isoladamente, obrigatoriedade de registro ou ocorrência de infração."
    )
    uploaded_crea = st.file_uploader(
        f"Carregar lista de empresas registradas no {crea_label} (Excel ou CSV)", type=["xlsx", "xlsm", "csv"],
        key="crea_upload",
        help="Colunas esperadas: CNPJ, Nome e Situação. Os cabeçalhos podem variar e o app tenta identificá-los.",
    )
    if uploaded_crea is not None:
        try:
            sheet = None
            if uploaded_crea.name.lower().endswith((".xlsx", ".xlsm")):
                sheet_names = pd.ExcelFile(io.BytesIO(uploaded_crea.getvalue()), engine="openpyxl").sheet_names
                if len(sheet_names) > 1:
                    sheet = st.selectbox("Aba da planilha", sheet_names, key=f"crea_sheet_{uploaded_crea.name}")
            registry_table = read_registry_table(uploaded_crea, sheet)
            reg_cols = list(registry_table.columns)
            guess_cnpj = guess_column(reg_cols, CNPJ_HINTS)
            guess_name = guess_column([c for c in reg_cols if c != guess_cnpj], NAME_HINTS)
            guess_status = guess_column([c for c in reg_cols if c not in (guess_cnpj, guess_name)], STATUS_HINTS)
            none_option = "(nenhuma)"
            with st.expander("Colunas identificadas (confira e corrija se precisar)", expanded=guess_cnpj is None):
                c1, c2, c3 = st.columns(3)
                key_sfx = f"{uploaded_crea.name}_{sheet}"
                cnpj_col = c1.selectbox(
                    "Coluna do CNPJ", reg_cols, key=f"crea_cnpj_{key_sfx}",
                    index=reg_cols.index(guess_cnpj) if guess_cnpj in reg_cols else 0,
                )
                name_choice = c2.selectbox(
                    "Coluna do nome da empresa", [none_option] + reg_cols, key=f"crea_name_{key_sfx}",
                    index=reg_cols.index(guess_name) + 1 if guess_name in reg_cols else 0,
                )
                status_choice = c3.selectbox(
                    "Coluna da situação", [none_option] + reg_cols, key=f"crea_status_{key_sfx}",
                    index=reg_cols.index(guess_status) + 1 if guess_status in reg_cols else 0,
                )
            crea = build_registry(
                registry_table, cnpj_col,
                None if name_choice == none_option else name_choice,
                None if status_choice == none_option else status_choice,
            )
            if crea.empty:
                raise ValueError("Nenhum CNPJ válido (14 dígitos) na coluna escolhida. Confira a coluna do CNPJ.")
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

                detail = summarize_holders(cfem_match, "CNPJ_normalizado")
                detail = detail.merge(crea, on="CNPJ_normalizado", how="left", indicator=True)
                detail["Resultado_conferencia"] = detail["_merge"].map({"both": FOUND_LABEL, "left_only": NOT_FOUND_LABEL})
                detail = detail.drop(columns=["_merge"]).rename(columns={"CNPJ_normalizado": "CNPJ"})

                name_candidates = [c for c in ["Nome", "Titular", "NomeTitular", "NomePessoa", "NomeContribuinte"] if c in cfem_match.columns]
                if name_candidates:
                    name_col = name_candidates[0]
                    names = cfem_match.groupby("CNPJ_normalizado")[name_col].agg(
                        lambda x: next((str(v).strip() for v in x if pd.notna(v) and str(v).strip()), "")
                    ).rename("Nome_ANM").reset_index().rename(columns={"CNPJ_normalizado": "CNPJ"})
                    detail = detail.merge(names, on="CNPJ", how="left")

                status_upper = detail["Situacao_CREA"].fillna("").astype(str).str.upper()
                detail.loc[detail["Resultado_conferencia"].eq(FOUND_LABEL) & status_upper.str.contains("INATIV|CANCEL|BAIXAD", regex=True), "Resultado_conferencia"] = "Encontrado — cadastro INATIVO"
                detail.loc[detail["Resultado_conferencia"].eq(FOUND_LABEL) & status_upper.str.contains("ATIVO", regex=False), "Resultado_conferencia"] = "Encontrado — cadastro ATIVO"

                # Indicadores consolidados por CNPJ: a empresa não é contada várias vezes por município/substância.
                holder_summary = detail.groupby("CNPJ", dropna=False).agg(
                    Resultado_conferencia=("Resultado_conferencia", "first"),
                    Valor_CFEM_R=("Valor_CFEM_R", "sum"),
                    Processos_distintos=("Processos_distintos", "sum"),
                ).reset_index()
                total = holder_summary["CNPJ"].nunique()
                found = holder_summary[holder_summary["Resultado_conferencia"].str.startswith("Encontrado")]["CNPJ"].nunique()
                missing_count = holder_summary[holder_summary["Resultado_conferencia"].eq(NOT_FOUND_LABEL)]["CNPJ"].nunique()
                a, b, c = st.columns(3)
                a.metric("Titulares PJ com CNPJ", f"{total:,}".replace(",", "."))
                b.metric(f"Localizados no {crea_label}", f"{found:,}".replace(",", "."))
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

                preferred_cols = ["CNPJ", "Nome_ANM", "UF", "Município", "Substância", "Anos_CFEM", "Ultimo_ano_CFEM", "Registros_por_ano", "Processos_por_ano", "Registros_CFEM", "Processos_distintos", "Valor_CFEM_R", "Empresa_CREA", "Situacao_CREA", "Resultado_conferencia"]
                display_cols = [c for c in preferred_cols if c in shown.columns]
                st.dataframe(shown[display_cols].sort_values("Valor_CFEM_R", ascending=False), use_container_width=True, hide_index=True, height=500)
                st.caption(f"Exibindo {len(shown):,} combinações de CNPJ, UF, município e substância. Os indicadores acima contam titulares únicos. Os anos e o último ano refletem o período selecionado no filtro global de arrecadação.".replace(",", "."))

                csv_bytes = shown[display_cols].to_csv(index=False).encode("utf-8-sig")
                st.download_button(
                    "Baixar resultado da conferência (CSV)", csv_bytes,
                    file_name=f"conferencia_crea_cfem_{selected_state.lower()}.csv", mime="text/csv"
                )
                xlsx_buffer = io.BytesIO()
                # Preparar tipos explicitamente antes de gravar: evita que o Excel
                # interprete CNPJ como número e "2022: 12" como hora.
                excel_data = shown[display_cols].copy()
                text_cols = ["CNPJ", "UF", "Município", "Substância", "Anos_CFEM",
                             "Ultimo_ano_CFEM", "Registros_por_ano", "Processos_por_ano", "Empresa_CREA",
                             "Situacao_CREA", "Resultado_conferencia"]
                for col in text_cols:
                    if col in excel_data.columns:
                        excel_data[col] = excel_data[col].fillna("").astype(str)
                integer_cols = ["Registros_CFEM", "Processos_distintos"]
                for col in integer_cols:
                    if col in excel_data.columns:
                        excel_data[col] = pd.to_numeric(excel_data[col], errors="coerce").fillna(0).astype("int64")
                if "Valor_CFEM_R" in excel_data.columns:
                    excel_data["Valor_CFEM_R"] = pd.to_numeric(excel_data["Valor_CFEM_R"], errors="coerce")
                with pd.ExcelWriter(xlsx_buffer, engine="openpyxl") as writer:
                    excel_data.to_excel(writer, sheet_name="Conferencia", index=False)
                    ws = writer.sheets["Conferencia"]
                    # Identificadores e campos descritivos ficam explicitamente como texto.
                    for col_name in text_cols:
                        if col_name in display_cols:
                            col_idx = display_cols.index(col_name) + 1
                            for row in range(2, ws.max_row + 1):
                                cell = ws.cell(row=row, column=col_idx)
                                cell.number_format = "@"
                                if cell.value is not None:
                                    cell.value = str(cell.value)
                    # Formatação numérica explícita para contagens e valores monetários.
                    for col_name in integer_cols:
                        if col_name in display_cols:
                            col_idx = display_cols.index(col_name) + 1
                            for row in range(2, ws.max_row + 1):
                                ws.cell(row=row, column=col_idx).number_format = "0"
                    if "Valor_CFEM_R" in display_cols:
                        col_idx = display_cols.index("Valor_CFEM_R") + 1
                        for row in range(2, ws.max_row + 1):
                            ws.cell(row=row, column=col_idx).number_format = '#,##0.00'
                    # Larguras de coluna adequadas para leitura.
                    preferred_widths = {"CNPJ": 18, "UF": 8, "Município": 22, "Substância": 24,
                                        "Anos_CFEM": 22, "Ultimo_ano_CFEM": 18, "Registros_por_ano": 32, "Processos_por_ano": 32,
                                        "Registros_CFEM": 16, "Processos_distintos": 20, "Valor_CFEM_R": 18,
                                        "Empresa_CREA": 30, "Situacao_CREA": 18, "Resultado_conferencia": 32}
                    for col_idx, col_name in enumerate(display_cols, start=1):
                        letter = ws.cell(row=1, column=col_idx).column_letter
                        ws.column_dimensions[letter].width = preferred_widths.get(col_name, 18)
                    ws.freeze_panes = "A2"
                    ws.auto_filter.ref = ws.dimensions
                    holder_summary.groupby("Resultado_conferencia", dropna=False).agg(
                        Titulares=("CNPJ", "nunique"),
                        Valor_CFEM_R=("Valor_CFEM_R", "sum"),
                        Processos=("Processos_distintos", "sum")
                    ).reset_index().to_excel(writer, sheet_name="Resumo por titular", index=False)
                st.download_button(
                    "Baixar resultado em Excel", xlsx_buffer.getvalue(),
                    file_name=f"conferencia_crea_cfem_{selected_state.lower()}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
                st.caption("A conferência considera os filtros atualmente aplicados ao painel e somente identificadores com formato de CNPJ. A ausência no cadastro é um indício para análise, não uma conclusão de irregularidade.")
        except Exception as exc:
            st.error("Não foi possível processar a lista de empresas. Confira se o arquivo (Excel ou CSV) possui uma coluna de CNPJ.")
            st.caption(str(exc))
    else:
        st.info("Carregue a planilha (Excel ou CSV) de empresas registradas no seu Conselho para iniciar a conferência. O arquivo é processado na sessão e não precisa ser publicado no GitHub.")

with tab_method:
    st.subheader("Como interpretar o painel")
    st.markdown("""
- **Fonte:** arquivo público de arrecadação da CFEM disponibilizado pela ANM.
- **Período:** anos de arrecadação presentes na versão publicada do arquivo (2022 a 2026).
- **Filtro principal:** UF; o painel inicia no Ceará para apoiar o treinamento.
- **Consolidação:** registros são agrupados por identificador disponível e/ou processo; processos diferentes podem pertencer ao mesmo titular.
- **Limitações:** CPF pode estar mascarado; o valor recolhido não é faturamento nem medida direta de produção; registros históricos não comprovam atividade atual.
- **Uso fiscalizatório:** os resultados são indícios para planejamento e seleção de alvos de verificação, não conclusões automáticas de infração.
- **Localização dos processos:** polígonos dos processos minerários ativos do SIGMINE (ANM), ligados à CFEM pelo número do processo; processos inativos não possuem polígono nessa base.
- **Validação recomendada:** consultar o cadastro da ANM, a situação cadastral do CNPJ, o registro empresarial no CREA competente, as ARTs e a atividade efetivamente executada.
""")
    st.markdown(f"**Arquivo de origem:** [{SOURCE_NAME}]({SOURCE_URL})")

st.caption("Painel demonstrativo. Antes de usar em procedimento oficial, valide amostras dos dados, critérios de triagem e atualidade das informações.")